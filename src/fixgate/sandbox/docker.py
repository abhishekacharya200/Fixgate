"""A Docker container as the agent's sandbox.

Isolation, in layers:
- no network (`--network none`; in live mode the network is detached after the fixed setup commands)
- memory, CPU, process-count, file-size and open-file limits
- all Linux capabilities dropped, no privilege escalation, an init process to reap zombies
- every command runs under `timeout -k`, with a second watchdog outside the container
- output is captured head+tail, so a command that prints gigabytes cannot exhaust memory
- no secrets inside: no tokens or API keys are ever passed to the container

The container is thrown away after the task; only the diff leaves it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from typing import List, Optional, Sequence, Tuple

from .base import ExecResult, HeadTail, SandboxError

DOCKER = os.environ.get("FIXGATE_DOCKER", "docker")
SCRATCH = "/tmp/fixgate"


def docker_available() -> bool:
    try:
        return subprocess.run([DOCKER, "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run(args: Sequence[str], timeout: float = 120, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([DOCKER, *args], capture_output=True, timeout=timeout, input=input)
    except subprocess.TimeoutExpired as exc:
        raise SandboxError("docker_timeout", f"docker {args[0]} did not finish in {timeout}s") from exc
    except OSError as exc:
        raise SandboxError("docker_missing", str(exc)) from exc


def image_present(image: str) -> bool:
    return _run(["image", "inspect", image], timeout=60).returncode == 0


def pull_image(image: str, attempts: int = 3, timeout: float = 1800) -> None:
    """Pull with retries; network hiccups while pulling multi-GB images are common."""
    last = ""
    for attempt in range(1, attempts + 1):
        result = _run(["pull", "--quiet", image], timeout=timeout)
        if result.returncode == 0:
            return
        last = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        if "not found" in last.lower() or "manifest unknown" in last.lower():
            break
        time.sleep(min(60, 5 * 2 ** attempt))
    raise SandboxError("image_pull", f"{image}: {last[-500:]}")


class DockerSandbox:
    def __init__(self, image: str, *, workdir: str, env_setup: str = "", name: Optional[str] = None,
                 memory: str = "3g", cpus: float = 2.0, pids_limit: int = 1024, network: str = "none",
                 mounts: Optional[List[Tuple[str, str, str]]] = None, user: Optional[str] = None,
                 env: Optional[dict] = None, exec_timeout: int = 180, max_exec_timeout: int = 900,
                 capture_bytes: int = 2_000_000, file_size_limit: int = 2 * 1024 ** 3,
                 labels: Optional[dict] = None):
        self.image = image
        self.workdir = workdir
        self.env_setup = env_setup.strip()
        self.name = name or f"fixgate-{uuid.uuid4().hex[:12]}"
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.network = network
        self.mounts = mounts or []
        self.user = user
        self.env = env or {}
        self.exec_timeout = exec_timeout
        self.max_exec_timeout = max_exec_timeout
        self.capture_bytes = capture_bytes
        self.file_size_limit = file_size_limit
        self.labels = labels or {}
        self.container_id: Optional[str] = None
        self.exec_count = 0
        self.scratch_dir = SCRATCH

    # ---------- lifecycle ----------

    def run_args(self) -> List[str]:
        args = ["run", "-d", "--init", "--name", self.name,
                "--network", self.network,
                "--memory", self.memory, "--memory-swap", self.memory,
                "--cpus", str(self.cpus), "--pids-limit", str(self.pids_limit),
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--ulimit", f"fsize={self.file_size_limit}:{self.file_size_limit}",
                "--ulimit", "nofile=8192:8192",
                "--label", "fixgate=1",
                "-w", self.workdir, "--entrypoint", "tail"]
        for key, value in self.labels.items():
            args += ["--label", f"{key}={value}"]
        for host, container, mode in self.mounts:
            args += ["-v", f"{host}:{container}:{mode}"]
        for key, value in self.env.items():
            args += ["-e", f"{key}={value}"]
        if self.user:
            args += ["--user", self.user, "-e", "HOME=/tmp/fixgate-home"]
        return args + [self.image, "-f", "/dev/null"]

    def start(self) -> "DockerSandbox":
        result = _run(self.run_args(), timeout=300)
        if result.returncode != 0:
            raise SandboxError("container_start", result.stderr.decode("utf-8", "replace").strip()[-800:])
        self.container_id = result.stdout.decode().strip()
        setup = self.exec(f"mkdir -p {SCRATCH} /tmp/fixgate-home && (git config --global user.name fixgate; "
                          f"git config --global user.email fixgate@localhost) >/dev/null 2>&1; echo ok",
                          timeout=60, workdir="/")
        if not setup.ok:
            self.close()
            raise SandboxError("container_start", f"the container does not run commands: {setup.output[-500:]}")
        return self

    def __enter__(self) -> "DockerSandbox":
        return self.start() if self.container_id is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.container_id or self.name:
            _run(["rm", "-f", self.name], timeout=120)
        self.container_id = None

    def disconnect_network(self) -> None:
        """Cut every network after the setup commands, before the agent's first command."""
        result = _run(["inspect", "--format", "{{json .NetworkSettings.Networks}}", self.name], timeout=60)
        networks = json.loads(result.stdout.decode() or "{}") or {}
        for network in networks:
            if network == "none":
                continue
            done = _run(["network", "disconnect", "--force", network, self.name], timeout=60)
            if done.returncode != 0:
                raise SandboxError("network", done.stderr.decode("utf-8", "replace").strip())
        self.network = "none"

    def networks(self) -> List[str]:
        result = _run(["inspect", "--format", "{{json .NetworkSettings.Networks}}", self.name], timeout=60)
        return [n for n in (json.loads(result.stdout.decode() or "{}") or {}) if n != "none"]

    # ---------- commands ----------

    def exec(self, command: str, timeout: Optional[int] = None, workdir: Optional[str] = None) -> ExecResult:
        if self.container_id is None:
            raise SandboxError("exec", "the sandbox is not running")
        limit = max(1, min(int(timeout or self.exec_timeout), self.max_exec_timeout))
        marker = f"fixgate-exec-{uuid.uuid4().hex[:10]}"
        steps = []
        if self.env_setup:
            steps.append(self.env_setup)
        steps.append(f"cd {shlex.quote(workdir or self.workdir)}")
        steps.append(f"timeout -k 5 {limit} bash -c {shlex.quote(command)} {marker} 2>&1")
        script = " && ".join(steps[:-1] + [steps[-1]]) if len(steps) > 1 else steps[0]
        self.exec_count += 1
        capture = HeadTail(self.capture_bytes)
        started = time.monotonic()
        proc = subprocess.Popen([DOCKER, "exec", self.name, "bash", "-c", script], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        def pump() -> None:
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                capture.feed(chunk)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=limit + 30)
        except subprocess.TimeoutExpired:
            # `timeout` inside the container should have fired; kill from outside as a fallback.
            timed_out = True
            proc.kill()
            self._kill_marked(marker)
            proc.wait(timeout=30)
        reader.join(timeout=30)
        duration = time.monotonic() - started
        code = proc.returncode if proc.returncode is not None else -9
        output = capture.text()
        if code in (124, 137) and duration >= limit - 1:
            timed_out = True
        if code != 0 and output.startswith("Error response from daemon"):
            raise SandboxError("exec", output.strip()[:500])
        if timed_out:
            output += f"\n[fixgate: the command was stopped after {limit}s]"
        return ExecResult(exit_code=code, output=output, total_bytes=capture.total, truncated=capture.truncated,
                          duration_s=round(duration, 3), timed_out=timed_out)

    def _kill_marked(self, marker: str) -> None:
        script = (f"for p in /proc/[0-9]*; do grep -q {marker} $p/cmdline 2>/dev/null && kill -9 ${{p#/proc/}}; "
                  f"done; true")
        _run(["exec", self.name, "bash", "-c", script], timeout=60)

    # ---------- files ----------

    def read_file(self, path: str, max_bytes: Optional[int] = None) -> bytes:
        cmd = ["exec", self.name] + (["head", "-c", str(max_bytes), "--", path] if max_bytes else ["cat", "--", path])
        result = _run(cmd, timeout=120)
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr.decode("utf-8", "replace").strip() or path)
        return result.stdout

    def write_file(self, path: str, data: bytes) -> None:
        script = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
        result = _run(["exec", "-i", self.name, "bash", "-c", script, "fixgate-write", path], timeout=120, input=data)
        if result.returncode != 0:
            raise OSError(result.stderr.decode("utf-8", "replace").strip() or f"cannot write {path}")

    def file_info(self, path: str) -> Optional[Tuple[int, str]]:
        """(size in bytes, kind) or None if the path does not exist."""
        result = _run(["exec", self.name, "stat", "-L", "-c", "%s|%F", "--", path], timeout=60)
        if result.returncode != 0:
            return None
        size, kind = result.stdout.decode().strip().split("|", 1)
        return int(size), kind

    def realpath(self, path: str) -> str:
        result = _run(["exec", "-w", self.workdir, self.name, "realpath", "-m", "--", path], timeout=60)
        if result.returncode != 0:
            raise SandboxError("exec", result.stderr.decode("utf-8", "replace").strip())
        return result.stdout.decode().strip()

    def copy_in(self, host_path: str, container_path: str) -> None:
        result = _run(["cp", host_path, f"{self.name}:{container_path}"], timeout=300)
        if result.returncode != 0:
            raise SandboxError("copy", result.stderr.decode("utf-8", "replace").strip())


def remove_leftovers() -> int:
    """Remove fixgate containers left behind by a crash (`fixgate sandbox gc`)."""
    result = _run(["ps", "-aq", "--filter", "label=fixgate=1"], timeout=60)
    ids = result.stdout.decode().split()
    if ids:
        _run(["rm", "-f", *ids], timeout=300)
    return len(ids)
