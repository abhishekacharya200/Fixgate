"""The fixgate command line."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Config, ConfigError, load_config
from .store import Store


def _reviewer(name: Optional[str]) -> str:
    if name:
        return name
    try:
        found = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        found = ""
    return found or getpass.getuser()


def _store(cfg: Config) -> Store:
    return Store(cfg.db_path)


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, default=str))


# ---------- doctor ----------

def cmd_doctor(cfg: Config, args) -> int:
    import httpx

    from .github import resolve_token
    from .llm.pricing import PriceBook
    from .sandbox.docker import docker_available

    ok = True
    print(f"fixgate {__version__}, state in {cfg.home}")
    docker = docker_available()
    ok &= docker
    print(f"[{'ok' if docker else 'FAIL'}] Docker daemon reachable")
    llm = cfg.llm
    where = llm.base_url or "the provider's API"
    print(f"[info] LLM: {llm.provider} / {llm.model} via {where}")
    if llm.uses_gateway:
        try:
            status = httpx.get(llm.base_url.rstrip("/") + "/ui/api/status",
                               headers={"x-api-key": llm.api_key() or ""}, timeout=10).json()
            providers = {p["id"]: p for p in status.get("providers", [])}
            want = {"anthropic": "claude", "openai": "codex", "gemini": "antigravity"}[llm.provider]
            item = providers.get(want) or {}
            ready = bool(item.get("ready"))
            ok &= ready
            print(f"[{'ok' if ready else 'FAIL'}] gateway provider {want}: "
                  f"{item.get('account') or item.get('problem') or 'not found'}")
            windows = ((item.get("rate_limit") or {}).get("unifiedWindows")) or {}
            for name, window in windows.items():
                print(f"[info] {want} {name} window used: {window.get('utilization', 0):.0%}")
        except (httpx.HTTPError, ValueError, KeyError) as err:
            ok = False
            print(f"[FAIL] gateway at {llm.base_url} not reachable: {err}")
    elif not llm.api_key():
        ok = False
        print(f"[FAIL] no API key in ${llm.api_key_env}")
    price = PriceBook(cfg.pricing).get(llm.model)
    print(f"[{'ok' if price else 'warn'}] price for {llm.model}: "
          f"{'$%.2f in / $%.2f out per MTok' % (price.input, price.output) if price else 'unknown, cost cap cannot be enforced in dollars'}")
    token = resolve_token(cfg.github)
    print(f"[{'ok' if token else 'warn'}] GitHub token {'found' if token else 'missing (needed for live mode)'}")
    return 0 if ok else 1


# ---------- live mode ----------

def cmd_run(cfg: Config, args) -> int:
    from .live import run_issue

    store = _store(cfg)
    result = run_issue(cfg, args.issue, store=store, keep_workspace=args.keep_workspace, model=args.model)
    o = result.outcome
    print(f"task {result.task_id}: {o.category.value}{'/' + o.subcategory if o.subcategory else ''} ({o.phase})")
    if o.message:
        print(f"  {o.message}")
    u = result.usage
    cost = u.get("cost_usd")
    print(f"  steps {u.get('steps')}, cost {'$%.4f' % cost if cost is not None else 'n/a'}, time {u.get('wall_s')}s")
    if result.approval_id:
        print(f"\nA human must approve this patch before a PR is opened:\n  fixgate show {result.approval_id}\n"
              f"  fixgate approve {result.approval_id}    |    fixgate reject {result.approval_id} --reason ...")
    print(f"trace: fixgate trace {result.task_id}")
    return 0 if not o.is_failure else 2


def cmd_triage(cfg: Config, args) -> int:
    from .agent.triage import triage_issue
    from .approval import Approvals
    from .budget import make_meter
    from .github import GitHub, parse_issue_ref, resolve_token
    from .llm import make_backend
    from .llm.pricing import PriceBook
    from .tracing import Tracer

    owner, name, number = parse_issue_ref(args.issue)
    gh = GitHub(resolve_token(cfg.github), cfg.github.api_url)
    issue = gh.issue(owner, name, number)
    task_id = "t_" + secrets.token_hex(6)
    tracer = Tracer(cfg.traces_dir / task_id, task_id=task_id)
    meter = make_meter(cfg, PriceBook(cfg.pricing))
    llm = make_backend(cfg.llm)
    result = triage_issue(issue, llm=llm, model=cfg.llm.triage_model or cfg.llm.model, meter=meter, tracer=tracer,
                          retry=cfg.retry)
    tracer.close()
    _print_json({"issue": issue.ref, **vars(result), "proceed": result.proceed, "usage": meter.snapshot()})
    if args.propose:
        labels = [label for label in result.suggested_labels if label not in issue.labels]
        comment = (f"Triage (fixgate): **{result.kind}**, severity {result.severity}, component `{result.component}`."
                   f"\n\n{result.summary}")
        approval = Approvals.from_config(_store(cfg), cfg).request(task_id, "apply_triage", {
            "repo": issue.repo, "issue_number": issue.number, "labels": labels, "comment": comment})
        print(f"\nApproval requested: fixgate show {approval.id}")
    return 0


def cmd_watch(cfg: Config, args) -> int:
    from .github import GitHub, resolve_token
    from .live import run_issue

    owner, name = args.repo.split("/", 1)
    gh = GitHub(resolve_token(cfg.github), cfg.github.api_url)
    store = _store(cfg)
    label = args.label or cfg.github.watch_label
    print(f"watching {args.repo} for open issues labelled '{label}' every {args.interval}s (Ctrl+C to stop)")
    while True:
        try:
            numbers = gh.issues_with_label(owner, name, label, limit=50)
            seen = {row["issue_ref"] for row in store.query("SELECT issue_ref FROM tasks WHERE mode = 'live'")}
            fresh = [n for n in numbers if f"{args.repo}#{n}" not in seen][: args.max_per_poll]
            for number in fresh:
                result = run_issue(cfg, f"{args.repo}#{number}", store=store)
                print(f"{args.repo}#{number}: {result.outcome.category.value} "
                      f"{'(approval ' + result.approval_id + ')' if result.approval_id else ''}")
        except KeyboardInterrupt:
            return 0
        except Exception as err:  # keep the watcher alive; the next poll retries
            print(f"poll failed: {err}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


# ---------- approvals ----------

def cmd_approvals(cfg: Config, args) -> int:
    from .approval import Approvals

    items = Approvals.from_config(_store(cfg), cfg).list(args.status)
    if not items:
        print("no approval requests" + (f" with status {args.status}" if args.status else ""))
    for a in items:
        target = f"{a.payload.get('repo')}#{a.payload.get('issue_number')}"
        print(f"{a.id}  {a.status:<9} {a.action:<13} {target:<40} created {a.created_at}  expires {a.expires_at}")
    return 0


def cmd_show(cfg: Config, args) -> int:
    from .approval import Approvals

    a = Approvals.from_config(_store(cfg), cfg).get(args.approval_id)
    p = a.payload
    print(f"{a.id}: {a.action} for {p.get('repo')}#{p.get('issue_number')}  [{a.status}]")
    print(f"task {a.task_id}, payload sha256 {a.payload_sha256[:16]}..., expires {a.expires_at}")
    if a.decided_by:
        print(f"decided by {a.decided_by} at {a.decided_at}: {a.note or ''}")
    if a.action == "open_pr":
        print(f"\nPR title: {p['title']}\nbranch:   {p['branch']} -> {p['base_branch']} (base {p['base_commit'][:12]})")
        print("\n----- PR body -----\n" + p["body"] + "\n----- patch -----\n" + p["patch"])
    else:
        print(f"\nlabels: {', '.join(p.get('labels') or []) or '(none)'}\ncomment:\n{p.get('comment') or '(none)'}")
    return 0


def cmd_approve(cfg: Config, args) -> int:
    from .approval import ApprovalError, Approvals
    from .publish import PublishError, publish

    store = _store(cfg)
    try:
        a = Approvals.from_config(store, cfg).approve(args.approval_id, _reviewer(args.by), args.note or "")
    except ApprovalError as err:
        print(f"not approved: {err}", file=sys.stderr)
        return 1
    print(f"{a.id} approved by {a.decided_by}")
    if args.no_publish:
        print(f"publish later with: fixgate publish {a.id}")
        return 0
    try:
        print(f"published: {publish(cfg, store, a.id)}")
    except PublishError as err:
        print(f"publishing failed: {err}\nretry with: fixgate publish {a.id}", file=sys.stderr)
        return 1
    return 0


def cmd_reject(cfg: Config, args) -> int:
    from .approval import ApprovalError, Approvals

    try:
        a = Approvals.from_config(_store(cfg), cfg).reject(args.approval_id, _reviewer(args.by),
                                                                      args.reason)
    except ApprovalError as err:
        print(f"not rejected: {err}", file=sys.stderr)
        return 1
    print(f"{a.id} rejected by {a.decided_by}")
    return 0


def cmd_publish(cfg: Config, args) -> int:
    from .publish import PublishError, publish

    try:
        print(publish(cfg, _store(cfg), args.approval_id))
    except PublishError as err:
        print(f"publishing failed: {err}", file=sys.stderr)
        return 1
    return 0


# ---------- inspection ----------

def cmd_tasks(cfg: Config, args) -> int:
    for row in _store(cfg).tasks(args.limit, args.run):
        cost = row["cost_usd"]
        print(f"{row['id']:<28} {row['mode']:<5} {row['issue_ref']:<40} {row['category']}/{row['subcategory'] or '-'} "
              f"steps={row['steps']} cost={'$%.3f' % cost if cost is not None else 'n/a'}")
    return 0


def _find_trace(cfg: Config, task: str, runs_dir: Path) -> Optional[Path]:
    row = _store(cfg).task(task)
    if row and row.get("trace_dir") and Path(row["trace_dir"]).is_dir():
        return Path(row["trace_dir"])
    for candidate in [cfg.traces_dir / task, *runs_dir.glob(f"*/traces/{task}")]:
        if candidate.is_dir():
            return candidate
    return None


def cmd_trace(cfg: Config, args) -> int:
    from .tracing import load_payload, load_spans, render_tree

    directory = _find_trace(cfg, args.task, Path(args.runs_dir))
    if directory is None:
        print(f"no trace for {args.task}", file=sys.stderr)
        return 1
    spans = load_spans(directory)
    if args.json:
        _print_json(spans)
        return 0
    if args.otlp or args.otlp_endpoint:
        from .tracing import to_otlp

        body = to_otlp(spans)
        if args.otlp:
            Path(args.otlp).write_text(json.dumps(body), encoding="utf-8")
            print(f"wrote {len(spans)} spans to {args.otlp}")
        if args.otlp_endpoint:
            import httpx

            url = args.otlp_endpoint.rstrip("/")
            url = url if url.endswith("/v1/traces") else url + "/v1/traces"
            response = httpx.post(url, json=body, timeout=30)
            print(f"sent {len(spans)} spans to {url}: HTTP {response.status_code}")
            return 0 if response.is_success else 1
        return 0
    print(render_tree(spans))
    if args.payload:
        _print_json(load_payload(directory, args.payload))
    return 0


def cmd_serve(cfg: Config, args) -> int:
    from .web import serve

    serve(cfg, host=args.host, port=args.port, runs_dir=Path(args.runs_dir))
    return 0


def cmd_gc(cfg: Config, args) -> int:
    from .sandbox.docker import remove_leftovers

    print(f"removed {remove_leftovers()} leftover sandbox container(s)")
    return 0


# ---------- benchmark ----------

def _run_dir(args) -> Path:
    return Path(args.runs_dir) / args.run_id


def cmd_eval_run(cfg: Config, args) -> int:
    from .eval.runner import EvalRun, QuotaGuard
    from .eval.swebench import load_instances

    ids = args.instances or None
    instances = load_instances(instance_ids=ids, subset=None if ids else args.subset, limit=args.limit)
    quota = None
    if args.quota_guard and cfg.llm.uses_gateway:
        provider = {"anthropic": "claude", "openai": "codex", "gemini": "antigravity"}[cfg.llm.provider]
        quota = QuotaGuard(cfg.llm.base_url.rstrip("/") + "/ui/api/status", cfg.llm.api_key() or "", provider,
                           max_five_hour=args.max_five_hour, max_seven_day=args.max_seven_day)
    run = EvalRun(cfg, _run_dir(args), args.run_id, workers=args.workers, retry_infra=args.retry_infra,
                  max_total_usd=args.max_total_usd, quota=quota, store=_store(cfg))
    run.run(instances)
    return 0


def cmd_eval_grade(cfg: Config, args) -> int:
    from .eval.grade import grade_run
    from .eval.runner import write_predictions

    counts = write_predictions(_run_dir(args), args.run_id)
    print(f"predictions: {counts['gate_passed']} gate-passed patches, {counts['shadow']} shadow patches")
    grades = grade_run(_run_dir(args), args.run_id, workers=args.workers, shadow=not args.no_shadow)
    main = grades.get("main", {})
    print(f"graded {len(main)}: {sum(1 for g in main.values() if g['resolved'])} resolved")
    return 0


def cmd_eval_repro(cfg: Config, args) -> int:
    from .eval.repro_check import check_run
    from .eval.swebench import load_instances

    run_dir = _run_dir(args)
    ids = [p.stem for p in (run_dir / "instances").glob("*.json")]
    check_run(run_dir, load_instances(instance_ids=ids))
    return 0


def cmd_eval_report(cfg: Config, args) -> int:
    from .eval.report import write_report
    from .eval.swebench import load_instances

    run_dir = _run_dir(args)
    ids = [p.stem for p in (run_dir / "instances").glob("*.json")]
    meta = {}
    if not args.offline:
        meta = {i["instance_id"]: {"difficulty": i.get("difficulty")} for i in load_instances(instance_ids=ids)}
    report = write_report(run_dir, meta)
    print((run_dir / "report.md").read_text(encoding="utf-8") if args.print else
          f"{report['resolved']}/{report['n']} resolved; wrote {run_dir / 'report.md'} and report.json")
    return 0


def cmd_eval_triage_collect(cfg: Config, args) -> int:
    from .eval.triage_eval import collect
    from .github import GitHub, resolve_token

    rows = collect(GitHub(resolve_token(cfg.github), cfg.github.api_url), per_label=args.per_label, seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"{len(rows)} issues -> {out}")
    return 0


def cmd_eval_triage(cfg: Config, args) -> int:
    from .eval.triage_eval import run, score

    dataset = [json.loads(line) for line in Path(args.dataset).read_text(encoding="utf-8").splitlines() if line]
    out_dir = Path(args.runs_dir) / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = run(cfg, dataset, out_dir / "triage_results.jsonl")
    result = {"model": cfg.llm.triage_model or cfg.llm.model, "provider": cfg.llm.provider, **score(rows)}
    (out_dir / "triage_score.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    _print_json({k: v for k, v in result.items() if k != "confusion"})
    return 0


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fixgate", description="A GitHub issue agent with guardrails.")
    parser.add_argument("--config", type=Path, help="fixgate.toml (default: ./fixgate.toml or ~/.fixgate/fixgate.toml)")
    parser.add_argument("--version", action="version", version=f"fixgate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check Docker, the LLM endpoint and GitHub access").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("run", help="triage, reproduce and fix one issue, then ask for approval")
    p.add_argument("issue", help="owner/repo#123 or an issue URL")
    p.add_argument("--model")
    p.add_argument("--keep-workspace", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("triage", help="triage one issue")
    p.add_argument("issue")
    p.add_argument("--propose", action="store_true", help="request approval to apply the labels and a comment")
    p.set_defaults(fn=cmd_triage)

    p = sub.add_parser("watch", help="process new issues with a label, continuously")
    p.add_argument("repo", help="owner/repo")
    p.add_argument("--label")
    p.add_argument("--interval", type=int, default=600)
    p.add_argument("--max-per-poll", type=int, default=3)
    p.add_argument("--once", action="store_true")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("approvals", help="list approval requests")
    p.add_argument("--status", choices=["pending", "approved", "rejected", "expired", "executed"])
    p.set_defaults(fn=cmd_approvals)

    p = sub.add_parser("show", help="show an approval request with its patch")
    p.add_argument("approval_id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("approve", help="approve a request (and publish it)")
    p.add_argument("approval_id")
    p.add_argument("--by", help="reviewer name (default: git user.name)")
    p.add_argument("--note")
    p.add_argument("--no-publish", action="store_true")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("reject", help="reject a request")
    p.add_argument("approval_id")
    p.add_argument("--by")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_reject)

    p = sub.add_parser("publish", help="carry out an approved request")
    p.add_argument("approval_id")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("tasks", help="list recent tasks")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--run")
    p.set_defaults(fn=cmd_tasks)

    p = sub.add_parser("trace", help="show a task's trace")
    p.add_argument("task")
    p.add_argument("--json", action="store_true")
    p.add_argument("--payload", help="also print one payload (an id from a span's payload attribute)")
    p.add_argument("--otlp", metavar="FILE", help="write the trace as OTLP/JSON (OpenTelemetry)")
    p.add_argument("--otlp-endpoint", metavar="URL", help="send the trace to an OTLP/HTTP collector")
    p.add_argument("--runs-dir", default="runs")
    p.set_defaults(fn=cmd_trace)

    p = sub.add_parser("serve", help="web page for approvals, tasks, traces and benchmark runs")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--runs-dir", default="runs")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("sandbox-gc", help="remove sandbox containers left behind by a crash")
    p.set_defaults(fn=cmd_gc)

    ev = sub.add_parser("eval", help="benchmark on SWE-bench Verified").add_subparsers(dest="eval_command",
                                                                                         required=True)

    def with_run(p):
        p.add_argument("--run-id", required=True)
        p.add_argument("--runs-dir", default="runs")
        return p

    p = with_run(ev.add_parser("run", help="run the agent on benchmark issues"))
    p.add_argument("--subset", default="mini", choices=["mini", "all"])
    p.add_argument("--instances", nargs="*")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--retry-infra", action="store_true", help="rerun instances that ended in an infrastructure error")
    p.add_argument("--max-total-usd", type=float)
    p.add_argument("--quota-guard", action="store_true", help="pause/stop based on the gateway's plan usage")
    p.add_argument("--max-five-hour", type=float, default=0.85)
    p.add_argument("--max-seven-day", type=float, default=0.6)
    p.set_defaults(fn=cmd_eval_run)

    p = with_run(ev.add_parser("grade", help="grade with the official SWE-bench harness"))
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--no-shadow", action="store_true")
    p.set_defaults(fn=cmd_eval_grade)

    p = with_run(ev.add_parser("repro-check", help="check reproductions against the maintainers' fix"))
    p.set_defaults(fn=cmd_eval_repro)

    p = ev.add_parser("triage-collect", help="collect maintainer-labelled GitHub issues for the triage eval")
    p.add_argument("--out", default="runs/triage/dataset.jsonl")
    p.add_argument("--per-label", type=int, default=8)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(fn=cmd_eval_triage_collect)

    p = with_run(ev.add_parser("triage", help="triage accuracy against maintainers' labels"))
    p.add_argument("--dataset", default="runs/triage/dataset.jsonl")
    p.set_defaults(fn=cmd_eval_triage)

    p = with_run(ev.add_parser("report", help="write report.md and report.json"))
    p.add_argument("--offline", action="store_true", help="skip loading dataset metadata (difficulty)")
    p.add_argument("--print", action="store_true")
    p.set_defaults(fn=cmd_eval_report)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # progress lines appear at once in redirected logs
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 2
    cfg.home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    return args.fn(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
