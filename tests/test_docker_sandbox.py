"""The Docker sandbox's isolation, on a small public image. Skipped when Docker is not available."""

import pytest

from fixgate.sandbox.docker import DockerSandbox, docker_available, image_present, pull_image

IMAGE = "python:3.12-slim"

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def sandbox():
    if not docker_available():
        pytest.skip("Docker is not available")
    if not image_present(IMAGE):
        pull_image(IMAGE)
    sb = DockerSandbox(IMAGE, workdir="/work", memory="512m", cpus=1, pids_limit=128, exec_timeout=20,
                       max_exec_timeout=30, capture_bytes=100_000).start()
    sb.exec("mkdir -p /work", workdir="/")
    yield sb
    sb.close()


def test_runs_commands_and_files(sandbox):
    result = sandbox.exec("echo hello && echo oops >&2 && exit 3")
    assert result.exit_code == 3 and "hello" in result.output and "oops" in result.output
    sandbox.write_file("/work/a/b.txt", b"data\n")
    assert sandbox.read_file("/work/a/b.txt") == b"data\n"
    assert sandbox.file_info("/work/a/b.txt")[0] == 5
    assert sandbox.file_info("/work/missing") is None


def test_no_network(sandbox):
    result = sandbox.exec("python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\"")
    assert result.exit_code != 0 and "unreachable" in result.output.lower()
    assert sandbox.networks() == []


def test_timeouts_and_output_limits(sandbox):
    slow = sandbox.exec("sleep 60", timeout=2)
    assert slow.timed_out and slow.duration_s < 15
    loud = sandbox.exec("yes | head -c 3000000")
    assert loud.truncated and loud.total_bytes == 3_000_000 and len(loud.output) < 110_000


def test_resource_and_privilege_limits(sandbox):
    caps = sandbox.exec("grep CapEff /proc/self/status")
    assert caps.output.split()[-1] == "0000000000000000"          # every capability dropped
    nnp = sandbox.exec("grep NoNewPrivs /proc/self/status")
    assert nnp.output.split()[-1] == "1"
    bomb = sandbox.exec("for i in $(seq 1 400); do sleep 5 & done; wait", timeout=20)
    assert bomb.exit_code != 0 or "Resource temporarily unavailable" in bomb.output
