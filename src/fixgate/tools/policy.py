"""Policy rules: which commands the agent may run, where it may write, and what a patch may contain.

The sandbox is the real security boundary (no network, no secrets, resource limits). These rules add
defence in depth and, more importantly, keep the agent honest: no editing tests to make them pass, no
environment changes a reviewer could not reproduce, no oversized or secret-bearing patches.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional

from ..config import PolicyConfig
from ..redact import find_secrets
from ..sandbox.gitops import FileChange, parse_diff


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: "re.Pattern[str]"
    reason: str


COMMAND_RULES = [
    Rule("git_remote", re.compile(r"\bgit\s+(?:-c\s+\S+\s+)*(push|pull|fetch|clone|remote|submodule)\b"),
         "There is no network, and fixgate publishes changes itself after a human approves them."),
    Rule("git_history", re.compile(r"\bgit\s+(?:-c\s+\S+\s+)*(commit|rebase|merge|cherry-pick|am|tag|"
                                   r"reset\s+--hard|checkout\s+-b|switch|branch\s+-[dDmM])\b"),
         "fixgate manages git history. Use git diff, git status, git stash or `git checkout -- <file>` only."),
    Rule("package_install", re.compile(r"\b(pip3?|python3?\s+-m\s+pip|conda|mamba|apt(-get)?|npm|yarn|pnpm|poetry|"
                                       r"uv)\s+(install|add|update|upgrade|remove|uninstall)\b"),
         "Dependencies are already installed and there is no network. A fix must work in the existing "
         "environment."),
    Rule("network_tool", re.compile(r"(^|[\s;&|(])(curl|wget|nc|ncat|netcat|ssh|scp|sftp|telnet|ftp|rsync)\s"),
         "The sandbox has no network."),
    Rule("privilege", re.compile(r"(^|[\s;&|(])(sudo|su|doas|chroot|nsenter|mount|umount|unshare)\s"),
         "Privilege changes are not allowed."),
    Rule("destroy_filesystem", re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*(/|/\*|~|\$HOME)(\s|$)"),
         "Deleting the filesystem root or home directory is not allowed."),
    Rule("fork_bomb", re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:"), "Fork bombs are not allowed."),
    Rule("background_process", re.compile(r"(^|[\s;&|(])(nohup|disown|setsid|screen|tmux)\s"),
         "Background processes are not allowed: every command must finish within its time limit."),
    Rule("interactive", re.compile(r"(^|[\s;&|(])(vi|vim|nvim|nano|emacs|less|more|top|htop|watch)(\s|$)"),
         "Interactive programs hang in the sandbox. Use read_file and edit_file instead."),
]


def check_command(command: str) -> Optional[Rule]:
    for rule in COMMAND_RULES:
        if rule.pattern.search(command):
            return rule
    return None


# ---------- paths ----------

@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> "re.Pattern[str]":
    """Glob with ** (any number of directories), * (within one directory) and ?."""
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile(out + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    return bool(_glob_regex(pattern).match(path[2:] if path.startswith("./") else path))


def matches_any(path: str, patterns: List[str]) -> bool:
    return any(glob_match(path, p) for p in patterns)


def write_denied(real_path: str, workdir: str, scratch: str, forbidden: List[str]) -> Optional[str]:
    """Why a write to this (already resolved) path is not allowed, or None."""
    real_path = posixpath.normpath(real_path)
    for root in (scratch, workdir):
        root = posixpath.normpath(root)
        if real_path == root or real_path.startswith(root + "/"):
            if root == posixpath.normpath(workdir):
                relative = posixpath.relpath(real_path, root)
                if matches_any(relative, forbidden):
                    return f"'{relative}' matches a forbidden path rule"
            return None
    return f"writes are only allowed inside {workdir} and {scratch}"


# ---------- patches ----------

SKIP_MARKERS = re.compile(r"@pytest\.mark\.(skip|skipif|xfail)\b|@unittest\.(skip|skipIf|skipUnless|expectedFailure)"
                          r"\b|\bpytest\.(skip|xfail)\(|\.skipTest\(|@skip(If|Unless)?\b")
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(|^\s*class\s+(Test\w*)")
# Replacing whole modules from test code: the way to make tests "pass" without a dependency they need.
MODULE_STUB = re.compile(r"\bsys\.modules\s*\[[^\]]+\]\s*=|setitem\(\s*sys\.modules|sys\.modules\.(update|setdefault)\(")


@dataclass
class Violation:
    rule: str
    message: str
    path: Optional[str] = None


def is_test_path(path: str, policy: PolicyConfig) -> bool:
    return matches_any(path, policy.test_path_globs)


def _test_names(lines: List[str]) -> set:
    names = set()
    for line in lines:
        match = TEST_DEF.match(line)
        if match:
            names.add(match.group(1) or match.group(2))
    return names


def check_patch(diff: str, policy: PolicyConfig) -> List[Violation]:
    files: List[FileChange] = parse_diff(diff)
    problems: List[Violation] = []
    if len(files) > policy.max_files_changed:
        problems.append(Violation("diff_too_large", f"{len(files)} files changed; the limit is "
                                  f"{policy.max_files_changed}"))
    lines = sum(f.added + f.removed for f in files)
    if lines > policy.max_lines_changed:
        problems.append(Violation("diff_too_large", f"{lines} lines changed; the limit is {policy.max_lines_changed}"))
    for change in files:
        if matches_any(change.path, policy.forbidden_paths):
            problems.append(Violation("forbidden_path", f"{change.path} may not be changed", change.path))
        if change.binary:
            problems.append(Violation("binary_file", f"{change.path} is a binary change", change.path))
        secrets = find_secrets("\n".join(change.added_lines))
        if secrets:
            problems.append(Violation("secret_detected", f"{change.path} adds what looks like a secret "
                                      f"({', '.join(sorted(set(secrets)))})", change.path))
        if policy.block_test_tampering and is_test_path(change.path, policy):
            removed = _test_names(change.removed_lines) - _test_names(change.added_lines)
            if removed or change.deleted:
                problems.append(Violation("test_tampering", f"{change.path} removes existing tests "
                                          f"({', '.join(sorted(removed)) or 'file deleted'})", change.path))
            skips = [line for line in change.added_lines if SKIP_MARKERS.search(line)]
            if skips:
                problems.append(Violation("test_tampering", f"{change.path} adds skip/xfail markers: "
                                          f"{skips[0].strip()[:80]}", change.path))
            stubs = [line for line in change.added_lines if MODULE_STUB.search(line)]
            if stubs:
                problems.append(Violation("test_tampering", f"{change.path} replaces modules in sys.modules "
                                          f"({stubs[0].strip()[:80]}); tests must run against real dependencies",
                                          change.path))
    return problems
