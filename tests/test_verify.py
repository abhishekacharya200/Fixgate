"""fixgate's reading of reproduction and test output."""

from fixgate.agent.verify import judge_tests, parse_failures, repro_script_problem
from fixgate.sandbox.base import ExecResult


def run(output, code=1, timed_out=False):
    return ExecResult(exit_code=code, output=output, total_bytes=len(output), truncated=False, duration_s=1.0,
                      timed_out=timed_out)


SCRIPT = "/tmp/fixgate/repro.py"


def test_assertion_failure_counts():
    out = ('Traceback (most recent call last):\n  File "/tmp/fixgate/repro.py", line 9, in <module>\n'
           '    assert actual == expected, actual\nAssertionError: -1\n')
    assert repro_script_problem(run(out), SCRIPT) is None


def test_library_error_counts():
    out = ('Traceback (most recent call last):\n  File "/tmp/fixgate/repro.py", line 5, in <module>\n'
           '    t.render(Context())\n  File "/testbed/django/template/base.py", line 170, in render\n'
           '    raise TemplateSyntaxError("bad")\ndjango.template.exceptions.TemplateSyntaxError: bad\n')
    assert repro_script_problem(run(out), SCRIPT) is None


def test_error_in_the_script_itself_does_not_count():
    out = ('Traceback (most recent call last):\n  File "/tmp/fixgate/repro.py", line 13, in <module>\n'
           "    actual = engine.from_string('x').render()\n"
           "TypeError: render() missing 1 required positional argument: 'context'\n")
    problem = repro_script_problem(run(out), SCRIPT)
    assert problem and "TypeError at line 13" in problem


def test_other_broken_scripts():
    assert "timed out" in repro_script_problem(run("", 124, timed_out=True), SCRIPT)
    assert "could not be started" in repro_script_problem(run("bash: foo: command not found", 127), SCRIPT)
    missing = "python: can't open file '/tmp/fixgate/repro.py': [Errno 2] No such file or directory"
    assert "does not exist" in repro_script_problem(run(missing, 2), SCRIPT)
    assert repro_script_problem(run("expected 5, got -1\n"), SCRIPT) is None   # sys.exit(1) without traceback


def test_failure_parsing_for_pytest_and_django():
    pytest_out = "FAILED tests/test_a.py::test_one - assert 1 == 2\nERROR tests/test_b.py::test_two\n"
    django_out = "FAIL: test_enum (migrations.test_writer.WriterTests)\nERROR: test_x (admin.tests.AdminTests)\n"
    assert parse_failures(pytest_out) == {"tests/test_a.py::test_one", "tests/test_b.py::test_two"}
    assert parse_failures(django_out) == {"migrations.test_writer.WriterTests.test_enum",
                                          "admin.tests.AdminTests.test_x"}


def test_judging_against_the_baseline():
    after = run("FAILED t.py::a\nFAILED t.py::b\n")
    same = run("FAILED t.py::a\nFAILED t.py::b\n")
    assert judge_tests("pytest", after, same).verdict == "preexisting_failures_only"
    fewer = run("FAILED t.py::a\n")
    judged = judge_tests("pytest", after, fewer)
    assert judged.verdict == "regressed" and judged.new_failures == ["t.py::b"]
    assert judge_tests("pytest", after, run("", 0)).verdict == "regressed"
    assert judge_tests("pytest", run("ok", 0), None).verdict == "passed"
    assert judge_tests("pytest", run("boom"), run("boom")).verdict == "inconclusive"
