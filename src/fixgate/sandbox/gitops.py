"""Git operations inside the sandbox: the base commit and the agent's diff.

The diff is taken against the commit recorded when the task started, through a throwaway index, so it
includes new files and never touches the repository's own index or HEAD.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, List

from .base import SandboxError

EXCLUDES = [":(exclude)*.orig", ":(exclude)*.rej", ":(exclude)**/__pycache__/**", ":(exclude)*.pyc",
            ":(exclude).pytest_cache/**", ":(exclude)**/.pytest_cache/**"]


def head_commit(sandbox) -> str:
    result = sandbox.exec("git rev-parse HEAD", timeout=60)
    sha = result.output.strip().splitlines()[-1] if result.output.strip() else ""
    if not result.ok or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SandboxError("git", f"cannot read HEAD in {sandbox.workdir}: {result.output[-300:]}")
    return sha


def untracked_files(sandbox) -> List[str]:
    """Untracked, not ignored files (recorded at the start so they never end up in the patch)."""
    result = sandbox.exec("git ls-files --others --exclude-standard", timeout=120)
    if not result.ok:
        raise SandboxError("git", f"git ls-files failed: {result.output[-300:]}")
    return [line for line in result.output.splitlines() if line.strip()]


def diff_since(sandbox, base: str, exclude: Iterable[str] = ()) -> str:
    """Tracked and untracked changes (respecting .gitignore) compared with `base`.

    The exclusions go to `git diff` only: naming an ignored path in `git add` makes it fail."""
    specs = list(EXCLUDES) + [f":(exclude,literal){path}" for path in exclude]
    pathspec = " ".join(shlex.quote(p) for p in specs)
    index = shlex.quote(f"{sandbox.scratch_dir}/git-index")
    script = (f"export GIT_INDEX_FILE={index}; rm -f {index}; "
              f"git read-tree {base} && git add -A -- . >/dev/null 2>&1 && "
              f"git diff --cached --binary --no-color --no-ext-diff --no-renames {base} -- . {pathspec}; "
              f"status=$?; rm -f {index}; exit $status")
    result = sandbox.exec(script, timeout=180)
    if not result.ok:
        raise SandboxError("git", f"git diff failed (exit {result.exit_code}): {result.output[-500:]}")
    if result.truncated:
        raise SandboxError("git", "the diff is larger than the capture limit")
    text = result.output
    return text if text.endswith("\n") or not text else text + "\n"


@dataclass
class FileChange:
    path: str
    added: int = 0
    removed: int = 0
    new_file: bool = False
    deleted: bool = False
    binary: bool = False
    removed_lines: List[str] = field(default_factory=list)
    added_lines: List[str] = field(default_factory=list)


def parse_diff(diff: str) -> List[FileChange]:
    files: List[FileChange] = []
    current = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = FileChange(path=match.group(2) if match else line[11:])
            files.append(current)
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current.new_file = True
        elif line.startswith("deleted file mode"):
            current.deleted = True
        elif line.startswith("GIT binary patch") or line.startswith("Binary files "):
            current.binary = True
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        elif line.startswith("+"):
            current.added += 1
            current.added_lines.append(line[1:])
        elif line.startswith("-"):
            current.removed += 1
            current.removed_lines.append(line[1:])
    return files
