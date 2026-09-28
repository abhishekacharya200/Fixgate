"""One issue, end to end: triage, reproduce, fix, verify.

The phases form a small state machine driven by the agent's phase tools (report_reproduction,
submit_fix, give_up). fixgate checks every claim itself: it re-runs the reproduction (it must fail
before the fix and pass after it), re-runs the agent's test command (and, if it fails, the same command
on the original code to tell new failures from old ones), and checks the diff against the policy.
Whatever happens, the task ends with one Outcome from the failure taxonomy.
"""

from __future__ import annotations

import re
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable, Optional

from ..budget import Meter
from ..config import Config
from ..llm.base import LLMBackend
from ..llm.retry import wall_sleep
from ..models import FixSubmission, Issue, Reproduction, TaskResult, TriageResult, Verification
from ..sandbox.base import SandboxError
from ..sandbox.gitops import diff_since, head_commit, parse_diff, untracked_files
from ..taxonomy import Category, Outcome, TaskStop
from ..tools.builtin import ToolContext, clip, make_tools
from ..tools.policy import check_patch, is_test_path
from ..tools.registry import Tool, ToolOutput, ToolRegistry
from ..tracing import Tracer
from .loop import AgentLoop, Continue, Finish
from .prompts import agent_first_message, agent_system
from .triage import triage_issue
from .verify import (
    baseline_command,
    format_test_feedback,
    judge_tests,
    looks_like_test_command,
    only_tests_changed,
    repro_script_problem,
)

GIVE_UP_REASONS = ["not_a_bug", "needs_more_info", "out_of_scope", "cannot_reproduce", "cannot_fix",
                   "environment_problem"]
NOT_ACTIONABLE_REASONS = {"not_a_bug", "needs_more_info", "out_of_scope"}

PHASE_TOOLS = [
    ("report_reproduction",
     "Report the result of stage 1. With reproduced=true, fixgate runs the command itself and checks that it "
     "fails (exits non-zero). With reproduced=false, explain why and move on to the fix.",
     {"type": "object", "properties": {
         "reproduced": {"type": "boolean"},
         "command": {"type": "string", "description": "The command that runs the reproduction, e.g. "
                                                      "`python /tmp/fixgate/repro.py`."},
         "explanation": {"type": "string", "description": "What the script shows, or why it could not be "
                                                          "reproduced."}},
      "required": ["reproduced", "explanation"], "additionalProperties": False}),
    ("submit_fix",
     "Submit the fix (stage 2). fixgate then re-runs the reproduction and test_command on your changes.",
     {"type": "object", "properties": {
         "summary": {"type": "string", "description": "What you changed, in one or two sentences."},
         "root_cause": {"type": "string", "description": "Why the bug happened."},
         "test_command": {"type": "string", "description": "The command that runs the existing tests closest "
                                                           "to your change."}},
      "required": ["summary", "root_cause", "test_command"], "additionalProperties": False}),
    ("give_up",
     "Stop without a fix, when the issue is not a real problem in this repository or cannot be fixed with a "
     "code change here.",
     {"type": "object", "properties": {
         "reason": {"type": "string", "enum": GIVE_UP_REASONS},
         "explanation": {"type": "string"}},
      "required": ["reason", "explanation"], "additionalProperties": False}),
]

SCRIPT_PATH = re.compile(r"(\S+\.py)\b")
STRAY_FILE = re.compile(r"^(repro|reproduce|reproduction|debug|scratch|tmp)[\w.-]*\.(py|sh|txt|log)$", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Pipeline:
    def __init__(self, cfg: Config, *, llm: LLMBackend, sandbox, tracer: Tracer, meter: Meter,
                 test_hint: str = "", environment: str = "", python: str = "python",
                 triage_llm: Optional[LLMBackend] = None, sleep: Callable[[float], None] = wall_sleep):
        self.cfg = cfg
        self.llm = llm
        self.triage_llm = triage_llm or llm
        self.sandbox = sandbox
        self.tracer = tracer
        self.meter = meter
        self.test_hint = test_hint
        self.environment = environment
        self.python = python
        self.sleep = sleep
        self.phase = "setup"
        self.base_commit = ""
        self.preexisting: list = []   # untracked files present before the agent started
        self.reproduction: Optional[Reproduction] = None
        self.fix_round = 0
        self.last_verification: Optional[Verification] = None
        self.last_fix: Optional[FixSubmission] = None
        self.loop: Optional[AgentLoop] = None
        self.tool_ctx: Optional[ToolContext] = None
        self.script_path: Optional[str] = None   # where the verified reproduction script lives

    # ---------- entry point ----------

    def run(self, issue: Issue, task_id: str) -> TaskResult:
        llm_cfg = self.cfg.llm
        result = TaskResult(task_id=task_id, issue=issue, outcome=Outcome(Category.INTERNAL_ERROR),
                            model=llm_cfg.model, provider=llm_cfg.provider, trace_dir=str(self.tracer.dir),
                            started_at=_now())
        with self.tracer.span("task", "task", issue=issue.ref, model=llm_cfg.model, provider=llm_cfg.provider) as span:
            try:
                self.phase = "setup"
                self.base_commit = head_commit(self.sandbox)
                self.preexisting = untracked_files(self.sandbox)
                result.base_commit = self.base_commit
                self.phase = "triage"
                triage = triage_issue(issue, llm=self.triage_llm, model=llm_cfg.triage_model or llm_cfg.model,
                                      meter=self.meter, tracer=self.tracer, retry=self.cfg.retry, sleep=self.sleep)
                result.triage = triage
                if not triage.proceed:
                    reason = "needs_more_info" if triage.needs_more_info else triage.kind
                    raise TaskStop(Outcome(Category.NOT_ACTIONABLE, reason, "triage",
                                           f"triage: {triage.kind}, actionable={triage.actionable}, "
                                           f"needs_more_info={triage.needs_more_info}: {triage.summary}"))
                self.phase = "reproduce"
                finish = self._run_agent(issue, triage)
                result.outcome = self._finish(finish, result)
            except TaskStop as stop:
                result.outcome = stop.outcome
                if not result.outcome.phase:
                    result.outcome.phase = self.phase
            except SandboxError as err:
                result.outcome = Outcome(Category.SANDBOX_ERROR, err.kind, self.phase, err.message[:500])
            except Exception as err:  # a bug in fixgate: record it, never crash the batch
                result.outcome = Outcome(Category.INTERNAL_ERROR, type(err).__name__, self.phase, str(err)[:500],
                                         details={"traceback": traceback.format_exc()[-4000:]})
            finally:
                result.reproduction = self.reproduction
                result.fix = self.last_fix
                result.verification = self.last_verification
                if not result.patch and self.base_commit:
                    try:
                        result.patch = diff_since(self.sandbox, self.base_commit, self.preexisting)
                    except Exception:  # the sandbox may be gone; the patch is only kept for analysis
                        pass
                result.usage = self.meter.snapshot()
                stats = dict(self.loop.stats) if self.loop else {}
                if self.loop:
                    stats.update({f"retry:{k}": v for k, v in self.loop.retries.items()})
                if self.tool_ctx:
                    stats["blocked_commands"] = self.tool_ctx.blocked_commands
                stats["fix_rounds"] = self.fix_round
                result.stats = stats
                result.finished_at = _now()
                span.set(category=result.outcome.category.value, subcategory=result.outcome.subcategory,
                         blame=result.outcome.blame.value, gate_passed=result.gate_passed,
                         cost_usd=result.usage.get("cost_usd"), steps=result.usage.get("steps"))
                if result.outcome.is_failure:
                    span.status = "error"
                    span.error = {"type": result.outcome.category.value, "message": result.outcome.message[:500]}
        return result

    # ---------- the agent ----------

    def _run_agent(self, issue: Issue, triage: TriageResult) -> Finish:
        ctx = ToolContext(sandbox=self.sandbox, policy=self.cfg.policy, agent=self.cfg.agent, python=self.python,
                          phase_of=lambda: self.phase)
        self.tool_ctx = ctx
        registry = ToolRegistry(make_tools(ctx))
        for name, description, schema in PHASE_TOOLS:
            registry.add(Tool(name, description, schema, lambda args: ToolOutput("phase tool"), terminal=True))
        system = agent_system(issue.repo, self.sandbox.workdir, self.sandbox.scratch_dir, self.test_hint,
                              self.environment)
        self.loop = AgentLoop(llm=self.llm, model=self.cfg.llm.model, system=system, registry=registry,
                              meter=self.meter, tracer=self.tracer, retry=self.cfg.retry, agent=self.cfg.agent,
                              max_tokens=self.cfg.llm.max_output_tokens, on_terminal=self._on_phase_tool,
                              phase=lambda: self.phase, sleep=self.sleep)
        with self.tracer.span("agent", "phase"):
            return self.loop.run(agent_first_message(issue, triage))

    def _on_phase_tool(self, name: str, args: dict):
        if name == "report_reproduction":
            return self._on_reproduction(args)
        if name == "submit_fix":
            return self._on_submit(args)
        if name == "give_up":
            return Finish("give_up", args)
        return Continue(f"Unknown phase tool {name}.", is_error=True)

    # ---------- stage 1: reproduction ----------

    def _on_reproduction(self, args: dict):
        if self.phase != "reproduce":
            return Continue("The reproduction is already recorded. Continue with the fix.", is_error=True)
        repro = self.reproduction or Reproduction(status="pending")
        repro.attempts += 1
        repro.explanation = args.get("explanation", "")
        self.reproduction = repro
        rounds = self.cfg.agent.max_repro_rounds
        with self.tracer.span("verify reproduction", "verify", attempt=repro.attempts) as span:
            if not args.get("reproduced"):
                repro.status = "not_reproduced"
                span.set(status=repro.status)
                if self.cfg.agent.require_reproduction:
                    raise TaskStop(Outcome(Category.NO_PATCH, "not_reproduced", "reproduce",
                                           "the agent could not reproduce the issue and the policy requires a "
                                           "reproduction", span_id=span.span_id))
                self.phase = "fix"
                return Continue("Recorded: not reproduced. Continue with stage 2 and fix the issue. The pull "
                                "request will say that the bug was not reproduced.")
            command = (args.get("command") or "").strip()
            if not command:
                return Continue("report_reproduction with reproduced=true needs the command that runs the "
                                "reproduction.", is_error=True)
            run = self.sandbox.exec(command, timeout=min(600, self.cfg.sandbox.max_exec_timeout_s))
            scripts = SCRIPT_PATH.findall(command)
            problem = ("it exited 0, so it does not show the problem" if run.ok
                       else repro_script_problem(run, scripts[0] if scripts else None))
            span.set(command=command, exit_code=run.exit_code, problem=problem)
            if problem:
                if repro.attempts < rounds:
                    return Continue(f"fixgate ran `{command}` and it does not count as a reproduction: {problem}. "
                                    f"A reproduction must exit non-zero because of the bug described in the issue. "
                                    f"Output (end):\n{clip(run.output, 3000)}", is_error=True)
                repro.status = "invalid"
                repro.command, repro.exit_code, repro.output = command, run.exit_code, run.output[-4000:]
                self.phase = "fix"
                return Continue(f"The reproduction still does not count ({problem}); {rounds} attempts are used. "
                                "Continue with stage 2 without a verified reproduction.")
            repro.status = "verified"
            repro.command, repro.exit_code, repro.output = command, run.exit_code, run.output[-4000:]
            repro.script = self._read_script(command)
            self.phase = "fix"
            return Continue(f"Reproduction verified: `{command}` exits with code {run.exit_code}. Output (end):\n"
                            f"{clip(run.output, 2000)}\n\nNow stage 2: fix the root cause in the source code.")

    def _read_script(self, command: str) -> Optional[str]:
        for candidate in SCRIPT_PATH.findall(command):
            path = candidate if candidate.startswith("/") else f"{self.sandbox.workdir}/{candidate}"
            try:
                content = self.sandbox.read_file(path, max_bytes=200_000).decode("utf-8", "replace")
            except (FileNotFoundError, OSError, SandboxError):
                continue
            self.script_path = path
            return content
        return None

    def _restore_verified_script(self, verification: Verification) -> None:
        """Re-run the reproduction that was verified, not whatever the script says now: an agent could
        otherwise weaken its own reproduction until it passes."""
        repro = self.reproduction
        if not (repro and repro.script and self.script_path):
            return
        try:
            current = self.sandbox.read_file(self.script_path, max_bytes=200_000).decode("utf-8", "replace")
        except (FileNotFoundError, OSError, SandboxError):
            current = None
        if current != repro.script:
            self.sandbox.write_file(self.script_path, repro.script.encode("utf-8"))
            verification.warnings.append("the reproduction script changed after it was verified; fixgate re-ran the "
                                         "verified version")

    # ---------- stage 2: fix and verification ----------

    def _on_submit(self, args: dict):
        if self.phase == "reproduce":
            return Continue("Call report_reproduction first (with reproduced=false if you could not reproduce "
                            "the problem).", is_error=True)
        self.fix_round += 1
        rounds = self.cfg.agent.max_fix_rounds
        self.last_fix = FixSubmission(summary=args["summary"], root_cause=args["root_cause"],
                                      test_command=args["test_command"], round=self.fix_round)
        self.phase = "verify"
        with self.tracer.span("verify fix", "verify", round=self.fix_round) as span:
            verification = self._verify(args["test_command"].strip())
            self.last_verification = verification
            span.set(passed=verification.passed, problems=verification.problems,
                     violations=[v["rule"] for v in verification.policy_violations])
        if verification.passed:
            return Finish("submit_fix", args)
        self.phase = "fix"
        if self.fix_round >= rounds:
            if verification.policy_violations:
                first = verification.policy_violations[0]
                raise TaskStop(Outcome(Category.POLICY_BLOCKED, first["rule"], "verify", first["message"],
                                       details={"violations": verification.policy_violations}))
            code = verification.problems[0].split(":", 1)[0] if verification.problems else "unknown"
            raise TaskStop(Outcome(Category.VERIFICATION_FAILED, code, "verify",
                                   f"{self.fix_round} submissions failed fixgate's checks: "
                                   + "; ".join(p[:200] for p in verification.problems)))
        feedback = [f"fixgate's checks failed (submission {self.fix_round} of {rounds}). Fix these problems and call "
                    "submit_fix again:"]
        feedback += [f"- {v['rule']}: {v['message']}" for v in verification.policy_violations]
        feedback += [f"- {p}" for p in verification.problems]
        return Continue("\n".join(feedback), is_error=True)

    def _verify(self, test_command: str) -> Verification:
        policy = self.cfg.policy
        diff = diff_since(self.sandbox, self.base_commit, self.preexisting)
        files = parse_diff(diff)
        verification = Verification(passed=False)
        verification.policy_violations = [asdict(v) for v in check_patch(diff, policy)]
        if not diff.strip():
            verification.problems.append("empty_diff: the repository has no changes")
            return verification
        paths = [f.path for f in files]
        if only_tests_changed(paths, lambda p: is_test_path(p, policy)):
            verification.problems.append("tests_only: only test files changed; the fix belongs in the library code")
        stray = [f.path for f in files if f.new_file and STRAY_FILE.match(f.path.rsplit("/", 1)[-1])]
        if stray:
            verification.problems.append("stray_files: remove scratch files from the repository (keep them in "
                                         f"{self.sandbox.scratch_dir}): " + ", ".join(stray))
        timeout = self.cfg.sandbox.max_exec_timeout_s
        repro = self.reproduction
        if repro and repro.status == "verified" and repro.command:
            self._restore_verified_script(verification)
            run = self.sandbox.exec(repro.command, timeout=min(600, timeout))
            verification.repro_after = {"exit_code": run.exit_code, "output": run.output[-3000:]}
            if not run.ok:
                verification.problems.append(f"repro_still_fails: `{repro.command}` still exits with code "
                                             f"{run.exit_code}. Output (end):\n{clip(run.output, 2500)}")
        elif repro and repro.status != "verified":
            verification.warnings.append("the bug was not reproduced, so the fix could not be checked against a "
                                         "reproduction")
        if not looks_like_test_command(test_command):
            verification.problems.append("bad_test_command: test_command must run the project's existing tests "
                                         "(for example with pytest or the project's test runner), not the "
                                         "reproduction script")
        elif not verification.problems or verification.problems[0].startswith("repro_still_fails"):
            after = self.sandbox.exec(test_command, timeout=timeout)
            baseline = None
            if not after.ok:
                changed = [f.path for f in files if not f.new_file]
                added = [f.path for f in files if f.new_file]
                baseline = self.sandbox.exec(baseline_command(test_command, timeout, changed, added),
                                             timeout=timeout + 60)
                if baseline.exit_code in (90, 91) and "[fixgate: could not" in baseline.output:
                    raise SandboxError("git", "stashing the changes for the baseline test run failed: "
                                       + baseline.output[-300:])
            tests = judge_tests(test_command, after, baseline)
            verification.tests = tests
            if tests.verdict == "inconclusive":
                verification.warnings.append("the test command fails with and without the change and its output "
                                             "lists no failing tests")
            if not tests.passed:
                verification.problems.append("tests_regressed: " + format_test_feedback(tests))
        verification.passed = not verification.problems and not verification.policy_violations
        return verification

    # ---------- how the agent finished ----------

    def _finish(self, finish: Finish, result: TaskResult) -> Outcome:
        if finish.name == "give_up":
            reason = finish.arguments.get("reason", "cannot_fix")
            explanation = finish.arguments.get("explanation", "")
            if reason in NOT_ACTIONABLE_REASONS:
                return Outcome(Category.NOT_ACTIONABLE, reason, self.phase, explanation[:500])
            return Outcome(Category.NO_PATCH, reason, self.phase, explanation[:500])
        result.patch = diff_since(self.sandbox, self.base_commit, self.preexisting)
        result.gate_passed = True
        warnings = self.last_verification.warnings if self.last_verification else []
        return Outcome(Category.PATCH_READY, "reproduced" if self.reproduction and
                       self.reproduction.status == "verified" else "not_reproduced", "verify",
                       self.last_fix.summary[:500] if self.last_fix else "", details={"warnings": warnings})
