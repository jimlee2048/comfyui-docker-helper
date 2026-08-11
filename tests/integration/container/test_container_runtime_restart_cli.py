"""Real terminal interruption boundaries for the runtime restart CLI."""

from __future__ import annotations

import errno
import os
import pty
import signal
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import pytest

from comfyui_docker_helper.container.runtime_control import (
    open_runtime_control_listener,
)
from comfyui_docker_helper.container.runtime_control_server import (
    RuntimeControlServer,
)
from comfyui_docker_helper.container.runtime_controller import RuntimeController
from comfyui_docker_helper.container.runtime_logging import RuntimeLoggingBroker

_RESTART_CLI = """
import sys
from pathlib import Path

from comfyui_docker_helper.container import cli as container_cli
from comfyui_docker_helper.container import runtime_control_client as client
from comfyui_docker_helper.container.runtime_control import RuntimeAcceptedResponse
from comfyui_docker_helper.cli import app

endpoint = Path(sys.argv[1])
marker = None if sys.argv[2] == "-" else Path(sys.argv[2])
original_receive = client._receive_response
accepted_seen = None

def observe_acceptance(peer):
    global accepted_seen
    if marker is not None and accepted_seen is not None and not marker.exists():
        marker.write_text(accepted_seen, encoding="utf-8")
    response = original_receive(peer)
    if isinstance(response, RuntimeAcceptedResponse):
        accepted_seen = response.operation
    return response

client._receive_response = observe_acceptance
container_cli.restart_runtime = lambda: client.restart_runtime(endpoint)
sys.argv = ["cdh", "container", "runtime", "restart"]
app()
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


def _spawn_restart_cli(endpoint: Path, marker: Path | None) -> tuple[int, int]:
    pid, master_fd = pty.fork()
    if pid == 0:
        marker_arg = "-" if marker is None else os.fspath(marker)
        os.execv(
            sys.executable,
            [sys.executable, "-c", _RESTART_CLI, os.fspath(endpoint), marker_arg],
        )
    return pid, master_fd


def _wait_for_child(pid: int, *, timeout: float = 3.0) -> int:
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while True:
        completed, status = os.waitpid(pid, os.WNOHANG)
        if completed == pid:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= deadline:
            os.kill(pid, signal.SIGKILL)
            _completed, status = os.waitpid(pid, 0)
            pytest.fail(
                "restart CLI did not exit before the test deadline "
                f"(forced result {os.waitstatus_to_exitcode(status)})"
            )
        wake.wait(0.01)


def _read_terminal(master_fd: int) -> str:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(master_fd, 4096)
        except OSError as error:
            if error.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


@pytest.mark.parametrize("after_acceptance", [False, True])
def test_restart_cli_ctrl_c_never_cancels_an_accepted_operation(
    tmp_path: Path,
    after_acceptance: bool,
) -> None:
    endpoint = _endpoint(tmp_path)
    marker = tmp_path / "accepted" if after_acceptance else None
    controller = _running_controller()
    listener = open_runtime_control_listener(endpoint)
    broker = _active_logging_broker()
    pid: int | None = None
    master_fd: int | None = None
    child_reaped = False

    try:
        pid, master_fd = _spawn_restart_cli(endpoint, marker)
        with RuntimeControlServer(listener, controller, broker):
            assert controller.wait(2.0) is True
            if after_acceptance:
                assert controller.accept_if_requested(accepted_at=1.0) is True
                assert marker is not None
                _wait_until(marker.exists)

            os.write(master_fd, b"\x03")
            exit_code = _wait_for_child(pid)
            child_reaped = True
            output = _read_terminal(master_fd)

            if after_acceptance:
                snapshot = controller.snapshot()
                assert snapshot.operation == "op-1"
                assert snapshot.state == "restarting"
                assert controller.allocate_restart_successor() == "gen-2"
                controller.publish_restart_terminal("succeeded")
                assert controller.release_successful_restart() is True
            else:
                _wait_until(lambda: not controller.wait(0))
                assert controller.snapshot().state == "running"
                assert controller.snapshot().operation is None
    finally:
        if pid is not None and not child_reaped:
            try:
                completed, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            else:
                if completed == 0:
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
        if master_fd is not None:
            os.close(master_fd)

    assert exit_code == 130
    assert "Aborted!" not in output
    if after_acceptance:
        assert "Restart continues in the container: op-1." in output
        assert controller.snapshot().state == "running"
        assert controller.snapshot().generation == "gen-2"
    else:
        assert "Restart wait was interrupted before acceptance was confirmed." in output
        assert controller.snapshot().generation == "gen-1"
