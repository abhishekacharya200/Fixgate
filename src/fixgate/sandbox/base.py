"""What the agent's tools need from a sandbox, independent of how it is isolated."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass
class ExecResult:
    exit_code: int
    output: str            # stdout and stderr interleaved; the middle is cut when it is very long
    total_bytes: int
    truncated: bool
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxError(Exception):
    """Docker or the environment failed (not the agent's command)."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


class Sandbox(Protocol):
    workdir: str

    def exec(self, command: str, timeout: Optional[int] = None, workdir: Optional[str] = None) -> ExecResult:
        ...

    def read_file(self, path: str) -> bytes:
        ...

    def write_file(self, path: str, data: bytes) -> None:
        ...

    def realpath(self, path: str) -> str:
        ...

    def close(self) -> None:
        ...


class HeadTail:
    """Keeps the first and last `limit / 2` bytes of a stream and counts the rest."""

    def __init__(self, limit: int):
        self.half = max(1024, limit // 2)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.half:
                del self.tail[: len(self.tail) - self.half]

    def text(self) -> str:
        head = self.head.decode("utf-8", "replace")
        if self.total <= len(self.head) + len(self.tail):
            body = head + self.tail.decode("utf-8", "replace")
        else:
            omitted = self.total - len(self.head) - len(self.tail)
            body = head + f"\n\n[... {omitted} bytes omitted ...]\n\n" + self.tail.decode("utf-8", "replace")
        return _ANSI.sub("", body).replace("\r\n", "\n")

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)
