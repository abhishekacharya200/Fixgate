"""Triage accuracy on real GitHub issues, judged against the labels their maintainers gave them.

SWE-bench cannot test triage: every one of its issues is a fixable problem. This set mixes bugs, feature
requests, documentation issues and usage questions from projects whose labels say which is which. The
model sees only the title and the original body: no labels, no comments, no later discussion.
"""

from __future__ import annotations

import json
import random
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, List

from ..agent.triage import triage_issue
from ..budget import make_meter
from ..config import Config
from ..github import GitHub
from ..llm import make_backend
from ..llm.pricing import PriceBook
from ..models import Issue
from ..taxonomy import TaskStop
from ..tracing import NullTracer

# Maintainer label -> triage kind. Only issues carrying exactly one of a repository's mapped labels are used.
LABEL_MAPS: Dict[str, Dict[str, str]] = {
    "pytest-dev/pytest": {"type: bug": "bug", "type: enhancement": "feature", "type: proposal": "feature",
                          "type: docs": "documentation", "type: question": "question"},
    "psf/black": {"T: bug": "bug", "T: enhancement": "feature", "T: documentation": "documentation",
                  "T: user support": "question"},
    "python-poetry/poetry": {"kind/bug": "bug", "kind/feature": "feature", "area/docs": "documentation",
                             "kind/question": "question"},
    "pandas-dev/pandas": {"Bug": "bug", "Enhancement": "feature", "Docs": "documentation",
                          "Usage Question": "question"},
}
KINDS = ["bug", "feature", "documentation", "question"]


def collect(gh: GitHub, per_label: int = 8, seed: int = 7, log: Callable[[str], None] = print) -> List[dict]:
    """Closed issues per (repository, label), sampled at random with a fixed seed."""
    rng = random.Random(seed)
    rows: List[dict] = []
    for repo, mapping in LABEL_MAPS.items():
        for label, kind in mapping.items():
            query = f'repo:{repo} is:issue is:closed label:"{label}"'
            found = gh.request("GET", "/search/issues", params={"q": query, "per_page": 100,
                                                                "sort": "created"}).json().get("items", [])
            usable = []
            for item in found:
                names = {lab["name"] for lab in item.get("labels") or []}
                if len(names & set(mapping)) == 1 and len((item.get("body") or "").strip()) >= 40:
                    usable.append(item)
            rng.shuffle(usable)
            for item in usable[:per_label]:
                rows.append({"repo": repo, "number": item["number"], "title": item["title"],
                             "body": item.get("body") or "", "label": label, "gold": kind})
            log(f"{repo} {label!r}: {min(per_label, len(usable))} of {len(usable)} usable")
            time.sleep(2.5)  # the search API allows 30 requests a minute
    return rows


def run(cfg: Config, dataset: List[dict], out: Path, log: Callable[[str], None] = print) -> List[dict]:
    """Triage every issue once; results are appended to out (a JSON-lines file) and resumed from it."""
    done = {}
    if out.is_file():
        for line in out.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            done[(row["repo"], row["number"])] = row
    llm = make_backend(cfg.llm)
    prices = PriceBook(cfg.pricing)
    model = cfg.llm.triage_model or cfg.llm.model
    with open(out, "a", encoding="utf-8") as fh:
        for item in dataset:
            key = (item["repo"], item["number"])
            if key in done:
                continue
            issue = Issue(repo=item["repo"], number=item["number"], title=item["title"], body=item["body"])
            meter = make_meter(cfg, prices)
            try:
                result = triage_issue(issue, llm=llm, model=model, meter=meter, tracer=NullTracer(), retry=cfg.retry)
                row = {**{k: item[k] for k in ("repo", "number", "label", "gold")}, "kind": result.kind,
                       "actionable": result.actionable, "needs_more_info": result.needs_more_info,
                       "severity": result.severity, "confidence": result.confidence, "usage": meter.snapshot()}
            except TaskStop as stop:
                row = {**{k: item[k] for k in ("repo", "number", "label", "gold")}, "kind": None,
                       "error": stop.outcome.to_json(), "usage": meter.snapshot()}
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            done[key] = row
            log(f"{item['repo']}#{item['number']}: gold={item['gold']} predicted={row['kind']}")
    return list(done.values())


def score(rows: List[dict]) -> dict:
    """Accuracy over the four kinds; anything else the model says (duplicate, invalid, other) counts as wrong."""
    graded = [r for r in rows if r.get("kind") is not None]
    confusion: Dict[str, Counter] = defaultdict(Counter)
    for r in graded:
        confusion[r["gold"]][r["kind"]] += 1
    correct = sum(1 for r in graded if r["kind"] == r["gold"])
    per_kind = {}
    for kind in KINDS:
        tp = confusion[kind][kind]
        predicted = sum(confusion[g][kind] for g in confusion)
        actual = sum(confusion[kind].values())
        precision = tp / predicted if predicted else None
        recall = tp / actual if actual else None
        f1 = (2 * precision * recall / (precision + recall)) if precision and recall else 0.0
        per_kind[kind] = {"n": actual, "precision": precision, "recall": recall, "f1": round(f1, 4)}
    costs = [r["usage"].get("cost_usd") for r in rows if r.get("usage", {}).get("cost_usd") is not None]
    bugs = [r for r in graded if r["gold"] == "bug"]
    questions = [r for r in graded if r["gold"] == "question"]
    return {
        "n": len(rows), "graded": len(graded), "errors": len(rows) - len(graded),
        "accuracy": round(correct / len(graded), 4) if graded else None,
        "macro_f1": round(sum(v["f1"] for v in per_kind.values()) / len(KINDS), 4),
        "per_kind": per_kind,
        "confusion": {g: dict(c) for g, c in confusion.items()},
        # what the pipeline would do next: work on bugs, and not spend agent time on usage questions
        "bugs_sent_to_agent": round(sum(1 for r in bugs if r["actionable"] and r["kind"] in ("bug", "feature",
                                    "documentation") and not r["needs_more_info"]) / len(bugs), 4) if bugs else None,
        "questions_stopped": round(sum(1 for r in questions if not (r["actionable"] and r["kind"] in
                                   ("bug", "feature", "documentation") and not r["needs_more_info"])) / len(questions),
                                   4) if questions else None,
        "cost_per_issue_usd": round(sum(costs) / len(costs), 5) if costs else None,
    }
