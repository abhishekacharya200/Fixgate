"""Grading with the official SWE-bench harness (swebench.harness.run_evaluation), unchanged.

The harness applies each patch in a fresh container from the instance image, applies the hidden test
patch, runs the tests and decides `resolved` (every FAIL_TO_PASS test passes and no PASS_TO_PASS test
breaks). fixgate only reads its report.json files.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict

from .swebench import DATASET


def run_harness(run_dir: Path, predictions: str, harness_run_id: str, dataset: str = DATASET, workers: int = 2,
                timeout: int = 1800) -> Path:
    """Run the harness on one predictions file; returns the folder that holds its per-instance reports."""
    run_dir = Path(run_dir).resolve()
    path = run_dir / predictions
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    grading = run_dir / "grading"
    grading.mkdir(exist_ok=True)
    if not rows:
        return grading
    ids = [row["instance_id"] for row in rows]
    command = [sys.executable, "-m", "swebench.harness.run_evaluation", "-d", dataset, "-s", "test", "-p", str(path),
               "-i", *ids, "--max_workers", str(workers), "-id", harness_run_id, "-t", str(timeout),
               "--report_dir", str(grading)]
    with open(grading / f"{harness_run_id}.log", "w", encoding="utf-8") as log:
        subprocess.run(command, cwd=grading, stdout=log, stderr=subprocess.STDOUT, check=False)
    return grading


def read_reports(run_dir: Path, harness_run_id: str, model_name: str) -> Dict[str, dict]:
    base = Path(run_dir).resolve() / "grading" / "logs" / "run_evaluation" / harness_run_id / model_name.replace("/", "__")
    grades: Dict[str, dict] = {}
    if not base.is_dir():
        return grades
    for report in base.glob("*/report.json"):
        data = json.loads(report.read_text(encoding="utf-8"))
        for instance_id, item in data.items():
            status = item.get("tests_status") or {}
            f2p = status.get("FAIL_TO_PASS") or {}
            p2p = status.get("PASS_TO_PASS") or {}
            grades[instance_id] = {
                "resolved": bool(item.get("resolved")),
                "applied": bool(item.get("patch_successfully_applied")),
                "infra_failure": bool(item.get("infra_failure")),
                "fail_to_pass_failed": len(f2p.get("failure") or []),
                "fail_to_pass_total": len(f2p.get("success") or []) + len(f2p.get("failure") or []),
                "pass_to_pass_failed": len(p2p.get("failure") or []),
                "pass_to_pass_total": len(p2p.get("success") or []) + len(p2p.get("failure") or []),
            }
    return grades


def grade_run(run_dir: Path, run_id: str, workers: int = 2, shadow: bool = True) -> Dict[str, Dict[str, dict]]:
    """Grade predictions.jsonl (and shadow.jsonl) and save grades.json."""
    out: Dict[str, Dict[str, dict]] = {}
    run_harness(run_dir, "predictions.jsonl", run_id, workers=workers)
    out["main"] = read_reports(run_dir, run_id, run_id)
    if shadow and (run_dir / "shadow.jsonl").is_file():
        run_harness(run_dir, "shadow.jsonl", f"{run_id}-shadow", workers=workers)
        out["shadow"] = read_reports(run_dir, f"{run_id}-shadow", f"{run_id}-shadow")
    (run_dir / "grades.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out
