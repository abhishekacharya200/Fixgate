"""Run the agent on many benchmark issues: resumable, parallel, with a run-wide spending cap and a quota guard.

Each finished instance is written to <run>/instances/<id>.json at once, so a crash or a restart loses at
most the instances in flight. A rerun skips finished instances. `retry_infra` reruns only instances that
ended in an infrastructure failure (LLM API, sandbox, internal error); agent failures are never rerun,
so the numbers are not cherry-picked.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

from ..agent.pipeline import Pipeline
from ..budget import make_meter
from ..config import Config
from ..llm import make_backend
from ..llm.pricing import PriceBook
from ..llm.retry import wall_sleep
from ..models import TaskResult
from ..sandbox.base import SandboxError
from ..sandbox.docker import DockerSandbox, image_present, pull_image
from ..store import Store
from ..taxonomy import Blame, Category, Outcome
from ..tracing import Tracer
from .swebench import ENV_SETUP, ENVIRONMENT_NOTE, WORKDIR, issue_from_instance, test_hint

INFRA = {Category.LLM_ERROR.value, Category.SANDBOX_ERROR.value, Category.INTERNAL_ERROR.value}


class RunStopped(Exception):
    pass


class QuotaGuard:
    """Pause before a task when the gateway reports a subscription window nearly used up.

    Reads the RAG gateway's /ui/api/status (Claude Code reports its five-hour and seven-day utilization).
    Above max_five_hour it waits for that window to reset; above max_seven_day it stops the run, so a
    benchmark cannot eat the whole week's plan.
    """

    def __init__(self, status_url: str, api_key: str, provider: str = "claude", max_five_hour: float = 0.85,
                 max_seven_day: float = 0.6, sleep: Callable[[float], None] = wall_sleep,
                 log: Callable[[str], None] = print):
        self.status_url = status_url
        self.api_key = api_key
        self.provider = provider
        self.max_five_hour = max_five_hour
        self.max_seven_day = max_seven_day
        self.sleep = sleep
        self.log = log
        self.lock = threading.Lock()

    def windows(self) -> Dict[str, dict]:
        try:
            status = httpx.get(self.status_url, headers={"x-api-key": self.api_key}, timeout=10).json()
        except (httpx.HTTPError, ValueError):
            return {}
        for provider in status.get("providers", []):
            if provider.get("id") == self.provider:
                return ((provider.get("rate_limit") or {}).get("unifiedWindows")) or {}
        return {}

    def current(self) -> Dict[str, dict]:
        """Windows whose reading is still valid: once a window's reset time has passed, its last reading
        says nothing about the new window (the gateway only updates readings when the CLI reports them)."""
        now = time.time()
        return {name: w for name, w in self.windows().items()
                if not isinstance(w.get("resetsAt"), (int, float)) or w["resetsAt"] > now}

    def wait(self) -> None:
        with self.lock:
            while True:
                windows = self.current()
                week = (windows.get("seven_day") or {}).get("utilization")
                five = windows.get("five_hour") or {}
                if week is not None and week >= self.max_seven_day:
                    raise RunStopped(f"the {self.provider} seven-day window is at {week:.0%} "
                                     f"(limit {self.max_seven_day:.0%})")
                if five.get("utilization") is not None and five["utilization"] >= self.max_five_hour:
                    wait = max(60.0, float(five.get("resetsAt", time.time() + 900)) - time.time() + 60)
                    self.log(f"quota guard: five-hour window at {five['utilization']:.0%}, waiting {wait / 60:.0f} min")
                    self.sleep(min(wait, 3600))
                    continue
                return


@dataclass
class EvalRun:
    cfg: Config
    run_dir: Path
    run_id: str
    workers: int = 1
    retry_infra: bool = False
    max_total_usd: Optional[float] = None
    quota: Optional[QuotaGuard] = None
    store: Optional[Store] = None
    log: Callable[[str], None] = print

    def __post_init__(self) -> None:
        self.instances_dir = self.run_dir / "instances"
        self.instances_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir = self.run_dir / "traces"
        self._pull_locks: Dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._stopped: Optional[str] = None
        self.prices = PriceBook(self.cfg.pricing)
        self.llm = make_backend(self.cfg.llm)

    # ---------- bookkeeping ----------

    def result_path(self, instance_id: str) -> Path:
        return self.instances_dir / f"{instance_id}.json"

    def finished(self) -> Dict[str, dict]:
        out = {}
        for path in sorted(self.instances_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            out[data["task_id"]] = data
        return out

    def spent(self) -> float:
        return sum((d.get("usage") or {}).get("cost_usd") or 0.0 for d in self.finished().values())

    def todo(self, instances: List[dict]) -> List[dict]:
        done = self.finished()
        pending = []
        for instance in instances:
            previous = done.get(instance["instance_id"])
            if previous is None:
                pending.append(instance)
            elif self.retry_infra and previous["outcome"]["category"] in INFRA:
                pending.append(instance)
        return pending

    # ---------- running ----------

    def run(self, instances: List[dict]) -> Dict[str, dict]:
        pending = self.todo(instances)
        self.log(f"{len(instances)} instances, {len(instances) - len(pending)} already finished, "
                 f"{len(pending)} to run with {self.workers} worker(s)")
        from .. import __version__
        from ..agent import prompts

        prompt_source = Path(prompts.__file__).read_text(encoding="utf-8")
        (self.run_dir / "prompts.py").write_text(prompt_source, encoding="utf-8")
        (self.run_dir / "run.json").write_text(json.dumps({
            "run_id": self.run_id, "model": self.cfg.llm.model, "provider": self.cfg.llm.provider,
            "fixgate_version": __version__, "prompts_sha256": hashlib.sha256(prompt_source.encode()).hexdigest(),
            "base_url": self.cfg.llm.base_url, "instances": [i["instance_id"] for i in instances],
            "budget": vars(self.cfg.budget), "agent": vars(self.cfg.agent), "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, indent=2, default=str), encoding="utf-8")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._run_one, inst): inst["instance_id"] for inst in pending}
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    result = future.result()
                except RunStopped as stop:
                    self.log(f"{instance_id}: not started ({stop})")
                    continue
                if result is None:
                    continue
                o = result.outcome
                cost = result.usage.get("cost_usd")
                self.log(f"{instance_id}: {o.category.value}/{o.subcategory} gate={'pass' if result.gate_passed else '-'} "
                         f"steps={result.usage.get('steps')} cost={'$%.3f' % cost if cost is not None else 'n/a'} "
                         f"time={result.usage.get('wall_s')}s")
        if self._stopped:
            self.log(f"run stopped early: {self._stopped}")
        return self.finished()

    def _check_run_limits(self) -> None:
        if self._stopped:
            raise RunStopped(self._stopped)
        if self.max_total_usd is not None and self.spent() >= self.max_total_usd:
            self._stopped = f"the run's spending cap ${self.max_total_usd:.2f} is reached"
            raise RunStopped(self._stopped)
        if self.quota is not None:
            try:
                self.quota.wait()
            except RunStopped as stop:
                self._stopped = str(stop)
                raise

    def _ensure_image(self, image: str) -> None:
        with self._lock:
            lock = self._pull_locks.setdefault(image, threading.Lock())
        with lock:
            if not image_present(image):
                self.log(f"pulling {image}")
                pull_image(image)

    def _run_one(self, instance: dict) -> Optional[TaskResult]:
        self._check_run_limits()
        instance_id = instance["instance_id"]
        issue = issue_from_instance(instance)
        trace_dir = self.traces_dir / instance_id
        if trace_dir.exists():  # a rerun of an infrastructure failure: keep the old trace next to the new one
            trace_dir.rename(trace_dir.with_name(f"{instance_id}.{int(time.time())}"))
        tracer = Tracer(trace_dir, task_id=instance_id, run_id=self.run_id)
        meter = make_meter(self.cfg, self.prices)
        sandbox = None
        sb = self.cfg.sandbox
        try:
            with tracer.span("prepare", "phase", image=instance["image"]):
                self._ensure_image(instance["image"])
                sandbox = DockerSandbox(instance["image"], workdir=WORKDIR, env_setup=ENV_SETUP, memory=sb.memory,
                                        cpus=sb.cpus, pids_limit=sb.pids_limit, network="none",
                                        exec_timeout=sb.exec_timeout_s, max_exec_timeout=sb.max_exec_timeout_s,
                                        capture_bytes=sb.output_capture_bytes,
                                        labels={"fixgate.run": self.run_id, "fixgate.task": instance_id}).start()
            pipeline = Pipeline(self.cfg, llm=self.llm, sandbox=sandbox, tracer=tracer, meter=meter,
                                test_hint=test_hint(instance["repo"]), environment=ENVIRONMENT_NOTE)
            result = pipeline.run(issue, instance_id)
        except SandboxError as err:
            result = TaskResult(task_id=instance_id, issue=issue,
                                outcome=Outcome(Category.SANDBOX_ERROR, err.kind, "setup", err.message[:500]),
                                model=self.cfg.llm.model, provider=self.cfg.llm.provider, trace_dir=str(trace_dir),
                                usage=meter.snapshot())
        finally:
            if sandbox is not None:
                sandbox.close()
            tracer.close()
        self.result_path(instance_id).write_text(json.dumps(result.to_json(), indent=1), encoding="utf-8")
        if self.store is not None:
            self.store.save_task(result, mode="eval", run_id=self.run_id)
        return result


def write_predictions(run_dir: Path, run_id: str) -> Dict[str, int]:
    """predictions.jsonl (patches that passed fixgate's gate: what production would propose) and
    shadow.jsonl (patches the gate stopped, graded only to measure the gate)."""
    counts = {"gate_passed": 0, "shadow": 0}
    with open(run_dir / "predictions.jsonl", "w", encoding="utf-8") as main, \
            open(run_dir / "shadow.jsonl", "w", encoding="utf-8") as shadow:
        for path in sorted((run_dir / "instances").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            patch = data.get("patch") or ""
            if not patch.strip():
                continue
            if data.get("gate_passed"):
                main.write(json.dumps({"instance_id": data["task_id"], "model_name_or_path": run_id,
                                       "model_patch": patch}) + "\n")
                counts["gate_passed"] += 1
            elif data["outcome"]["blame"] in (Blame.AGENT.value, Blame.GUARDRAIL.value):
                shadow.write(json.dumps({"instance_id": data["task_id"], "model_name_or_path": f"{run_id}-shadow",
                                         "model_patch": patch}) + "\n")
                counts["shadow"] += 1
    return counts
