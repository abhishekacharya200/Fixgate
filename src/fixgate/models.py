"""Records passed between the pipeline stages and stored in the database."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from .taxonomy import Outcome


@dataclass
class Issue:
    repo: str                      # owner/name
    title: str
    body: str
    number: Optional[int] = None
    url: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    comments: List[str] = field(default_factory=list)
    author: Optional[str] = None
    ref: str = ""                  # owner/name#123, or a benchmark instance id

    def __post_init__(self) -> None:
        if not self.ref:
            self.ref = f"{self.repo}#{self.number}" if self.number else self.repo


@dataclass
class TriageResult:
    kind: str                      # bug | feature | documentation | question | duplicate | invalid | other
    actionable: bool
    needs_more_info: bool
    severity: str                  # low | medium | high | critical
    component: str
    summary: str
    suggested_labels: List[str]
    confidence: float
    evidence: str

    @property
    def proceed(self) -> bool:
        return self.actionable and self.kind in ("bug", "feature", "documentation") and not self.needs_more_info


@dataclass
class Reproduction:
    status: str                    # verified | not_reproduced | invalid | skipped
    command: Optional[str] = None
    script: Optional[str] = None   # content of the reproduction script, kept for the PR and the eval
    exit_code: Optional[int] = None
    output: str = ""
    explanation: str = ""
    attempts: int = 0


@dataclass
class TestRun:
    command: str
    exit_code: int
    passed: bool
    failures: List[str] = field(default_factory=list)
    baseline_exit_code: Optional[int] = None
    baseline_failures: List[str] = field(default_factory=list)
    new_failures: List[str] = field(default_factory=list)
    verdict: str = ""              # passed | preexisting_failures_only | regressed | inconclusive | failed
    output: str = ""


@dataclass
class Verification:
    passed: bool
    repro_after: Optional[dict] = None     # {exit_code, output}
    tests: Optional[TestRun] = None
    policy_violations: List[dict] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class FixSubmission:
    summary: str
    root_cause: str
    test_command: str
    round: int


@dataclass
class TaskResult:
    task_id: str
    issue: Issue
    outcome: Outcome
    triage: Optional[TriageResult] = None
    reproduction: Optional[Reproduction] = None
    fix: Optional[FixSubmission] = None
    verification: Optional[Verification] = None
    patch: str = ""                # the final diff (also kept when verification failed, for analysis)
    gate_passed: bool = False      # the patch passed every fixgate check
    usage: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    base_commit: str = ""
    trace_dir: str = ""
    approval_id: Optional[str] = None
    started_at: str = ""
    finished_at: str = ""

    @property
    def patch_sha256(self) -> str:
        return hashlib.sha256(self.patch.encode("utf-8")).hexdigest() if self.patch else ""

    def to_json(self) -> dict:
        data = asdict(self)
        data["outcome"] = self.outcome.to_json()
        data["patch_sha256"] = self.patch_sha256
        return data

    @classmethod
    def from_json(cls, data: dict) -> "TaskResult":
        def build(kind, value):
            return kind(**value) if value else None

        verification = data.get("verification")
        if verification and verification.get("tests"):
            verification = {**verification, "tests": TestRun(**verification["tests"])}
        return cls(
            task_id=data["task_id"], issue=Issue(**data["issue"]), outcome=Outcome.from_json(data["outcome"]),
            triage=build(TriageResult, data.get("triage")), reproduction=build(Reproduction, data.get("reproduction")),
            fix=build(FixSubmission, data.get("fix")), verification=build(Verification, verification),
            patch=data.get("patch", ""), gate_passed=data.get("gate_passed", False), usage=data.get("usage") or {},
            stats=data.get("stats") or {}, model=data.get("model", ""), provider=data.get("provider", ""),
            base_commit=data.get("base_commit", ""), trace_dir=data.get("trace_dir", ""),
            approval_id=data.get("approval_id"), started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""))
