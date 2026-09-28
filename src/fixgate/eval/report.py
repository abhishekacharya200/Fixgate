"""Turn a benchmark run into numbers: success rate (with a confidence interval), cost per task, the failure
breakdown, reproduction validity, how well fixgate's own gate predicted the hidden tests, and how often
each guardrail stepped in."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from ..taxonomy import BLAME, DESCRIPTIONS, Category


def wilson(successes: int, n: int, z: float = 1.96) -> tuple:
    """95% Wilson score interval for a proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _summary(values: List[float]) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "total": round(sum(values), 4), "mean": round(statistics.mean(values), 4),
            "median": round(statistics.median(values), 4), "p90": round(_pct(values, 0.9), 4),
            "max": round(max(values), 4)}


def final_category(result: dict, grade: Optional[dict]) -> tuple:
    """(category, subcategory) after grading. Benchmark issues are all real and fixable, so 'not actionable'
    counts as a triage mistake."""
    outcome = result["outcome"]
    category, sub = outcome["category"], outcome.get("subcategory", "")
    if result.get("gate_passed"):
        if grade is None:
            return "ungraded", ""
        if grade.get("resolved"):
            return Category.RESOLVED.value, ""
        if not grade.get("applied"):
            return Category.WRONG_FIX.value, "patch_did_not_apply" if not grade.get("infra_failure") else "grading_error"
        if grade.get("fail_to_pass_failed"):
            return Category.WRONG_FIX.value, "target_tests_fail"
        return Category.WRONG_FIX.value, "broke_existing_tests"
    if category == Category.NOT_ACTIONABLE.value:
        return Category.TRIAGE_REJECTED.value, sub
    return category, sub


def repro_edited_after_verification(trace_dir: Path, script_path: Optional[str]) -> bool:
    """True if the agent wrote to its reproduction script after fixgate verified it (read from the trace)."""
    if not script_path:
        return False
    try:
        from ..tracing import load_spans

        spans = load_spans(trace_dir)
    except (OSError, ValueError):
        return False
    verified_at = None
    for span in spans:
        attrs = span.get("attrs") or {}
        if span["name"] == "verify reproduction" and attrs.get("problem") is None and attrs.get("exit_code"):
            verified_at = span["end_ns"]
            break
    if verified_at is None:
        return False
    for span in spans:
        if span["kind"] == "tool" and span["name"] in ("tool write_file", "tool edit_file") and \
                span["start_ns"] > verified_at:
            path = str((span.get("attrs") or {}).get("args", {}).get("path", ""))
            if path and (path == script_path or script_path.endswith("/" + path.lstrip("/"))):
                return True
    return False


def build_report(run_dir: Path, instances_meta: Optional[Dict[str, dict]] = None) -> dict:
    run_dir = Path(run_dir)
    results = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((run_dir / "instances").glob("*.json"))]
    grades_file = run_dir / "grades.json"
    grades = json.loads(grades_file.read_text(encoding="utf-8")) if grades_file.is_file() else {}
    main, shadow = grades.get("main", {}), grades.get("shadow", {})
    checks_file = run_dir / "repro_checks.json"
    checks = json.loads(checks_file.read_text(encoding="utf-8")) if checks_file.is_file() else {}
    meta = instances_meta or {}
    run_info = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").is_file() else {}

    rows = []
    for r in results:
        iid = r["task_id"]
        category, sub = final_category(r, main.get(iid))
        usage = r.get("usage") or {}
        rows.append({
            "instance_id": iid, "repo": r["issue"]["repo"], "difficulty": (meta.get(iid) or {}).get("difficulty"),
            "category": category, "subcategory": sub,
            "blame": BLAME[Category(category)].value if category in Category._value2member_map_ else "none",
            "agent_outcome": r["outcome"]["category"], "phase": r["outcome"].get("phase"),
            "gate_passed": bool(r.get("gate_passed")), "resolved": category == Category.RESOLVED.value,
            "shadow_resolved": (shadow.get(iid) or {}).get("resolved"),
            "repro": (r.get("reproduction") or {}).get("status") or "none",
            "repro_valid": (checks.get(iid) or {}).get("valid"),
            "triage_kind": (r.get("triage") or {}).get("kind"),
            "cost_usd": usage.get("cost_usd"), "cost_cached_estimate_usd": usage.get("cost_cached_estimate_usd"),
            "prompt_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("output_tokens"),
            "steps": usage.get("steps"), "llm_calls": usage.get("llm_calls"), "wall_s": usage.get("wall_s"),
            "usage_estimated_calls": usage.get("usage_estimated_calls", 0),
            "stats": r.get("stats") or {}, "message": r["outcome"].get("message", "")[:300],
            "repro_edited_after_verification": repro_edited_after_verification(
                run_dir / "traces" / iid, _script_path((r.get("reproduction") or {}).get("command"))),
        })

    n = len(rows)
    resolved = sum(1 for row in rows if row["resolved"])
    ungraded = sum(1 for row in rows if row["category"] == "ungraded")
    ci = wilson(resolved, n)
    costs = [row["cost_usd"] for row in rows if row["cost_usd"] is not None]
    total_cost = sum(costs)

    by_category = Counter(row["category"] for row in rows)
    by_blame = Counter(row["blame"] for row in rows)
    subcats: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        subcats[row["category"]][row["subcategory"] or "-"] += 1

    def group(key: str) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for value in sorted({str(row[key]) for row in rows}):
            members = [row for row in rows if str(row[key]) == value]
            won = sum(1 for m in members if m["resolved"])
            out[value] = {"n": len(members), "resolved": won, "rate": round(won / len(members), 4)}
        return out

    gate = Counter()
    for row in rows:
        if row["gate_passed"]:
            gate["accepted_resolved" if row["resolved"] else "accepted_unresolved"] += 1
        elif row["shadow_resolved"] is not None:
            gate["rejected_would_resolve" if row["shadow_resolved"] else "rejected_unresolved"] += 1
    accepted = gate["accepted_resolved"] + gate["accepted_unresolved"]

    repro = Counter(row["repro"] for row in rows)
    checked = [row for row in rows if row["repro_valid"] is not None]
    guard = Counter()
    for row in rows:
        for key, value in row["stats"].items():
            if isinstance(value, (int, float)) and not key.startswith("tool:"):
                guard[key] += value
    tools = Counter()
    for row in rows:
        for key, value in row["stats"].items():
            if key.startswith("tool:"):
                tools[key[5:]] += value

    report = {
        "run": run_info,
        "n": n,
        "resolved": resolved,
        "ungraded": ungraded,
        "resolve_rate": round(resolved / n, 4) if n else 0.0,
        "resolve_rate_ci95": [round(ci[0], 4), round(ci[1], 4)],
        "cost": {
            "per_task": _summary(costs),
            "total_usd": round(total_cost, 4),
            "per_resolved_usd": round(total_cost / resolved, 4) if resolved else None,
            "per_task_with_caching_estimate": _summary([row["cost_cached_estimate_usd"] for row in rows
                                                        if row["cost_cached_estimate_usd"] is not None]),
            "calls_with_estimated_usage": sum(row["usage_estimated_calls"] or 0 for row in rows),
        },
        "tokens": {"prompt": _summary([row["prompt_tokens"] for row in rows if row["prompt_tokens"] is not None]),
                   "output": _summary([row["output_tokens"] for row in rows if row["output_tokens"] is not None])},
        "steps": _summary([row["steps"] for row in rows if row["steps"] is not None]),
        "wall_s": _summary([row["wall_s"] for row in rows if row["wall_s"] is not None]),
        "by_category": dict(by_category.most_common()),
        "by_blame": dict(by_blame.most_common()),
        "subcategories": {k: dict(v.most_common()) for k, v in subcats.items()},
        "by_repo": group("repo"),
        "by_difficulty": group("difficulty"),
        "reproduction": {"status": dict(repro.most_common()), "checked": len(checked),
                         "valid": sum(1 for row in checked if row["repro_valid"]),
                         "valid_rate": round(sum(1 for row in checked if row["repro_valid"]) / len(checked), 4)
                         if checked else None,
                         "edited_after_verification": sum(1 for r in rows if r["repro_edited_after_verification"]),
                         "resolve_rate_when_verified": _rate([r for r in rows if r["repro"] == "verified"]),
                         "resolve_rate_otherwise": _rate([r for r in rows if r["repro"] != "verified"])},
        "gate": {**dict(gate), "precision": round(gate["accepted_resolved"] / accepted, 4) if accepted else None},
        "guardrails": dict(guard.most_common()),
        "tool_calls": dict(tools.most_common()),
        "rows": rows,
    }
    return report


def _script_path(command: Optional[str]) -> Optional[str]:
    import re

    found = re.findall(r"(\S+\.py)\b", command or "")
    return found[0] if found else None


def _rate(rows: List[dict]) -> Optional[dict]:
    if not rows:
        return None
    won = sum(1 for r in rows if r["resolved"])
    return {"n": len(rows), "resolved": won, "rate": round(won / len(rows), 4)}


def _money(value) -> str:
    return "n/a" if value is None else f"${value:.3f}"


def render_markdown(report: dict) -> str:
    run = report.get("run") or {}
    n = report["n"]
    lo, hi = report["resolve_rate_ci95"]
    cost = report["cost"]
    lines = [f"# fixgate benchmark run `{run.get('run_id', '?')}`", "",
             f"Model `{run.get('model', '?')}` ({run.get('provider', '?')}), {n} issues.", "",
             "## Headline", "",
             "| metric | value |", "|---|---|",
             f"| resolved (hidden tests pass) | {report['resolved']} / {n} = {report['resolve_rate']:.1%} "
             f"(95% CI {lo:.1%} to {hi:.1%}) |",
             f"| cost per task (mean / median / p90) | {_money(cost['per_task'].get('mean'))} / "
             f"{_money(cost['per_task'].get('median'))} / {_money(cost['per_task'].get('p90'))} |",
             f"| cost per resolved issue | {_money(cost['per_resolved_usd'])} |",
             f"| total cost | {_money(cost['total_usd'])} |",
             f"| cost per task with prompt caching (estimate) | "
             f"{_money(cost['per_task_with_caching_estimate'].get('mean'))} |",
             f"| steps per task (mean / p90) | {report['steps'].get('mean', 0):.1f} / {report['steps'].get('p90', 0):.0f} |",
             f"| wall time per task (median / p90) | {report['wall_s'].get('median', 0):.0f}s / "
             f"{report['wall_s'].get('p90', 0):.0f}s |",
             ""]
    estimated = cost.get("calls_with_estimated_usage") or 0
    if estimated:
        lines += [f"Token counts for {estimated} model calls were counted locally with a tokenizer because the "
                  "endpoint reported no usage for them; their costs are estimates at the model's API list price.", ""]
    if report.get("ungraded"):
        lines += [f"{report['ungraded']} instances are not graded yet.", ""]
    lines += ["## Failure breakdown", "", "| outcome | blame | count | share | meaning |", "|---|---|---|---|---|"]
    for category, count in report["by_category"].items():
        blame = BLAME[Category(category)].value if category in Category._value2member_map_ else "-"
        meaning = DESCRIPTIONS.get(Category(category), "") if category in Category._value2member_map_ else ""
        subs = report["subcategories"].get(category, {})
        detail = ", ".join(f"{k}: {v}" for k, v in subs.items() if k != "-")
        lines.append(f"| {category}{' (' + detail + ')' if detail else ''} | {blame} | {count} | {count / n:.0%} | "
                     f"{meaning} |")
    lines += ["", "By blame: " + ", ".join(f"{k} {v}" for k, v in report["by_blame"].items()), ""]
    lines += ["## By repository", "", "| repo | n | resolved | rate |", "|---|---|---|---|"]
    lines += [f"| {k} | {v['n']} | {v['resolved']} | {v['rate']:.0%} |" for k, v in report["by_repo"].items()]
    if any(k != "None" for k in report["by_difficulty"]):
        lines += ["", "## By difficulty (SWE-bench Verified annotation)", "", "| difficulty | n | resolved | rate |",
                  "|---|---|---|---|"]
        lines += [f"| {k} | {v['n']} | {v['resolved']} | {v['rate']:.0%} |" for k, v in report["by_difficulty"].items()]
    rep = report["reproduction"]
    lines += ["", "## Reproduction", "", "Status: " + ", ".join(f"{k} {v}" for k, v in rep["status"].items())]
    if rep["checked"]:
        lines.append(f"Checked against the maintainers' fix: {rep['valid']} of {rep['checked']} verified "
                     f"reproductions fail before it and pass after it ({rep['valid_rate']:.0%}).")
    if rep.get("edited_after_verification"):
        lines.append(f"In {rep['edited_after_verification']} tasks the agent edited its reproduction script after "
                     "fixgate verified it.")
    if rep.get("resolve_rate_when_verified"):
        a, b = rep["resolve_rate_when_verified"], rep.get("resolve_rate_otherwise") or {"rate": 0, "n": 0}
        lines.append(f"Resolve rate with a verified reproduction: {a['rate']:.0%} (n={a['n']}); without: "
                     f"{b['rate']:.0%} (n={b['n']}).")
    gate = report["gate"]
    lines += ["", "## fixgate's gate versus the hidden tests", "",
              "| | hidden tests pass | hidden tests fail |", "|---|---|---|",
              f"| gate accepted | {gate.get('accepted_resolved', 0)} | {gate.get('accepted_unresolved', 0)} |",
              f"| gate rejected (graded anyway) | {gate.get('rejected_would_resolve', 0)} | "
              f"{gate.get('rejected_unresolved', 0)} |", ""]
    if gate.get("precision") is not None:
        lines.append(f"Precision of the gate (accepted patches that are correct): {gate['precision']:.0%}.")
    lines += ["", "## Guardrail activity (all tasks)", ""]
    lines += [f"- {k}: {v}" for k, v in report["guardrails"].items()]
    lines += ["", "## Per issue", "", "| instance | outcome | repro | steps | cost | time |", "|---|---|---|---|---|---|"]
    for row in report["rows"]:
        label = row["category"] + (f"/{row['subcategory']}" if row["subcategory"] else "")
        lines.append(f"| {row['instance_id']} | {label} | {row['repro']} | {row['steps']} | {_money(row['cost_usd'])} | "
                     f"{row['wall_s']}s |")
    return "\n".join(lines) + "\n"


def write_report(run_dir: Path, instances_meta: Optional[Dict[str, dict]] = None) -> dict:
    report = build_report(run_dir, instances_meta)
    (Path(run_dir) / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (Path(run_dir) / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report
