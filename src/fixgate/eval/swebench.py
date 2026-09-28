"""SWE-bench Verified tasks: real GitHub issues with hidden tests that decide whether a fix is right.

The agent sees only the issue text and the repository at the issue's base commit, inside the official
SWE-bench Docker image for that instance. The hidden tests (test_patch, FAIL_TO_PASS, PASS_TO_PASS)
and the maintainers' fix are never shown to it; they are used only for grading.
"""

from __future__ import annotations

from importlib import resources
from typing import Dict, List, Optional

from ..models import Issue

DATASET = "SWE-bench/SWE-bench_Verified"
ENV_SETUP = "source /opt/miniconda3/bin/activate && conda activate testbed"
WORKDIR = "/testbed"
ENVIRONMENT_NOTE = ("The conda environment 'testbed' with the project's dependencies is active in every command; "
                    "the package is installed in development mode from /testbed.")

# Repository-level test instructions, the kind a CONTRIBUTING file gives. They never name the hidden tests.
TEST_HINTS: Dict[str, str] = {
    "django/django": "Django uses its own runner: `./tests/runtests.py <app_label>` or "
                     "`./tests/runtests.py <app_label>.<test_module>.<TestClass>`, run from /testbed "
                     "(add `--parallel 1` for readable output).",
    "sphinx-doc/sphinx": "Run tests with pytest, for example `pytest tests/test_<name>.py -q` (use -k to select "
                         "tests).",
    "astropy/astropy": "Run tests with pytest, for example `pytest astropy/<subpackage>/tests/test_<name>.py -q`.",
    "sympy/sympy": "Run tests with `bin/test sympy/<module>/tests/test_<name>.py` or with pytest.",
}
DEFAULT_HINT = "Run tests with pytest, for example `pytest path/to/test_file.py -q`."


def mini_ids() -> List[str]:
    text = resources.files("fixgate.eval").joinpath("data/verified_mini.txt").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def load_instances(dataset: str = DATASET, split: str = "test", instance_ids: Optional[List[str]] = None,
                   subset: Optional[str] = None, limit: Optional[int] = None) -> List[dict]:
    from datasets import load_dataset

    rows = [dict(row) for row in load_dataset(dataset, split=split)]
    wanted = list(instance_ids or [])
    if subset == "mini":
        wanted = wanted or mini_ids()
    if wanted:
        by_id = {row["instance_id"]: row for row in rows}
        missing = [i for i in wanted if i not in by_id]
        if missing:
            raise ValueError(f"not in {dataset}: {', '.join(missing[:5])}")
        rows = [by_id[i] for i in wanted]
    return rows[:limit] if limit else rows


def issue_from_instance(instance: dict) -> Issue:
    text = instance["problem_statement"].strip()
    first = text.splitlines()[0].strip() if text else instance["instance_id"]
    title = first if len(first) <= 120 else first[:117] + "..."
    return Issue(repo=instance["repo"], title=title, body=text, ref=instance["instance_id"])


def test_hint(repo: str) -> str:
    return TEST_HINTS.get(repo, DEFAULT_HINT)
