"""The local web page: review and decide approval requests, inspect tasks and their traces, read
benchmark reports.

It listens on 127.0.0.1 only, refuses other Host headers, and every form carries a per-process token,
so another site in your browser cannot approve anything on your behalf.
"""

from __future__ import annotations

import html
import json
import secrets
from pathlib import Path
from typing import List, Optional

from ..approval import ApprovalError, Approvals
from ..config import Config
from ..store import Store
from ..tracing import load_spans

CSS = """
:root { --bg:#f7f7f5; --panel:#ffffff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e2dd; --accent:#2f5bd3;
  --ok:#1f7a4d; --bad:#b3261e; --warn:#8a5a00; --add:#e6f4ea; --del:#fce8e6; --code:#f1f0ec; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --panel:#1f1f1d; --text:#ecebe6; --muted:#a3a29b;
  --line:#34332f; --accent:#8ab4ff; --ok:#6fcf97; --bad:#ff8a80; --warn:#f2c46d; --add:#16321f; --del:#3b1d1a;
  --code:#262624; } }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; background:var(--bg);
  color:var(--text); }
header { padding:14px 20px; border-bottom:1px solid var(--line); display:flex; gap:18px; align-items:baseline;
  flex-wrap:wrap; background:var(--panel); }
header strong { font-size:17px; }
header a { color:var(--muted); text-decoration:none; }
main { max-width:1180px; margin:0 auto; padding:20px 16px 60px; }
h1 { font-size:22px; margin:6px 0 14px; } h2 { font-size:17px; margin:26px 0 10px; }
a { color:var(--accent); }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px 16px;
  margin:12px 0; overflow-x:auto; }
table { border-collapse:collapse; width:100%; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:13px; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.tag { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; border:1px solid var(--line); }
.ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); } .muted { color:var(--muted); }
pre, code { font:13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
pre { background:var(--code); padding:10px 12px; border-radius:6px; overflow-x:auto; margin:6px 0;
  white-space:pre-wrap; overflow-wrap:anywhere; }
pre.diff { white-space:pre; }
.diff span { display:block; white-space:pre; } .diff .a { background:var(--add); } .diff .d { background:var(--del); }
.diff .h { color:var(--muted); }
.kpis { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:10px; }
.kpi { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 14px; }
.kpi b { display:block; font-size:22px; font-variant-numeric:tabular-nums; } .kpi span { color:var(--muted); font-size:13px; }
form.decide { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:8px; }
input[type=text] { padding:7px 9px; border:1px solid var(--line); border-radius:6px; background:var(--bg);
  color:var(--text); min-width:180px; }
button { padding:7px 14px; border-radius:6px; border:1px solid var(--line); cursor:pointer; font-weight:600;
  background:var(--panel); color:var(--text); }
button.approve { background:var(--ok); color:#fff; border-color:var(--ok); }
button.reject { background:var(--bad); color:#fff; border-color:var(--bad); }
.bar { position:relative; height:14px; background:var(--code); border-radius:3px; min-width:120px; }
.bar i { position:absolute; top:2px; bottom:2px; background:var(--accent); border-radius:2px; min-width:2px; }
.bar i.error { background:var(--bad); } .bar i.llm { background:#7b61ff; } .bar i.tool { background:#2e9e6a; }
.bar i.verify { background:#d98e04; }
details summary { cursor:pointer; }
.hbar { display:flex; height:22px; border-radius:4px; overflow:hidden; border:1px solid var(--line); }
.hbar div { height:100%; }
@media (max-width: 640px) { th, td { padding:5px 6px; } .hide-sm { display:none; } }
"""

CATEGORY_CLASS = {"resolved": "ok", "pr_opened": "ok", "patch_ready": "ok", "awaiting_approval": "warn",
                  "not_actionable": "muted"}
BLAME_COLORS = {"none": "#2e9e6a", "agent": "#7b61ff", "guardrail": "#d98e04", "infra": "#b3261e", "human": "#6b6b66"}


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width, initial-scale=1'><title>{esc(title)} · fixgate</title>"
            f"<style>{CSS}</style></head><body><header><strong>fixgate</strong><a href='/'>overview</a>"
            f"<a href='/approvals'>approvals</a><a href='/tasks'>tasks</a><a href='/runs'>benchmark runs</a></header>"
            f"<main>{body}</main></body></html>")


def render_diff(patch: str, limit: int = 4000) -> str:
    lines = patch.splitlines()
    out = []
    for line in lines[:limit]:
        cls = "a" if line.startswith("+") and not line.startswith("+++") else \
            "d" if line.startswith("-") and not line.startswith("---") else \
            "h" if line.startswith(("diff ", "index ", "@@", "+++", "---")) else ""
        out.append(f"<span class='{cls}'>{esc(line) or ' '}</span>")
    more = f"<span class='h'>... {len(lines) - limit} more lines</span>" if len(lines) > limit else ""
    return f"<pre class='diff'>{''.join(out)}{more}</pre>"


def money(value) -> str:
    if value is None:
        return "n/a"
    return f"${value:.4f}" if 0 < value < 0.01 else f"${value:.3f}"


def duration(seconds) -> str:
    if seconds is None:
        return "n/a"
    return f"{seconds / 60:.1f} min" if seconds >= 120 else f"{seconds:.0f}s"


def api_cost(usage: dict) -> tuple:
    """(label, value) of the cost to show: through the gateway, the real-API estimate with prompt caching."""
    if usage.get("cost_basis") == "cached_estimate":
        return "est. API cost", usage.get("cost_cached_estimate_usd")
    return "model cost", usage.get("cost_usd")


def category_tag(category: Optional[str], sub: Optional[str] = None) -> str:
    cls = CATEGORY_CLASS.get(category or "", "bad")
    label = esc(category) + (f"/{esc(sub)}" if sub else "")
    return f"<span class='tag {cls}'>{label}</span>"


def create_app(cfg: Config, runs_dir: Path = Path("runs"), token: Optional[str] = None):
    from fastapi import FastAPI, Form, HTTPException, Request
    from fastapi.responses import HTMLResponse, RedirectResponse

    store = Store(cfg.db_path)
    approvals = Approvals.from_config(store, cfg)
    csrf = token or secrets.token_urlsafe(24)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.csrf = csrf

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        client = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "localhost", "testserver") or client not in ("127.0.0.1", "::1", "testclient"):
            return HTMLResponse("fixgate's page is only available on this computer.", status_code=403)
        return await call_next(request)

    def check_token(value: str) -> None:
        if not secrets.compare_digest(value or "", csrf):
            raise HTTPException(403, "invalid form token; reload the page")

    # ---------- overview ----------

    @app.get("/", response_class=HTMLResponse)
    def overview():
        pending = approvals.list("pending")
        tasks = store.tasks(15)
        body = ["<h1>Overview</h1>", "<div class=kpis>",
                f"<div class=kpi><b>{len(pending)}</b><span>approvals waiting</span></div>",
                f"<div class=kpi><b>{len(store.tasks(100000))}</b><span>tasks recorded</span></div>",
                f"<div class=kpi><b>{len(_runs(runs_dir))}</b><span>benchmark runs</span></div></div>"]
        body.append("<h2>Waiting for a decision</h2>" + _approval_table(pending))
        body.append("<h2>Recent tasks</h2>" + _task_table(tasks))
        return page("overview", "".join(body))

    # ---------- approvals ----------

    @app.get("/approvals", response_class=HTMLResponse)
    def approval_list():
        return page("approvals", "<h1>Approval requests</h1>" + _approval_table(approvals.list(None, 200)))

    @app.get("/approvals/{approval_id}", response_class=HTMLResponse)
    def approval_detail(approval_id: str):
        try:
            a = approvals.get(approval_id)
        except ApprovalError as err:
            raise HTTPException(404, str(err)) from err
        p = a.payload
        task = store.task(a.task_id)
        result = json.loads(task["result_json"]) if task and task.get("result_json") else {}
        body = [f"<h1>{esc(a.action.replace('_', ' '))}: {esc(p.get('repo'))}#{esc(p.get('issue_number'))}</h1>",
                f"<p>{category_tag(a.status)} requested {esc(a.created_at)}, expires {esc(a.expires_at)}. "
                f"Payload SHA-256 <code>{esc(a.payload_sha256[:16])}…</code>. "
                f"Task <a href='/tasks/{esc(a.task_id)}'>{esc(a.task_id)}</a>.</p>"]
        if a.decided_by:
            body.append(f"<p>Decided by <b>{esc(a.decided_by)}</b> at {esc(a.decided_at)}. {esc(a.note)}</p>")
        if a.status == "pending":
            body.append(
                "<div class=panel><b>Your decision</b> applies to exactly the patch and text below."
                f"<form class=decide method=post action='/approvals/{esc(a.id)}/approve'>"
                f"<input type=hidden name=token value='{esc(csrf)}'>"
                "<input type=text name=by placeholder='your name' required>"
                "<input type=text name=note placeholder='note (optional)'>"
                "<label><input type=checkbox name=publish value=1 checked> publish now</label>"
                "<button class=approve>Approve</button></form>"
                f"<form class=decide method=post action='/approvals/{esc(a.id)}/reject'>"
                f"<input type=hidden name=token value='{esc(csrf)}'>"
                "<input type=text name=by placeholder='your name' required>"
                "<input type=text name=reason placeholder='reason' required>"
                "<button class=reject>Reject</button></form></div>")
        if a.action == "open_pr":
            usage = result.get("usage") or {}
            repro = result.get("reproduction") or {}
            ver = result.get("verification") or {}
            tests = ver.get("tests") or {}
            body.append("<div class=kpis>"
                        f"<div class=kpi><b>{esc(repro.get('status', 'none'))}</b><span>reproduction</span></div>"
                        f"<div class=kpi><b>{esc(tests.get('verdict', 'n/a'))}</b><span>tests</span></div>"
                        f"<div class=kpi><b>{money(api_cost(usage)[1])}</b><span>{api_cost(usage)[0]}</span></div>"
                        f"<div class=kpi><b>{esc(usage.get('steps'))}</b><span>agent steps</span></div></div>")
            body.append(f"<h2>Pull request</h2><div class=panel><b>{esc(p['title'])}</b><br>"
                        f"<span class=muted>{esc(p['branch'])} → {esc(p['base_branch'])} "
                        f"(base {esc(p['base_commit'][:12])})</span><pre>{esc(p['body'])}</pre></div>")
            body.append("<h2>Patch</h2>" + render_diff(p["patch"]))
            if repro.get("command"):
                body.append(f"<h2>Reproduction</h2><div class=panel><code>{esc(repro['command'])}</code> exited "
                            f"{esc(repro.get('exit_code'))} before the fix."
                            f"<pre>{esc((repro.get('output') or '')[-3000:])}</pre></div>")
        else:
            body.append(f"<div class=panel>Labels: {esc(', '.join(p.get('labels') or []) or '(none)')}"
                        f"<pre>{esc(p.get('comment') or '')}</pre></div>")
        body.append("<h2>Audit log</h2>" + _audit_table(store.audit_log(a.id)))
        return page(f"approval {a.id}", "".join(body))

    @app.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, token: str = Form(...), by: str = Form(...), note: str = Form(""),
                publish: str = Form("")):
        check_token(token)
        try:
            a = approvals.approve(approval_id, by.strip(), note.strip())
        except ApprovalError as err:
            raise HTTPException(409, str(err)) from err
        if publish:
            from ..publish import PublishError
            from ..publish import publish as do_publish

            try:
                do_publish(cfg, store, a.id)
            except PublishError as err:
                store.audit(by, "publish.failed", a.id, error=str(err)[:300])
        return RedirectResponse(f"/approvals/{a.id}", status_code=303)

    @app.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str, token: str = Form(...), by: str = Form(...), reason: str = Form(...)):
        check_token(token)
        try:
            a = approvals.reject(approval_id, by.strip(), reason.strip())
        except ApprovalError as err:
            raise HTTPException(409, str(err)) from err
        return RedirectResponse(f"/approvals/{a.id}", status_code=303)

    # ---------- tasks ----------

    @app.get("/tasks", response_class=HTMLResponse)
    def task_list():
        return page("tasks", "<h1>Tasks</h1>" + _task_table(store.tasks(300)))

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(task_id: str):
        row = store.task(task_id)
        trace_dir = Path(row["trace_dir"]) if row and row.get("trace_dir") else None
        if trace_dir is None:
            found = list(runs_dir.glob(f"*/traces/{task_id}"))
            trace_dir = found[0] if found else None
        if row is None and trace_dir is None:
            raise HTTPException(404, "no such task")
        result = json.loads(row["result_json"]) if row and row.get("result_json") else {}
        outcome = result.get("outcome") or {}
        body = [f"<h1>Task {esc(task_id)}</h1>"]
        if result:
            usage = result.get("usage") or {}
            body.append(f"<p>{esc(result['issue']['ref'])}: {category_tag(outcome.get('category'), outcome.get('subcategory'))}"
                        f" <span class=muted>(blame: {esc(outcome.get('blame'))}, phase {esc(outcome.get('phase'))})"
                        f"</span><br>{esc(outcome.get('message'))}</p>")
            body.append("<div class=kpis>" + "".join(
                f"<div class=kpi><b>{esc(v)}</b><span>{esc(k)}</span></div>" for k, v in [
                    (api_cost(usage)[0], money(api_cost(usage)[1])), ("measured through the endpoint",
                                                                     money(usage.get("cost_usd"))),
                    ("steps", usage.get("steps")),
                    ("LLM calls", usage.get("llm_calls")), ("prompt tokens", usage.get("prompt_tokens")),
                    ("wall time", duration(usage.get("wall_s")))]) + "</div>")
        spans = load_spans(trace_dir) if trace_dir else []
        body.append("<h2>Trace</h2>" + _timeline(spans))
        if result.get("patch"):
            body.append("<h2>Final diff</h2>" + render_diff(result["patch"]))
        return page(f"task {task_id}", "".join(body))

    # ---------- benchmark runs ----------

    @app.get("/runs", response_class=HTMLResponse)
    def run_list():
        rows = []
        for run_dir, report in _runs(runs_dir):
            rows.append(f"<tr><td><a href='/runs/{esc(run_dir.name)}'>{esc(run_dir.name)}</a></td>"
                        f"<td>{esc((report.get('run') or {}).get('model'))}</td><td class=num>{report['n']}</td>"
                        f"<td class=num>{report['resolved']} ({report['resolve_rate']:.0%})</td>"
                        f"<td class=num>{money(report['cost']['per_task'].get('mean'))}</td></tr>")
        table = ("<table><tr><th>run</th><th>model</th><th class=num>issues</th><th class=num>resolved</th>"
                 "<th class=num>cost/task</th></tr>" + "".join(rows) + "</table>") if rows else \
            "<p class=muted>No reports yet. Run <code>fixgate eval report --run-id ...</code>.</p>"
        return page("benchmark runs", "<h1>Benchmark runs</h1><div class=panel>" + table + "</div>")

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: str):
        path = runs_dir / run_id / "report.json"
        if "/" in run_id or not path.is_file():
            raise HTTPException(404, "no report for this run")
        return page(f"run {run_id}", _report_html(run_id, json.loads(path.read_text(encoding="utf-8"))))

    return app


def _runs(runs_dir: Path) -> List[tuple]:
    out = []
    if runs_dir.is_dir():
        for report in sorted(runs_dir.glob("*/report.json")):
            try:
                out.append((report.parent, json.loads(report.read_text(encoding="utf-8"))))
            except ValueError:
                continue
    return out


def _approval_table(items) -> str:
    if not items:
        return "<p class=muted>None.</p>"
    rows = "".join(
        f"<tr><td><a href='/approvals/{esc(a.id)}'>{esc(a.id)}</a></td><td>{category_tag(a.status)}</td>"
        f"<td>{esc(a.action)}</td><td>{esc(a.payload.get('repo'))}#{esc(a.payload.get('issue_number'))}</td>"
        f"<td class=hide-sm>{esc(a.created_at)}</td></tr>" for a in items)
    return ("<div class=panel><table><tr><th>id</th><th>status</th><th>action</th><th>issue</th>"
            f"<th class=hide-sm>created</th></tr>{rows}</table></div>")


def _task_table(rows) -> str:
    if not rows:
        return "<p class=muted>No tasks yet.</p>"
    body = "".join(
        f"<tr><td><a href='/tasks/{esc(r['id'])}'>{esc(r['id'])}</a></td><td>{esc(r['issue_ref'])}</td>"
        f"<td>{category_tag(r['category'], r['subcategory'])}</td><td class=num>{esc(r['steps'])}</td>"
        f"<td class=num>{money(r['cost_usd'])}</td><td class='hide-sm muted'>{esc(r['mode'])}</td></tr>" for r in rows)
    return ("<div class=panel><table><tr><th>task</th><th>issue</th><th>outcome</th><th class=num>steps</th>"
            f"<th class=num>cost</th><th class=hide-sm>mode</th></tr>{body}</table></div>")


def _audit_table(rows) -> str:
    if not rows:
        return "<p class=muted>No entries.</p>"
    body = "".join(f"<tr><td>{esc(r['at'])}</td><td>{esc(r['actor'])}</td><td>{esc(r['action'])}</td>"
                   f"<td><code>{esc(r['details_json'])[:200]}</code></td></tr>" for r in rows)
    return f"<div class=panel><table><tr><th>at</th><th>who</th><th>action</th><th>details</th></tr>{body}</table></div>"


def _timeline(spans: List[dict]) -> str:
    if not spans:
        return "<p class=muted>No trace recorded.</p>"
    t0 = min(s["start_ns"] for s in spans)
    t1 = max(s["end_ns"] for s in spans)
    total = max(1, t1 - t0)
    depth = {}
    by_id = {s["span_id"]: s for s in spans}
    for s in spans:
        d, parent = 0, s.get("parent_id")
        while parent in by_id and d < 12:
            d += 1
            parent = by_id[parent].get("parent_id")
        depth[s["span_id"]] = d
    rows = []
    for s in spans[:1500]:
        attrs = s.get("attrs") or {}
        left = (s["start_ns"] - t0) / total * 100
        width = max(0.2, (s["end_ns"] - s["start_ns"]) / total * 100)
        cls = "error" if s.get("status") == "error" else s.get("kind", "")
        info = []
        if attrs.get("cost_usd") is not None:
            info.append(money(attrs["cost_usd"]))
        if attrs.get("input_tokens") or attrs.get("cache_write_tokens"):
            prompt = (attrs.get("input_tokens") or 0) + (attrs.get("cache_write_tokens") or 0) + \
                (attrs.get("cache_read_tokens") or 0)
            info.append(f"{prompt} in / {attrs.get('output_tokens', 0)} out")
        if attrs.get("exit_code") is not None:
            info.append(f"exit {attrs['exit_code']}")
        if s.get("error"):
            info.append(f"<span class=bad>{esc(s['error'].get('type'))}</span>")
        detail = esc(json.dumps({k: v for k, v in attrs.items() if k != "payload"}, indent=1)[:4000])
        events = esc(json.dumps(s.get("events") or [], indent=1)[:2000]) if s.get("events") else ""
        rows.append(
            f"<tr><td style='padding-left:{8 + depth[s['span_id']] * 14}px'><details><summary>{esc(s['name'])}"
            f"</summary><pre>{detail}</pre>{'<pre>' + events + '</pre>' if events else ''}</details></td>"
            f"<td class=num>{(s.get('duration_ms') or 0) / 1000:.1f}s</td><td class=hide-sm>{' · '.join(info)}</td>"
            f"<td style='width:34%'><div class=bar><i class='{esc(cls)}' style='left:{left:.2f}%;width:{width:.2f}%'>"
            f"</i></div></td></tr>")
    return ("<div class=panel><table><tr><th>span</th><th class=num>time</th><th class=hide-sm>details</th>"
            f"<th>timeline</th></tr>{''.join(rows)}</table></div>")


def _report_html(run_id: str, r: dict) -> str:
    run = r.get("run") or {}
    lo, hi = r["resolve_rate_ci95"]
    cost = r["cost"]
    body = [f"<h1>Benchmark run {esc(run_id)}</h1>",
            f"<p class=muted>{esc(run.get('model'))} via {esc(run.get('provider'))}, {r['n']} real GitHub issues "
            "(SWE-bench Verified), graded by the official harness.</p>",
            "<div class=kpis>",
            f"<div class=kpi><b>{r['resolve_rate']:.0%}</b><span>resolved ({r['resolved']}/{r['n']}), 95% CI "
            f"{lo:.0%}–{hi:.0%}</span></div>",
            f"<div class=kpi><b>{money(cost['per_task'].get('mean'))}</b><span>mean cost per task at API prices"
            f"</span></div>",
            f"<div class=kpi><b>{money(cost.get('per_resolved_usd'))}</b><span>cost per resolved issue</span></div>",
            f"<div class=kpi><b>{r['steps'].get('mean', 0):.1f}</b><span>mean agent steps</span></div>",
            f"<div class=kpi><b>{r['wall_s'].get('median', 0) / 60:.1f} min</b><span>median time per task</span>"
            f"</div>",
            "</div>"]
    blame_total = sum(r["by_blame"].values()) or 1
    bars = "".join(f"<div title='{esc(k)}: {v}' style='width:{v / blame_total * 100:.2f}%;"
                   f"background:{BLAME_COLORS.get(k, '#999')}'></div>" for k, v in r["by_blame"].items())
    legend = " · ".join(f"<span style='color:{BLAME_COLORS.get(k, '#999')}'>■</span> {esc(k)} {v}"
                        for k, v in r["by_blame"].items())
    body.append(f"<h2>Where tasks ended</h2><div class=panel><div class=hbar>{bars}</div><p>{legend}</p><table>"
                "<tr><th>outcome</th><th class=num>count</th><th class=num>share</th><th>details</th></tr>")
    for category, count in r["by_category"].items():
        subs = r["subcategories"].get(category, {})
        detail = ", ".join(f"{esc(k)} {v}" for k, v in subs.items() if k != "-")
        body.append(f"<tr><td>{category_tag(category)}</td><td class=num>{count}</td>"
                    f"<td class=num>{count / r['n']:.0%}</td><td class=muted>{detail}</td></tr>")
    body.append("</table></div>")
    gate = r["gate"]
    body.append("<h2>fixgate's gate versus the hidden tests</h2><div class=panel><table>"
                "<tr><th></th><th class=num>hidden tests pass</th><th class=num>hidden tests fail</th></tr>"
                f"<tr><td>gate accepted</td><td class=num>{gate.get('accepted_resolved', 0)}</td>"
                f"<td class=num>{gate.get('accepted_unresolved', 0)}</td></tr>"
                f"<tr><td>gate rejected</td><td class=num>{gate.get('rejected_would_resolve', 0)}</td>"
                f"<td class=num>{gate.get('rejected_unresolved', 0)}</td></tr></table></div>")
    body.append("<h2>Per issue</h2><div class=panel><table><tr><th>instance</th><th>outcome</th><th>repro</th>"
                "<th class=num>steps</th><th class=num>cost</th><th class='num hide-sm'>time</th></tr>")
    for row in r["rows"]:
        body.append(f"<tr><td><a href='/tasks/{esc(row['instance_id'])}'>{esc(row['instance_id'])}</a></td>"
                    f"<td>{category_tag(row['category'], row['subcategory'])}</td><td>{esc(row['repro'])}</td>"
                    f"<td class=num>{esc(row['steps'])}</td><td class=num>{money(row['cost_usd'])}</td>"
                    f"<td class='num hide-sm'>{(row['wall_s'] or 0) / 60:.1f} min</td></tr>")
    body.append("</table></div>")
    return "".join(body)


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8790, runs_dir: Path = Path("runs")) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost"):
        raise SystemExit("fixgate serve only listens on 127.0.0.1")
    print(f"fixgate: http://{host}:{port}/")
    uvicorn.run(create_app(cfg, runs_dir), host=host, port=port, log_level="warning")
