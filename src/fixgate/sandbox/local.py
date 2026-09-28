"""A sandbox that runs commands directly on this machine, in a folder.

It provides NO isolation and exists only for fixgate's own tests and for trying the agent loop on a
throwaway folder. Never point it at real issues: use DockerSandbox.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

from .base import ExecResult, HeadTail


class LocalSandbox:
    def __init__(self, workdir: Path, scratch_dir: Path, exec_timeout: int = 60, max_exec_timeout: int = 300,
                 capture_bytes: int = 200_000):
        self.workdir = str(workdir)
        self.scratch_dir = str(scratch_dir)
        self.exec_timeout = exec_timeout
        self.max_exec_timeout = max_exec_timeout
        self.capture_bytes = capture_bytes
        Path(self.scratch_dir).mkdir(parents=True, exist_ok=True)
        self.exec_count = 0

    def exec(self, command: str, timeout: Optional[int] = None, workdir: Optional[str] = None) -> ExecResult:
        limit = max(1, min(int(timeout or self.exec_timeout), self.max_exec_timeout))
        self.exec_count += 1
        started = time.monotonic()
        capture = HeadTail(self.capture_bytes)
        env = {k: v for k, v in os.environ.items() if not k.endswith(("_TOKEN", "_KEY"))}
        try:
            proc = subprocess.run(["bash", "-c", command], cwd=workdir or self.workdir, capture_output=True,
                                  timeout=limit, env=env, stdin=subprocess.DEVNULL)
            capture.feed(proc.stdout + proc.stderr)
            code, timed_out = proc.returncode, False
        except subprocess.TimeoutExpired as exc:
            capture.feed((exc.stdout or b"") + (exc.stderr or b""))
            code, timed_out = 124, True
        output = capture.text()
        if timed_out:
            output += f"\n[fixgate: the command was stopped after {limit}s]"
        return ExecResult(exit_code=code, output=output, total_bytes=capture.total, truncated=capture.truncated,
                          duration_s=round(time.monotonic() - started, 3), timed_out=timed_out)

    def _abs(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else Path(self.workdir) / p

    def read_file(self, path: str, max_bytes: Optional[int] = None) -> bytes:
        data = self._abs(path).read_bytes()
        return data[:max_bytes] if max_bytes else data

    def write_file(self, path: str, data: bytes) -> None:
        target = self._abs(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def file_info(self, path: str) -> Optional[Tuple[int, str]]:
        target = self._abs(path)
        if not target.exists():
            return None
        return (target.stat().st_size, "directory" if target.is_dir() else "regular file")

    def realpath(self, path: str) -> str:
        return os.path.realpath(self._abs(path))

    def close(self) -> None:
        return None
