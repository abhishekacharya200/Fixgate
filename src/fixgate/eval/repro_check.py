"""Is the agent's reproduction real? Check it against the maintainers' fix.

A valid reproduction fails on the original code and passes once the maintainers' patch (the benchmark's
gold patch, never shown to the agent) is applied. This measures the "reproduce the bug" step on its own,
independent of whether the agent's fix was right.
"""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Dict, List

from ..sandbox.docker import DockerSandbox, image_present, pull_image
from .swebench import ENV_SETUP, WORKDIR


def check_instance(instance: dict, result: dict, timeout: int = 600) -> dict:
    repro = result.get("reproduction") or {}
    if repro.get("status") != "verified" or not repro.get("command") or not repro.get("script"):
        return {"checked": False, "reason": f"no verified reproduction ({repro.get('status') or 'none'})"}
    command = repro["command"]
    scripts = re.findall(r"(\S+\.py)\b", command)
    if not scripts:
        return {"checked": False, "reason": "no script path in the command"}
    script_path = scripts[0] if scripts[0].startswith("/") else posixpath.join(WORKDIR, scripts[0])
    if not image_present(instance["image"]):
        pull_image(instance["image"])
    verdict: Dict[str, object] = {"checked": True, "command": command}
    with DockerSandbox(instance["image"], workdir=WORKDIR, env_setup=ENV_SETUP, network="none",
                       labels={"fixgate.check": instance["instance_id"]}) as sandbox:
        sandbox.write_file(script_path, repro["script"].encode("utf-8"))
        before = sandbox.exec(command, timeout=timeout)
        sandbox.write_file("/tmp/fixgate/gold.patch", instance["patch"].encode("utf-8"))
        applied = sandbox.exec("git apply --whitespace=nowarn /tmp/fixgate/gold.patch", timeout=120)
        if not applied.ok:
            return {**verdict, "checked": False, "reason": "the gold patch did not apply"}
        after = sandbox.exec(command, timeout=timeout)
    verdict.update({"fails_before": before.exit_code != 0, "passes_with_gold": after.exit_code == 0,
                    "exit_before": before.exit_code, "exit_after": after.exit_code,
                    "valid": before.exit_code != 0 and after.exit_code == 0,
                    "output_after": after.output[-1500:]})
    return verdict


def check_run(run_dir: Path, instances: List[dict], log=print) -> Dict[str, dict]:
    path = run_dir / "repro_checks.json"
    done: Dict[str, dict] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    by_id = {i["instance_id"]: i for i in instances}
    for result_path in sorted((run_dir / "instances").glob("*.json")):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        instance_id = result["task_id"]
        if instance_id in done or instance_id not in by_id:
            continue
        try:
            done[instance_id] = check_instance(by_id[instance_id], result)
        except Exception as err:  # keep going; one broken check must not stop the rest
            done[instance_id] = {"checked": False, "reason": f"error: {err}"[:300]}
        log(f"{instance_id}: {done[instance_id].get('valid', done[instance_id].get('reason'))}")
        path.write_text(json.dumps(done, indent=1), encoding="utf-8")
    return done
