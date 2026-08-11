"""Live runtime follow behavior across the real private UDS boundary."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import pytest

from comfyui_docker_helper.container.runtime_control import (
    open_runtime_control_listener,
)
from comfyui_docker_helper.container.runtime_control_client import read_runtime_status
from comfyui_docker_helper.container.runtime_control_server import (
    RuntimeControlServer,
)
from comfyui_docker_helper.container.runtime_controller import RuntimeController
from comfyui_docker_helper.container.runtime_logging import (
    RuntimeLogChunk,
    RuntimeLoggingBroker,
)

_FOLLOW_CLIENT = """
import sys
from pathlib import Path

from comfyui_docker_helper.container.runtime_control_client import follow_runtime

raise SystemExit(follow_runtime(Path(sys.argv[1])))
"""


def _endpoint(tmp_path: Path) -> Path:
    return tmp_path / "runtime-control" / "runtime.sock"


def _running_controller() -> RuntimeController:
    controller = RuntimeController()
    controller.begin_initial_admission()
    controller.mark_initial_generation_running()
    return controller


def _active_logging_broker() -> RuntimeLoggingBroker:
    broker = RuntimeLoggingBroker()
    broker._started = True
    return broker


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition did not become true before the test deadline")
        wake.wait(0.01)


def _start_follow(endpoint: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", _FOLLOW_CLIENT, str(endpoint)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _read_exact(pipe: BinaryIO, length: int) -> bytes:
    fd = pipe.fileno()
    chunks: list[bytes] = []
    remaining = length
    deadline = time.monotonic() + 2.0
    while remaining:
        readable, _writable, _exceptional = select.select(
            (fd,),
            (),
            (),
            max(0.0, deadline - time.monotonic()),
        )
        if not readable:
            pytest.fail("follow output did not arrive before the test deadline")
        chunk = os.read(fd, remaining)
        if not chunk:
            pytest.fail("follow output closed before the expected bytes arrived")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _terminate_follow(client: subprocess.Popen[bytes]) -> None:
    if client.poll() is None:
        client.terminate()
    try:
        client.communicate(timeout=1.0)
    except subprocess.TimeoutExpired:
        client.kill()
        client.communicate(timeout=1.0)


def test_follow_is_live_only_and_survives_one_runtime_restart(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    controller = _running_controller()
    broker = _active_logging_broker()
    broker._publish(RuntimeLogChunk("stdout", b"before-subscription"))
    listener = open_runtime_control_listener(endpoint)

    client: subprocess.Popen[bytes] | None = None
    completed = False
    try:
        with RuntimeControlServer(listener, controller, broker):
            client = _start_follow(endpoint)
            _wait_until(lambda: len(broker._followers) == 1)
            broker._publish(RuntimeLogChunk("stdout", b"old\x00\xff"))
            assert client.stdout is not None
            stdout = _read_exact(client.stdout, len(b"old\x00\xff"))

            submission = controller.submit_restart(delivery_expected=False)
            assert submission.disposition == "submitted"
            assert controller.accept_if_requested(accepted_at=1.0) is True
            assert controller.allocate_restart_successor() == "gen-2"
            controller.publish_restart_terminal("succeeded")
            assert controller.release_successful_restart() is True

            broker._publish(RuntimeLogChunk("stderr", b"new\x80tail"))
            assert client.stderr is not None
            stderr = _read_exact(client.stderr, len(b"new\x80tail"))

        remaining_stdout, remaining_stderr = client.communicate(timeout=5.0)
        completed = True
    finally:
        if client is not None and not completed:
            _terminate_follow(client)
    assert client.returncode == 0
    assert stdout + remaining_stdout == b"old\x00\xff"
    assert stderr + remaining_stderr == b"new\x80tail"


def test_follow_sigint_is_local_and_releases_only_its_follower(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    controller = _running_controller()
    broker = _active_logging_broker()
    listener = open_runtime_control_listener(endpoint)

    client: subprocess.Popen[bytes] | None = None
    completed = False
    try:
        with RuntimeControlServer(listener, controller, broker):
            client = _start_follow(endpoint)
            _wait_until(lambda: len(broker._followers) == 1)
            client.send_signal(signal.SIGINT)
            stdout, stderr = client.communicate(timeout=5.0)
            completed = True
            _wait_until(lambda: len(broker._followers) == 0)

            status = read_runtime_status(endpoint)
    finally:
        if client is not None and not completed:
            _terminate_follow(client)

    assert client.returncode == 130
    assert stdout == b""
    assert stderr == b""
    assert status.state == "running"
    assert status.generation == "gen-1"
