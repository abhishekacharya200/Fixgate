"""The failure taxonomy: every task ends in exactly one outcome, and every failure has one category.

Categories are mutually exclusive and assigned by code, never by the model. Each one names who or what
the failure is attributed to (blame), so a report can separate model capability from guardrail stops,
infrastructure problems and human decisions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class Blame(str, Enum):
    NONE = "none"            # not a failure
    AGENT = "agent"          # the model did not solve the task
    GUARDRAIL = "guardrail"  # a limit or policy stopped the task on purpose
    INFRA = "infra"          # the LLM API, the sandbox, GitHub or fixgate itself failed
    HUMAN = "human"          # a reviewer said no, or did not answer in time


class Category(str, Enum):
    # outcomes that are not failures
    RESOLVED = "resolved"                      # eval: the hidden tests pass
    PATCH_READY = "patch_ready"                # a verified patch exists (before grading / approval)
    AWAITING_APPROVAL = "awaiting_approval"    # live: waiting for a human
    PR_OPENED = "pr_opened"                    # live: approved and published as a draft PR
    NOT_ACTIONABLE = "not_actionable"          # triage or the agent decided no code change is needed

    # the model did not solve the task
    TRIAGE_REJECTED = "triage_rejected"        # eval: a real, fixable issue was triaged as not actionable
    NO_PATCH = "no_patch"                      # the agent gave up or ended without submitting a fix
    VERIFICATION_FAILED = "verification_failed"  # every submitted fix failed fixgate's own checks
    WRONG_FIX = "wrong_fix"                    # eval: passed fixgate's checks, but the hidden tests fail
    STUCK_LOOP = "stuck_loop"                  # repeated the same action without progress
    MALFORMED_OUTPUT = "malformed_output"      # kept producing invalid tool calls or no tool calls

    # guardrails stopped the task
    BUDGET_EXCEEDED = "budget_exceeded"        # cost, tokens, steps or wall-clock limit reached
    POLICY_BLOCKED = "policy_blocked"          # the patch or the agent's commands broke a policy rule

    # infrastructure
    LLM_ERROR = "llm_error"                    # the model API failed after retries, or refused
    SANDBOX_ERROR = "sandbox_error"            # Docker or the environment failed
    GITHUB_ERROR = "github_error"              # GitHub API or git push failed
    INTERNAL_ERROR = "internal_error"          # a bug in fixgate

    # human decisions
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"


BLAME = {
    Category.RESOLVED: Blame.NONE,
    Category.PATCH_READY: Blame.NONE,
    Category.AWAITING_APPROVAL: Blame.NONE,
    Category.PR_OPENED: Blame.NONE,
    Category.NOT_ACTIONABLE: Blame.NONE,
    Category.TRIAGE_REJECTED: Blame.AGENT,
    Category.NO_PATCH: Blame.AGENT,
    Category.VERIFICATION_FAILED: Blame.AGENT,
    Category.WRONG_FIX: Blame.AGENT,
    Category.STUCK_LOOP: Blame.AGENT,
    Category.MALFORMED_OUTPUT: Blame.AGENT,
    Category.BUDGET_EXCEEDED: Blame.GUARDRAIL,
    Category.POLICY_BLOCKED: Blame.GUARDRAIL,
    Category.LLM_ERROR: Blame.INFRA,
    Category.SANDBOX_ERROR: Blame.INFRA,
    Category.GITHUB_ERROR: Blame.INFRA,
    Category.INTERNAL_ERROR: Blame.INFRA,
    Category.APPROVAL_REJECTED: Blame.HUMAN,
    Category.APPROVAL_EXPIRED: Blame.HUMAN,
}

DESCRIPTIONS = {
    Category.RESOLVED: "The patch makes the benchmark's hidden tests pass.",
    Category.PATCH_READY: "A patch passed fixgate's own checks.",
    Category.AWAITING_APPROVAL: "A verified patch is waiting for a human decision.",
    Category.PR_OPENED: "A human approved the patch and fixgate opened a draft PR.",
    Category.NOT_ACTIONABLE: "Triage or the agent decided the issue needs no code change.",
    Category.TRIAGE_REJECTED: "Triage called a fixable issue not actionable (benchmark issues are all fixable).",
    Category.NO_PATCH: "The agent gave up or stopped without submitting a fix.",
    Category.VERIFICATION_FAILED: "Every submitted fix failed fixgate's checks (reproduction still fails, "
                                  "tests regress, or the diff is empty).",
    Category.WRONG_FIX: "The patch passed fixgate's checks but the benchmark's hidden tests fail.",
    Category.STUCK_LOOP: "The agent repeated the same action without progress.",
    Category.MALFORMED_OUTPUT: "The model kept sending invalid tool calls or no tool call.",
    Category.BUDGET_EXCEEDED: "A cost, token, step or time limit stopped the task.",
    Category.POLICY_BLOCKED: "The patch or the agent's commands broke a policy rule.",
    Category.LLM_ERROR: "The model API failed after retries, or refused the request.",
    Category.SANDBOX_ERROR: "Docker or the task environment failed.",
    Category.GITHUB_ERROR: "The GitHub API or git push failed.",
    Category.INTERNAL_ERROR: "An unexpected error in fixgate.",
    Category.APPROVAL_REJECTED: "A reviewer rejected the patch.",
    Category.APPROVAL_EXPIRED: "Nobody decided before the approval request expired.",
}

SUCCESS = {Category.RESOLVED, Category.PR_OPENED}
NOT_FAILURE = {Category.RESOLVED, Category.PATCH_READY, Category.AWAITING_APPROVAL, Category.PR_OPENED,
               Category.NOT_ACTIONABLE}


@dataclass
class Outcome:
    """How a task ended. phase is where it ended: triage, reproduce, fix, verify, approval, publish, grade."""

    category: Category
    subcategory: str = ""
    phase: str = ""
    message: str = ""
    span_id: Optional[str] = None
    details: dict = field(default_factory=dict)

    @property
    def blame(self) -> Blame:
        return BLAME[self.category]

    @property
    def is_failure(self) -> bool:
        return self.category not in NOT_FAILURE

    def to_json(self) -> dict:
        data = asdict(self)
        data["category"] = self.category.value
        data["blame"] = self.blame.value
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Outcome":
        return cls(category=Category(data["category"]), subcategory=data.get("subcategory", ""),
                   phase=data.get("phase", ""), message=data.get("message", ""), span_id=data.get("span_id"),
                   details=data.get("details") or {})


class TaskStop(Exception):
    """Raised anywhere inside a task to end it with a specific outcome."""

    def __init__(self, outcome: Outcome):
        super().__init__(f"{outcome.category.value}/{outcome.subcategory}: {outcome.message}")
        self.outcome = outcome
