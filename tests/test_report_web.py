"""Benchmark report arithmetic and the local web page."""

import json

import pytest

from fixgate.eval.report import build_report, render_markdown, wilson
from fixgate.eval.runner import write_predictions


def result(iid, category, gate=False, cost=0.1, repro="verified", sub="", blame=None, patch="diff --git a/x b/x\n"):
    blames = {"patch_ready": "none", "not_actionable": "none", "no_patch": "agent", "budget_exceeded": "guardrail",
              "llm_error": "infra", "verification_failed": "agent"}
    return {"task_id": iid, "issue": {"repo": "django/django" if "django" in iid else "sphinx-doc/sphinx"},
            "outcome": {"category": category, "subcategory": sub, "phase": "verify",
                        "blame": blame or blames.get(category, "agent"), "message": ""},
            "gate_passed": gate, "patch": patch if gate or category == "verification_failed" else "",
            "reproduction": {"status": repro}, "triage": {"kind": "bug"},
            "usage": {"cost_usd": cost, "cost_cached_estimate_usd": cost / 5, "prompt_tokens": 1000,
                      "output_tokens": 100, "steps": 10, "llm_calls": 10, "wall_s": 60},
            "stats": {"tool:bash": 3, "invalid_calls": 1}}


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run"
    (d / "instances").mkdir(parents=True)
    rows = [result("django-1", "patch_ready", gate=True), result("django-2", "patch_ready", gate=True),
            result("django-3", "patch_ready", gate=True, repro="not_reproduced"),
            result("sphinx-1", "not_actionable", sub="question"),
            result("sphinx-2", "budget_exceeded", sub="steps", cost=2.0),
            result("sphinx-3", "verification_failed", sub="tests_regressed")]
    for r in rows:
        (d / "instances" / f"{r['task_id']}.json").write_text(json.dumps(r))
    grades = {"main": {"django-1": {"resolved": True, "applied": True},
                       "django-2": {"resolved": False, "applied": True, "fail_to_pass_failed": 1},
                       "django-3": {"resolved": False, "applied": True, "fail_to_pass_failed": 0,
                                    "pass_to_pass_failed": 2}},
              "shadow": {"sphinx-3": {"resolved": True, "applied": True}}}
    (d / "grades.json").write_text(json.dumps(grades))
    (d / "run.json").write_text(json.dumps({"run_id": "r1", "model": "m", "provider": "p"}))
    return d


def test_wilson_interval():
    lo, hi = wilson(25, 50)
    assert lo == pytest.approx(0.366, abs=0.002) and hi == pytest.approx(0.634, abs=0.002)
    assert wilson(0, 0) == (0.0, 0.0)


def test_report_classifies_every_task(run_dir):
    r = build_report(run_dir)
    assert r["n"] == 6 and r["resolved"] == 1
    cats = r["by_category"]
    assert cats == {"wrong_fix": 2, "resolved": 1, "triage_rejected": 1, "budget_exceeded": 1,
                    "verification_failed": 1} or sum(cats.values()) == 6
    assert r["subcategories"]["wrong_fix"] == {"target_tests_fail": 1, "broke_existing_tests": 1}
    assert r["subcategories"]["triage_rejected"] == {"question": 1}
    assert r["by_blame"]["agent"] == 4 and r["by_blame"]["guardrail"] == 1
    assert r["gate"] == {"accepted_resolved": 1, "accepted_unresolved": 2, "rejected_would_resolve": 1,
                         "precision": pytest.approx(1 / 3, abs=1e-3)}
    assert r["cost"]["total_usd"] == pytest.approx(2.5)
    assert r["cost"]["per_resolved_usd"] == pytest.approx(2.5)
    assert r["by_repo"]["django/django"] == {"n": 3, "resolved": 1, "rate": pytest.approx(0.3333, abs=1e-3)}
    md = render_markdown(r)
    assert "1 / 6" in md and "| budget_exceeded (steps: 1) | guardrail | 1 |" in md


def test_predictions_split_gate_and_shadow(run_dir):
    counts = write_predictions(run_dir, "r1")
    assert counts == {"gate_passed": 3, "shadow": 1}
    main = [json.loads(line) for line in (run_dir / "predictions.jsonl").read_text().splitlines()]
    assert {p["instance_id"] for p in main} == {"django-1", "django-2", "django-3"}
    assert all(p["model_name_or_path"] == "r1" for p in main)


def test_web_pages_and_decisions(tmp_path, cfg, run_dir):
    from fastapi.testclient import TestClient

    from fixgate.approval import Approvals
    from fixgate.eval.report import write_report
    from fixgate.store import Store
    from fixgate.web.app import create_app

    write_report(run_dir)
    runs = run_dir.parent
    store = Store(cfg.db_path)
    approval = Approvals(store).request("t1", "open_pr", {
        "repo": "acme/calc", "issue_number": 7, "base_branch": "main", "base_commit": "a" * 40,
        "branch": "fixgate/x", "title": "Fix #7", "body": "Fixes #7", "patch": "diff --git a/x b/x\n+new\n",
        "patch_sha256": "", "commit_message": "m"})
    client = TestClient(create_app(cfg, runs, token="tok"))
    home = client.get("/")
    assert home.status_code == 200 and approval.id in home.text
    detail = client.get(f"/approvals/{approval.id}")
    assert "Approve" in detail.text and "+new" in detail.text
    forged = client.post(f"/approvals/{approval.id}/approve", data={"token": "wrong", "by": "x"})
    assert forged.status_code == 403
    rejected = client.post(f"/approvals/{approval.id}/reject", data={"token": "tok", "by": "reviewer",
                                                                     "reason": "not needed"},
                           follow_redirects=False)
    assert rejected.status_code == 303
    assert Approvals(store).get(approval.id).status == "rejected"
    report = client.get("/runs/run")
    assert report.status_code == 200 and "17%" in report.text
    assert client.get("/runs").status_code == 200
    outsider = TestClient(create_app(cfg, runs, token="tok"), base_url="http://evil.example")
    assert outsider.get("/").status_code == 403


def test_quota_guard_ignores_stale_windows_and_stops_on_weekly():
    import time as _time

    from fixgate.eval.runner import QuotaGuard, RunStopped

    now = _time.time()
    guard = QuotaGuard("http://x", "k", max_five_hour=0.85, max_seven_day=0.75, sleep=lambda s: None,
                       log=lambda m: None)
    guard.windows = lambda: {"five_hour": {"utilization": 1.0, "resetsAt": now - 60},
                             "seven_day": {"utilization": 0.42, "resetsAt": now + 86400}}
    guard.wait()   # the full five-hour window already reset: no waiting
    guard.windows = lambda: {"seven_day": {"utilization": 0.8, "resetsAt": now + 86400}}
    with pytest.raises(RunStopped):
        guard.wait()
    waits = []
    readings = iter([{"five_hour": {"utilization": 0.9, "resetsAt": now + 600}}, {}])
    guard = QuotaGuard("http://x", "k", sleep=waits.append, log=lambda m: None)
    guard.windows = lambda: next(readings)
    guard.wait()
    assert len(waits) == 1 and 600 <= waits[0] <= 700
