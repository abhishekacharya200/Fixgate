"""The pipeline end to end with a scripted model and a local git repository."""

from fixgate.agent.pipeline import Pipeline
from fixgate.llm.base import LLMError, Usage
from fixgate.llm.scripted import ScriptedBackend, reply, tool_call
from fixgate.models import Issue
from fixgate.taxonomy import Category

from .conftest import PYTHON

ISSUE = Issue(repo="acme/calc", number=7, title="add() returns the wrong result",
              body="add(2, 3) returns -1 instead of 5.")

TRIAGE = tool_call("record_triage", kind="bug", actionable=True, needs_more_info=False, severity="high",
                   component="calc", summary="add subtracts instead of adding", suggested_labels=["bug"],
                   confidence=0.9, evidence="add(2, 3) returns -1")


def repro_steps(sandbox):
    script = f"{sandbox.scratch_dir}/repro.py"
    command = f"PYTHONPATH=. {PYTHON} {script}"
    return [
        tool_call("write_file", path=script, content="from calc import add\nassert add(2, 3) == 5, add(2, 3)\n"),
        tool_call("bash", command=command),
        tool_call("report_reproduction", reproduced=True, command=command, explanation="add(2, 3) is -1"),
    ]


FIX = tool_call("edit_file", path="calc/__init__.py", old_text="return a - b", new_text="return a + b")
SUBMIT = tool_call("submit_fix", summary="Use + in add().", root_cause="a typo", test_command=f"{PYTHON} -m pytest -q tests")


def make(cfg, sandbox, tracer, meter, steps):
    llm = ScriptedBackend(steps)
    pipeline = Pipeline(cfg, llm=llm, sandbox=sandbox, tracer=tracer, meter=meter, python=PYTHON,
                        sleep=lambda s: None)
    return pipeline, llm


def test_happy_path_produces_a_verified_patch(cfg, sandbox, tracer, meter):
    pipeline, llm = make(cfg, sandbox, tracer, meter, [TRIAGE, *repro_steps(sandbox), FIX, SUBMIT])
    result = pipeline.run(ISSUE, "t1")
    assert result.outcome.category == Category.PATCH_READY, result.outcome
    assert result.outcome.subcategory == "reproduced"
    assert result.gate_passed
    assert "-    return a - b" in result.patch and "+    return a + b" in result.patch
    assert "repro.py" not in result.patch  # the reproduction lives outside the repository
    assert result.reproduction.status == "verified" and result.reproduction.exit_code != 0
    assert "assert add(2, 3) == 5" in result.reproduction.script
    assert result.verification.passed and result.verification.tests.verdict == "passed"
    assert result.verification.repro_after["exit_code"] == 0
    assert result.usage["llm_calls"] == 6 and result.usage["cost_usd"] > 0
    # the issue text reaches the model wrapped as untrusted input
    first = llm.calls[1]["messages"][0].text
    assert first.startswith('<issue repo="acme/calc" number="7">')


def test_reproduction_that_passes_is_rejected(cfg, sandbox, tracer, meter):
    script = f"{sandbox.scratch_dir}/ok.py"
    steps = [TRIAGE,
             tool_call("write_file", path=script, content="print('fine')\n"),
             tool_call("report_reproduction", reproduced=True, command=f"{PYTHON} {script}", explanation="x"),
             tool_call("give_up", reason="cannot_reproduce", explanation="could not")]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t2")
    feedback = llm.calls[3]["messages"][-1].tool_results[0]
    assert feedback.is_error and "exited 0" in feedback.content
    assert result.outcome.category == Category.NO_PATCH
    assert result.outcome.subcategory == "cannot_reproduce"


def test_broken_script_does_not_count_as_reproduction(cfg, sandbox, tracer, meter):
    script = f"{sandbox.scratch_dir}/repro.py"
    steps = [TRIAGE,
             tool_call("write_file", path=script, content="def broken(:\n"),
             tool_call("report_reproduction", reproduced=True, command=f"{PYTHON} {script}", explanation="x"),
             tool_call("give_up", reason="cannot_fix", explanation="stop")]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    pipeline.run(ISSUE, "t3")
    feedback = llm.calls[3]["messages"][-1].tool_results[0].content
    assert "syntax error" in feedback


def test_submit_without_changes_fails_verification(cfg, sandbox, tracer, meter):
    cfg.agent.max_fix_rounds = 2
    steps = [TRIAGE, *repro_steps(sandbox), SUBMIT, SUBMIT]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t4")
    assert result.outcome.category == Category.VERIFICATION_FAILED
    assert result.outcome.subcategory == "empty_diff"
    assert not result.gate_passed


def test_fix_that_breaks_tests_gets_feedback_then_fails(cfg, sandbox, tracer, meter):
    cfg.agent.max_fix_rounds = 2
    bad = tool_call("edit_file", path="calc/__init__.py", old_text="return x * 2", new_text="return x * 3")
    steps = [TRIAGE, *repro_steps(sandbox), FIX, bad, SUBMIT, SUBMIT]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t5")
    assert result.outcome.category == Category.VERIFICATION_FAILED
    assert result.outcome.subcategory == "tests_regressed"
    tests = result.verification.tests
    assert tests.verdict == "regressed" and tests.baseline_exit_code == 0
    assert any("test_double" in f for f in tests.failures)


def test_preexisting_failures_do_not_block(cfg, sandbox, tracer, meter, repo):
    from .conftest import git

    (repo / "tests" / "test_broken.py").write_text("def test_already_broken():\n    assert False\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a broken test")
    steps = [TRIAGE, *repro_steps(sandbox), FIX, SUBMIT]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t6")
    assert result.outcome.category == Category.PATCH_READY, result.outcome
    assert result.verification.tests.verdict == "preexisting_failures_only"
    # the baseline run restored the agent's change
    assert "return a + b" in (repo / "calc" / "__init__.py").read_text()


def test_deleting_a_test_is_blocked(cfg, sandbox, tracer, meter):
    cfg.agent.max_fix_rounds = 1
    delete = tool_call("edit_file", path="tests/test_calc.py",
                       old_text="def test_double():\n    assert double(3) == 6\n", new_text="")
    steps = [TRIAGE, *repro_steps(sandbox), FIX, delete, SUBMIT]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t7")
    assert result.outcome.category == Category.POLICY_BLOCKED
    assert result.outcome.subcategory == "test_tampering"


def test_triage_not_actionable_skips_the_agent(cfg, sandbox, tracer, meter):
    question = tool_call("record_triage", kind="question", actionable=False, needs_more_info=False, severity="low",
                         component="docs", summary="how do I add", suggested_labels=["question"], confidence=0.8,
                         evidence="how")
    pipeline, llm = make(cfg, sandbox, tracer, meter, [question])
    result = pipeline.run(ISSUE, "t8")
    assert result.outcome.category == Category.NOT_ACTIONABLE
    assert result.outcome.subcategory == "question"
    assert len(llm.calls) == 1


def test_give_up_not_a_bug_is_not_actionable(cfg, sandbox, tracer, meter):
    steps = [TRIAGE, tool_call("give_up", reason="not_a_bug", explanation="works as documented")]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t9")
    assert result.outcome.category == Category.NOT_ACTIONABLE
    assert not result.outcome.is_failure


def test_step_budget_stops_the_task(cfg, sandbox, tracer, meter):
    meter.limits.max_steps = 3
    steps = [TRIAGE] + [tool_call("bash", command=f"echo {i}") for i in range(10)]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t10")
    assert result.outcome.category == Category.BUDGET_EXCEEDED
    assert result.outcome.subcategory == "steps"
    assert result.usage["steps"] == 3


def test_cost_budget_stops_before_the_call(cfg, sandbox, tracer, meter):
    meter.limits.max_cost_usd = 0.004
    big = Usage(input_tokens=3000, output_tokens=100)
    steps = [TRIAGE] + [tool_call("bash", command=f"echo {i}", usage=big) for i in range(10)]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t11")
    assert result.outcome.category == Category.BUDGET_EXCEEDED
    assert result.outcome.subcategory == "cost_usd"
    # the check runs before each call on an estimate, so the overshoot is at most one call
    assert 0.004 < result.usage["cost_usd"] <= 0.004 + 0.0035


def test_transient_errors_are_retried(cfg, sandbox, tracer, meter):
    waits = []
    steps = [LLMError("overloaded", "busy", status=529), LLMError("server", "oops", status=500), TRIAGE,
             tool_call("give_up", reason="not_a_bug", explanation="x")]
    llm = ScriptedBackend(steps)
    pipeline = Pipeline(cfg, llm=llm, sandbox=sandbox, tracer=tracer, meter=meter, python=PYTHON,
                        sleep=waits.append)
    result = pipeline.run(ISSUE, "t12")
    assert result.outcome.category == Category.NOT_ACTIONABLE
    assert len(waits) == 2


def test_auth_error_is_not_retried(cfg, sandbox, tracer, meter):
    llm = ScriptedBackend([LLMError("auth", "bad key", status=401)])
    pipeline = Pipeline(cfg, llm=llm, sandbox=sandbox, tracer=tracer, meter=meter, python=PYTHON, sleep=lambda s: None)
    result = pipeline.run(ISSUE, "t13")
    assert result.outcome.category == Category.LLM_ERROR
    assert result.outcome.subcategory == "auth"
    assert len(llm.calls) == 1


def test_rate_limit_waits_do_not_count_as_wall_time(cfg, sandbox, tracer, meter):
    waits = []
    limited = LLMError("rate_limit", "Your usage limit has been reached.", status=429, retry_after_s=120)
    llm = ScriptedBackend([limited, TRIAGE, tool_call("give_up", reason="not_a_bug", explanation="x")])
    pipeline = Pipeline(cfg, llm=llm, sandbox=sandbox, tracer=tracer, meter=meter, python=PYTHON,
                        sleep=waits.append)
    result = pipeline.run(ISSUE, "t14")
    assert result.outcome.category == Category.NOT_ACTIONABLE
    assert waits and waits[0] >= 120
    assert result.usage["paused_s"] >= 120


def test_repeated_identical_calls_end_as_stuck(cfg, sandbox, tracer, meter):
    same = [tool_call("bash", command="ls") for _ in range(10)]
    pipeline, llm = make(cfg, sandbox, tracer, meter, [TRIAGE, *same])
    result = pipeline.run(ISSUE, "t15")
    assert result.outcome.category == Category.STUCK_LOOP
    warned = [m for m in llm.calls[-1]["messages"] if "same call several times" in (m.text or "")]
    assert warned


def test_replies_without_tools_end_as_malformed(cfg, sandbox, tracer, meter):
    pipeline, llm = make(cfg, sandbox, tracer, meter, [TRIAGE, reply("thinking"), reply("still"), reply("hmm")])
    result = pipeline.run(ISSUE, "t16")
    assert result.outcome.category == Category.MALFORMED_OUTPUT
    assert result.outcome.subcategory == "no_tool_call"


def test_invalid_calls_are_answered_then_stop(cfg, sandbox, tracer, meter):
    cfg.agent.max_invalid_calls = 3
    bad = [tool_call("bash", cmd="ls") for _ in range(5)]  # wrong argument name
    pipeline, llm = make(cfg, sandbox, tracer, meter, [TRIAGE, *bad])
    result = pipeline.run(ISSUE, "t17")
    assert result.outcome.category == Category.MALFORMED_OUTPUT
    assert result.outcome.subcategory == "invalid_tool_calls"
    answer = llm.calls[2]["messages"][-1].tool_results[0]
    assert answer.is_error and "Invalid arguments for bash" in answer.content


def test_blocked_commands_then_policy_stop(cfg, sandbox, tracer, meter):
    cfg.agent.max_blocked_commands = 2
    steps = [TRIAGE] + [tool_call("bash", command=f"pip install thing{i}") for i in range(5)]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t18")
    assert result.outcome.category == Category.POLICY_BLOCKED
    assert result.outcome.subcategory == "blocked_command_limit"
    assert "package_install" in llm.calls[2]["messages"][-1].tool_results[0].content


def test_refusal_is_an_llm_error(cfg, sandbox, tracer, meter):
    from fixgate.llm.base import LLMResponse

    refusal = LLMResponse(text="", tool_calls=[], stop_reason="refusal", usage=Usage(10, 1), model="m")
    pipeline, llm = make(cfg, sandbox, tracer, meter, [TRIAGE, refusal])
    result = pipeline.run(ISSUE, "t19")
    assert result.outcome.category == Category.LLM_ERROR and result.outcome.subcategory == "refusal"


def test_trace_records_every_step(cfg, sandbox, tracer, meter):
    from fixgate.tracing import load_spans, render_tree

    pipeline, llm = make(cfg, sandbox, tracer, meter, [TRIAGE, *repro_steps(sandbox), FIX, SUBMIT])
    pipeline.run(ISSUE, "t20")
    spans = load_spans(tracer.dir)
    kinds = {s["kind"] for s in spans}
    assert {"task", "phase", "step", "llm", "tool", "verify"} <= kinds
    task = next(s for s in spans if s["kind"] == "task")
    assert task["attrs"]["category"] == "patch_ready"
    llm_spans = [s for s in spans if s["kind"] == "llm"]
    assert all("cost_usd" in s["attrs"] for s in llm_spans)
    assert "tool edit_file" in render_tree(spans)


def test_files_present_before_the_agent_stay_out_of_the_patch(cfg, sandbox, tracer, meter, repo):
    (repo / "build_output.txt").write_text("left over from setup\n")   # untracked, not ignored
    pipeline, llm = make(cfg, sandbox, tracer, meter, [TRIAGE, *repro_steps(sandbox), FIX, SUBMIT])
    result = pipeline.run(ISSUE, "t21")
    assert result.outcome.category == Category.PATCH_READY
    assert "build_output.txt" not in result.patch
    assert (repo / "build_output.txt").exists()


def test_scratch_files_in_the_repo_are_rejected(cfg, sandbox, tracer, meter):
    cfg.agent.max_fix_rounds = 1
    stray = tool_call("write_file", path="reproduce_bug.py", content="print(1)\n")
    pipeline, llm = make(cfg, sandbox, tracer, meter, [TRIAGE, *repro_steps(sandbox), FIX, stray, SUBMIT])
    result = pipeline.run(ISSUE, "t22")
    assert result.outcome.category == Category.VERIFICATION_FAILED
    assert result.outcome.subcategory == "stray_files"


def test_weakened_reproduction_script_is_restored(cfg, sandbox, tracer, meter):
    """The agent verifies a failing repro, then rewrites it to pass without fixing anything."""
    cfg.agent.max_fix_rounds = 1
    cheat = tool_call("write_file", path=f"{sandbox.scratch_dir}/repro.py", content="print('all good')\n")
    steps = [TRIAGE, *repro_steps(sandbox), cheat,
             tool_call("edit_file", path="calc/__init__.py", old_text='"""Return the sum of a and b."""',
                       new_text='"""Return the sum of a and b (see issue 7)."""'), SUBMIT]
    pipeline, llm = make(cfg, sandbox, tracer, meter, steps)
    result = pipeline.run(ISSUE, "t23")
    assert result.outcome.category == Category.VERIFICATION_FAILED
    assert result.outcome.subcategory == "repro_still_fails"
    assert any("changed after it was verified" in w for w in result.verification.warnings)
