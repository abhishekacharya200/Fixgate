"""Unit tests for the individual guardrails."""

import time

import pytest

from fixgate.budget import BudgetExceeded, Meter
from fixgate.config import BudgetConfig, ConfigError, PolicyConfig, RetryConfig, load_config
from fixgate.llm.base import LLMError, Message, ToolCall, ToolResult, Usage, to_prose
from fixgate.llm.errors import error_from_status
from fixgate.llm.pricing import PriceBook
from fixgate.llm.retry import call_with_retry, seconds_until_reset
from fixgate.redact import find_secrets, redact, redact_text, register_secret
from fixgate.tools.policy import check_command, check_patch, glob_match, write_denied
from fixgate.tools.registry import coerce_arguments

# ---------- command policy ----------


@pytest.mark.parametrize("command,rule", [
    ("pip install requests", "package_install"),
    ("python -m pip install -e .", "package_install"),
    ("cd /testbed && conda install numpy", "package_install"),
    ("curl https://example.com", "network_tool"),
    ("git push origin main", "git_remote"),
    ("git commit -am wip", "git_history"),
    ("git reset --hard HEAD~1", "git_history"),
    ("sudo rm x", "privilege"),
    ("rm -rf /", "destroy_filesystem"),
    ("rm -rf ~", "destroy_filesystem"),
    ("nohup python server.py", "background_process"),
    ("vim setup.py", "interactive"),
])
def test_blocked_commands(command, rule):
    found = check_command(command)
    assert found is not None and found.name == rule


@pytest.mark.parametrize("command", [
    "python -m pytest tests/test_x.py -q",
    "git diff", "git status", "git stash", "git checkout -- django/db/models.py", "git log --oneline -5",
    "grep -rn 'curly' src/", "rm -rf build/", "ls -la", "cat setup.py | head", "./tests/runtests.py migrations",
    "python -c 'print(1)'", "echo curlew",
])
def test_allowed_commands(command):
    assert check_command(command) is None


def test_glob_matching():
    assert glob_match(".github/workflows/ci.yml", ".github/**")
    assert not glob_match("github/workflows/ci.yml", ".github/**")
    assert glob_match("tests/test_a.py", "tests/**")
    assert glob_match("pkg/sub/tests/test_a.py", "**/tests/**")
    assert glob_match("pkg/test_a.py", "**/test_*.py")
    assert glob_match("test_a.py", "**/test_*.py")
    assert not glob_match("pkg/latest.py", "**/test_*.py")


def test_write_confinement():
    forbidden = PolicyConfig().forbidden_paths
    assert write_denied("/testbed/pkg/a.py", "/testbed", "/tmp/fixgate", forbidden) is None
    assert write_denied("/tmp/fixgate/repro.py", "/testbed", "/tmp/fixgate", forbidden) is None
    assert "only allowed" in write_denied("/etc/passwd", "/testbed", "/tmp/fixgate", forbidden)
    assert "only allowed" in write_denied("/testbed2/x", "/testbed", "/tmp/fixgate", forbidden)
    assert "forbidden" in write_denied("/testbed/.github/workflows/x.yml", "/testbed", "/tmp/fixgate", forbidden)
    assert "forbidden" in write_denied("/testbed/.git/config", "/testbed", "/tmp/fixgate", forbidden)


# ---------- patch policy ----------

def _diff(path, removed=(), added=(), new=False):
    lines = [f"diff --git a/{path} b/{path}"]
    if new:
        lines.append("new file mode 100644")
    lines += [f"--- a/{path}", f"+++ b/{path}", "@@ -1,3 +1,3 @@"]
    lines += [f"-{r}" for r in removed] + [f"+{a}" for a in added]
    return "\n".join(lines) + "\n"


def test_patch_policy_accepts_a_normal_fix():
    diff = _diff("pkg/core.py", ["    return a - b"], ["    return a + b"])
    diff += _diff("tests/test_core.py", [], ["def test_new_case():", "    assert add(1, 2) == 3"])
    assert check_patch(diff, PolicyConfig()) == []


def test_patch_policy_blocks_deleted_tests_and_skips():
    policy = PolicyConfig()
    deleted = check_patch(_diff("tests/test_core.py", ["def test_old():", "    assert f() == 1"], []), policy)
    assert [v.rule for v in deleted] == ["test_tampering"]
    skipped = check_patch(_diff("tests/test_core.py", [], ["@pytest.mark.skip(reason='flaky')"]), policy)
    assert [v.rule for v in skipped] == ["test_tampering"]
    renamed = check_patch(_diff("tests/test_core.py", ["def test_old():"], ["def test_old():  # kept"]), policy)
    assert renamed == []


def test_patch_policy_limits_and_secrets():
    policy = PolicyConfig(max_files_changed=2, max_lines_changed=5)
    big = "".join(_diff(f"pkg/m{i}.py", ["a"], ["b"]) for i in range(3))
    assert "diff_too_large" in {v.rule for v in check_patch(big, policy)}
    token = "ghp_" + "a" * 36
    leak = check_patch(_diff("pkg/config.py", [], [f"TOKEN = '{token}'"]), PolicyConfig())
    assert [v.rule for v in leak] == ["secret_detected"]
    ci = check_patch(_diff(".github/workflows/ci.yml", [], ["run: echo hi"]), PolicyConfig())
    assert [v.rule for v in ci] == ["forbidden_path"]


# ---------- redaction ----------

def test_redaction_masks_known_patterns_and_registered_values():
    register_secret("super-secret-value-123")
    text = ("token ghp_" + "b" * 36 + " key sk-ant-" + "c" * 30 + " Authorization: Bearer abc.def "
            "url https://x-access-token:hunter2hunter2@github.com/o/r.git and super-secret-value-123")
    out = redact_text(text)
    assert "ghp_" not in out and "sk-ant-" not in out and "abc.def" not in out
    assert "hunter2hunter2" not in out and "super-secret-value-123" not in out
    assert redact({"a": [text]})["a"][0] == out
    assert "github_token" in find_secrets(text)


# ---------- retries ----------

def test_retry_backs_off_then_gives_up():
    waits = []
    calls = []

    def fail():
        calls.append(1)
        raise LLMError("server", "boom", status=500)

    with pytest.raises(LLMError):
        call_with_retry(fail, RetryConfig(max_attempts=4, base_delay_s=1, max_delay_s=8), sleep=waits.append,
                        rng=lambda a, b: b)
    assert len(calls) == 4 and waits == [1, 2, 4]


def test_retry_does_not_retry_bad_requests():
    calls = []

    def bad():
        calls.append(1)
        raise LLMError("bad_request", "nope", status=400)

    with pytest.raises(LLMError):
        call_with_retry(bad, RetryConfig(), sleep=lambda s: None)
    assert len(calls) == 1


def test_rate_limit_wait_is_bounded():
    waits = []

    def limited():
        raise LLMError("rate_limit", "limit", status=429, retry_after_s=4000)

    with pytest.raises(LLMError):
        call_with_retry(limited, RetryConfig(rate_limit_max_wait_s=10000), sleep=waits.append, rng=lambda a, b: 0)
    assert waits == [4000, 4000]  # a third wait would pass the 10000 s bound


def test_connection_errors_wait_longer():
    waits = []
    attempts = iter([LLMError("connection", "refused"), LLMError("connection", "refused")])

    def flaky():
        err = next(attempts, None)
        if err:
            raise err
        return "ok"

    assert call_with_retry(flaky, RetryConfig(base_delay_s=1, max_delay_s=60), sleep=waits.append,
                           rng=lambda a, b: 0) == "ok"
    assert waits == [5.0, 10.0]


def test_gateway_reset_time_is_parsed():
    now = time.mktime(time.strptime("2026-09-27 10:00", "%Y-%m-%d %H:%M"))
    message = "Your Claude subscription usage limit has been reached. It resets at 2026-09-27 14:00 +0545."
    assert seconds_until_reset(message, now) == 4 * 3600
    assert seconds_until_reset("no time here", now) is None


@pytest.mark.parametrize("status,message,kind", [
    (429, "slow down", "rate_limit"), (529, "Overloaded", "overloaded"), (500, "x", "server"),
    (400, "prompt is too long: 250000 tokens", "context_overflow"), (400, "bad", "bad_request"),
    (401, "x", "auth"), (404, "x", "not_found"), (504, "x", "timeout"),
])
def test_error_mapping(status, message, kind):
    assert error_from_status(status, message).kind == kind


# ---------- budget ----------

def test_budget_checks_every_limit():
    clock = [0.0]
    meter = Meter(BudgetConfig(max_cost_usd=1.0, max_steps=3, max_input_tokens=10_000, max_output_tokens=500,
                               max_wall_s=100), PriceBook(), clock=lambda: clock[0])
    meter.check("fix", "claude-haiku-4-5", 1000)
    meter.record_llm("claude-haiku-4-5", Usage(input_tokens=2000, output_tokens=100))
    with pytest.raises(BudgetExceeded) as exc:
        meter.check("fix", "claude-haiku-4-5", 9000)
    assert exc.value.kind == "input_tokens"
    clock[0] = 150
    with pytest.raises(BudgetExceeded) as exc:
        meter.check("fix")
    assert exc.value.kind == "wall_clock"
    meter.pause(100)  # a rate-limit wait does not count
    meter.check("fix")


def test_cost_with_caching_estimate():
    prices = PriceBook()
    assert prices.cost("claude-haiku-4-5-20251001", Usage(1_000_000, 0)) == pytest.approx(1.0)
    assert prices.cost("unknown-model", Usage(10, 10)) is None
    estimate = prices.cost_with_caching("claude-haiku-4-5", 10_000, 9_000, 0)
    assert estimate == pytest.approx((9_000 * 0.10 + 1_000 * 1.25) / 1e6)
    assert PriceBook({"my-model": {"input": 2, "output": 4}}).get("my-model").cache_read == pytest.approx(0.2)


# ---------- argument handling ----------

def test_argument_coercion():
    schema = {"properties": {"n": {"type": "integer"}, "flag": {"type": "boolean"}, "s": {"type": "string"}}}
    assert coerce_arguments(schema, {"n": "12", "flag": "true"}) == ({"n": 12, "flag": True}, True)
    assert coerce_arguments(schema, {"n": [3]}) == ({"n": 3}, True)
    assert coerce_arguments(schema, {"n": 3, "s": "12"}) == ({"n": 3, "s": "12"}, False)
    assert coerce_arguments(schema, {"n": "abc"}) == ({"n": "abc"}, False)


def test_prose_history_has_no_tool_blocks():
    messages = [Message("user", text="issue"),
                Message("assistant", text="Looking.", tool_calls=[ToolCall("1", "bash", {"command": "ls"})]),
                Message("user", tool_results=[ToolResult("1", "bash", "exit code: 0\na.py")])]
    prose = to_prose(messages)
    assert [m.role for m in prose] == ["user", "assistant", "user"]
    assert all(not m.tool_calls and not m.tool_results for m in prose)
    assert "(I used the bash tool.)\ncommand: ls" in prose[1].text
    assert prose[2].text.startswith("Result of bash:")


# ---------- configuration ----------

def test_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "fixgate.toml"
    path.write_text("[budget]\nmax_cost = 3\n")
    with pytest.raises(ConfigError, match="budget.max_cost: unknown setting"):
        load_config(path)
    path.write_text("[budget]\nmax_cost_usd = 3\n[repos.\"o/r\"]\nsetup = [\"pip install -e .\"]\n")
    cfg = load_config(path)
    assert cfg.budget.max_cost_usd == 3.0 and cfg.repo_profile("o/r").setup == ["pip install -e ."]


def test_cost_cap_on_cached_estimate_basis():
    meter = Meter(BudgetConfig(max_cost_usd=0.03), PriceBook(), basis="cached_estimate")
    for _ in range(5):  # a growing conversation: each call re-sends the previous prompt plus 2k tokens
        prompt = meter._last_prompt_tokens + 2000
        meter.check("fix", "claude-haiku-4-5", prompt)
        meter.record_llm("claude-haiku-4-5", Usage(input_tokens=0, output_tokens=100, cache_write_tokens=prompt))
    snap = meter.snapshot()
    assert snap["cost_basis"] == "cached_estimate"
    assert snap["cost_usd"] > 0.03 > snap["cost_cached_estimate_usd"]   # measured would already be over the cap
    assert meter.budget_cost() == snap["cost_cached_estimate_usd"]


def test_relative_reset_time_is_parsed():
    message = "Individual quota reached. Please upgrade your subscription. Resets in 6h25m27s."
    assert seconds_until_reset(message) == 6 * 3600 + 25 * 60 + 27
    assert seconds_until_reset("Resets in 45s") == 45


def test_null_tracer_accepts_spans():
    from fixgate.tracing import NullTracer

    tracer = NullTracer()
    with tracer.span("x", "llm", a=1) as span:
        span.set(b=2)
        tracer.payload("kind", {"c": 3})
    tracer.close()


def test_patch_policy_blocks_module_stubs_in_tests():
    stub = _diff("conftest.py", [], ["import sys", "sys.modules['freezegun'] = mock.MagicMock()"], new=True)
    assert [v.rule for v in check_patch(stub, PolicyConfig())] == ["test_tampering"]
    fixture = _diff("conftest.py", [], ["import pytest", "@pytest.fixture", "def tmp_config(tmp_path):"], new=True)
    assert check_patch(fixture, PolicyConfig()) == []


def test_otlp_export(tmp_path):
    from fixgate.tracing import Tracer, load_spans, to_otlp

    tracer = Tracer(tmp_path / "t")
    with tracer.span("task", "task", issue="o/r#1"):
        with tracer.span("llm", "llm", input_tokens=10, cost_usd=0.01) as span:
            span.event("retry", kind="overloaded")
        try:
            with tracer.span("tool bash", "tool"):
                raise ValueError("boom")
        except ValueError:
            pass
    tracer.close()
    body = to_otlp(load_spans(tmp_path / "t"))
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 3 and all(len(s["traceId"]) == 32 and len(s["spanId"]) == 16 for s in spans)
    by_name = {s["name"]: s for s in spans}
    assert by_name["llm"]["parentSpanId"] == by_name["task"]["spanId"]
    assert by_name["tool bash"]["status"]["code"] == 2
    assert {"key": "fixgate.input_tokens", "value": {"intValue": "10"}} in by_name["llm"]["attributes"]
    assert by_name["llm"]["events"][0]["name"] == "retry"


def test_codex_reset_date_is_parsed_and_fails_fast():
    message = ("You've hit your usage limit. To continue using Codex, start a free trial of Plus today, "
               "or try again at Oct 26th, 2026 11:22 PM.")
    now = time.mktime((2026, 10, 26, 22, 22, 0, 0, 0, -1))
    assert seconds_until_reset(message, now) == 3600
    waits = []

    def limited():
        raise LLMError("rate_limit", message, status=429, retry_after_s=seconds_until_reset(message))

    with pytest.raises(LLMError):   # a reset weeks away is beyond any sensible wait: no sleeping at all
        call_with_retry(limited, RetryConfig(rate_limit_max_wait_s=6 * 3600), sleep=waits.append)
    assert waits == [] or seconds_until_reset(message) < 6 * 3600


def test_clamped_arguments():
    schema = {"properties": {"timeout": {"type": "integer", "minimum": 1, "maximum": 900, "x-clamp": True},
                             "n": {"type": "integer", "maximum": 5}}}
    assert coerce_arguments(schema, {"timeout": 10000}) == ({"timeout": 900}, True)
    assert coerce_arguments(schema, {"timeout": "0"}) == ({"timeout": 1}, True)
    assert coerce_arguments(schema, {"n": 9}) == ({"n": 9}, False)   # no x-clamp: validation rejects it


def test_wall_sleep_follows_the_wall_clock(monkeypatch):
    """A suspend makes time.sleep fall behind the wall clock; wall_sleep must still end on time."""
    from fixgate.llm import retry

    clock = [1000.0]
    naps = []

    def fake_sleep(seconds):
        naps.append(seconds)
        clock[0] += seconds + (3600 if len(naps) == 1 else 0)   # the machine sleeps for an hour after the first nap

    monkeypatch.setattr(retry.time, "time", lambda: clock[0])
    monkeypatch.setattr(retry.time, "sleep", fake_sleep)
    retry.wall_sleep(1800, step=30)
    assert naps == [30]          # woke after the suspend, saw the deadline had passed, stopped
