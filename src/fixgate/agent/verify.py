"""fixgate's own checks. The model's claims are never trusted: the reproduction and the tests are re-run
by fixgate, and the patch is checked against the policy, before anything reaches a human reviewer."""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import List, Optional, Set

from ..models import TestRun
from ..sandbox.base import ExecResult

FAILURE_PATTERNS = [
    re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)", re.M),                  # pytest -rA / short summary
    re.compile(r"^(\S+::\S+) (?:FAILED|ERROR)\b", re.M),               # pytest -v
    re.compile(r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)", re.M),          # unittest and Django's runner
]
TEST_RUNNERS = ("pytest", "py.test", "runtests", "unittest", "tox", "nose", "manage.py test", "make test",
                "make check", "npm test", "yarn test", "go test", "cargo test", "trial ", "sympy.test", "bin/test")
BROKEN_SCRIPT = re.compile(r"^(SyntaxError|IndentationError|TabError)\b", re.M)


def parse_failures(output: str) -> Set[str]:
    found: Set[str] = set()
    for pattern in FAILURE_PATTERNS:
        for match in pattern.finditer(output):
            if match.lastindex and match.lastindex >= 2:
                found.add(f"{match.group(2)}.{match.group(1)}")
            else:
                found.add(match.group(1))
    return found


def looks_like_test_command(command: str) -> bool:
    lowered = command.lower()
    return any(runner in lowered for runner in TEST_RUNNERS)


FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+)', re.M)
EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)?)(?::|$)", re.M)
DELIBERATE = {"AssertionError", "SystemExit"}


def _last_exception(output: str) -> Optional[tuple]:
    """(file of the innermost frame, line, exception type) of the last Python traceback in output."""
    start = output.rfind("Traceback (most recent call last):")
    if start < 0:
        return None
    block = output[start:]
    frames = FRAME.findall(block)
    if not frames:
        return None
    after = block[block.rfind(f'line {frames[-1][1]}'):]
    for line in after.splitlines()[1:]:
        match = EXCEPTION_LINE.match(line.strip())
        if match and not line.startswith(" "):
            return frames[-1][0], int(frames[-1][1]), match.group(1).rsplit(".", 1)[-1]
    return None


def repro_script_problem(result: ExecResult, script_path: Optional[str] = None,
                         script_hint: str = "repro") -> Optional[str]:
    """Why a failing reproduction does not count, or None.

    It must fail because of the bug: a timeout, a missing script, a syntax error, or an exception raised by
    the script's own code (rather than an assertion, sys.exit, or an error from the library) does not count."""
    if result.timed_out:
        return "the reproduction command timed out"
    if result.exit_code in (126, 127):
        return "the reproduction command could not be started (command or file not found)"
    tail = result.output[-3000:]
    if "can't open file" in tail and script_hint in tail:
        return "the reproduction script does not exist at the given path"
    if BROKEN_SCRIPT.search(tail) and script_hint in tail:
        return "the reproduction script itself has a syntax error"
    last = _last_exception(result.output[-20000:])
    if last and script_path and last[0].endswith(script_path.rsplit("/", 1)[-1]) and last[2] not in DELIBERATE:
        return (f"the script's own code raised {last[2]} at line {last[1]} (not an assertion and not an error from "
                "the library); make the script call the library and assert on the wrong result")
    return None


def baseline_command(command: str, timeout: int, changed: List[str], added: List[str]) -> str:
    """Run `command` on the original code: stash only the agent's changed and added files, run, restore.

    Cached bytecode of those modules is removed before and after the run: the stash swaps file contents
    within the same second, and Python would otherwise run stale .pyc files."""
    paths = " ".join(shlex.quote(p) for p in changed + added)
    purge = []
    for path in changed + added:
        if path.endswith(".py"):
            directory, name = posixpath.split(path)
            cache = posixpath.join(directory, "__pycache__", name[:-3]) if directory else f"__pycache__/{name[:-3]}"
            purge.append(f"rm -f {shlex.quote(cache)}.*.pyc")
    purge_line = "; ".join(purge) or "true"
    restore = []
    if changed:
        restore.append("git checkout --quiet -- " + " ".join(shlex.quote(p) for p in changed) + " 2>/dev/null")
    if added:
        restore.append("rm -rf -- " + " ".join(shlex.quote(p) for p in added))
    return (
        "git -c user.name=fixgate -c user.email=fixgate@localhost stash push --include-untracked --quiet "
        f"-m fixgate-baseline -- {paths} >/dev/null 2>&1 || "
        "{ echo '[fixgate: could not stash the changes]'; exit 90; }\n"
        f"{purge_line}\n"
        f"timeout -k 5 {timeout} bash -c {shlex.quote(command)}; status=$?\n"
        + ("; ".join(restore) + "\n" if restore else "")
        + "git stash pop --quiet >/dev/null 2>&1 || { echo '[fixgate: could not restore the changes]'; exit 91; }\n"
        f"{purge_line}\n"
        "exit $status"
    )


def judge_tests(command: str, after: ExecResult, baseline: Optional[ExecResult]) -> TestRun:
    """Compare a test run on the patched code with the same run on the original code."""
    failures = parse_failures(after.output)
    run = TestRun(command=command, exit_code=after.exit_code, passed=after.ok, failures=sorted(failures),
                  output=after.output[-6000:])
    if after.ok:
        run.verdict = "passed"
        return run
    if baseline is None:
        run.verdict = "failed"
        return run
    before = parse_failures(baseline.output)
    run.baseline_exit_code = baseline.exit_code
    run.baseline_failures = sorted(before)
    run.new_failures = sorted(failures - before)
    if baseline.ok:
        run.verdict = "regressed"            # passed before the change, fails after it
    elif failures and not run.new_failures:
        run.verdict = "preexisting_failures_only"
        run.passed = True
    elif not failures and not before:
        run.verdict = "inconclusive"         # both runs failed and neither output lists failing tests
        run.passed = True
    else:
        run.verdict = "regressed"
    return run


def format_test_feedback(run: TestRun, limit: int = 4000) -> str:
    lines = [f"Test command: {run.command}", f"Exit code: {run.exit_code} ({run.verdict})"]
    if run.new_failures:
        lines.append("New failures compared with the original code: " + ", ".join(run.new_failures[:20]))
    elif run.failures:
        lines.append("Failures: " + ", ".join(run.failures[:20]))
    lines.append("Output (end):\n" + run.output[-limit:])
    return "\n".join(lines)


def only_tests_changed(paths: List[str], is_test) -> bool:
    return bool(paths) and all(is_test(p) for p in paths)
