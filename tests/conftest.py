import subprocess
import sys
from pathlib import Path

import pytest

from fixgate.budget import Meter
from fixgate.config import Config
from fixgate.llm.pricing import PriceBook
from fixgate.sandbox.local import LocalSandbox
from fixgate.tracing import Tracer

BUGGY = '''\
def add(a, b):
    """Return the sum of a and b."""
    return a - b


def double(x):
    return x * 2
'''

TESTS = '''\
from calc import add, double


def test_add_zero():
    assert add(0, 0) == 0


def test_double():
    assert double(3) == 6
'''


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "calc").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "calc" / "__init__.py").write_text(BUGGY)
    (root / "tests" / "test_calc.py").write_text(TESTS)
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def sandbox(repo: Path, tmp_path: Path) -> LocalSandbox:
    return LocalSandbox(repo, tmp_path / "scratch", exec_timeout=60)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config()
    config.home = tmp_path / "home"
    config.llm.model = "claude-haiku-4-5"
    config.retry.base_delay_s = 0.0
    config.retry.max_delay_s = 0.0
    return config


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    t = Tracer(tmp_path / "trace")
    yield t
    t.close()


@pytest.fixture
def meter(cfg: Config) -> Meter:
    return Meter(cfg.budget, PriceBook())


PYTHON = sys.executable
