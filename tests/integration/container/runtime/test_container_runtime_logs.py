"""Retained and live runtime logs across the real private UDS boundary."""

from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import pytest

from comfyui_docker_helper.config.logs import RuntimeLogSettings
from comfyui_docker_helper.container.runtime.control import server as server_module
from comfyui_docker_helper.container.runtime.control.client import read_runtime_status
from comfyui_docker_helper.container.runtime.control.protocol import (
    RuntimeControlMessage,
    RuntimeLogEndResponse,
    RuntimeLogResponse,
)
from comfyui_docker_helper.container.runtime.control.server import (
    RuntimeControlServer,
)
from comfyui_docker_helper.container.runtime.control.transport import (
    open_runtime_control_listener,
)
from comfyui_docker_helper.container.runtime.controller import RuntimeController
from comfyui_docker_helper.container.runtime.log_storage import (
    LogStorageError,
    LogStorageFailure,
)
from comfyui_docker_helper.container.runtime.logging import (
    RuntimeLogChunk,
    RuntimeLogFollower,
    RuntimeLoggingBroker,
)

_LOGS_CLIENT = """
import sys
from pathlib import Path
from comfyui_docker_helper.container.runtime.control.client import (
    RuntimeControlClientError, read_runtime_logs,
)
try:
    result = read_runtime_logs(
        Path(sys.argv[1]),
        tail=None if sys.argv[2] == "all" else int(sys.argv[2]),
        follow=sys.argv[3] == "1",
    )
except RuntimeControlClientError as error:
    print(str(error), file=sys.stderr)
    result = error.exit_code
raise SystemExit(result)
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
    broker.configure(RuntimeLogSettings(mode="memory"))
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
        [sys.executable, "-c", _LOGS_CLIENT, str(endpoint), "0", "1"],
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
            later = _read_exact(client.stdout, len(b"new\x80tail"))
            broker.close()

        remaining_stdout, remaining_stderr = client.communicate(timeout=5.0)
        completed = True
    finally:
        if client is not None and not completed:
            _terminate_follow(client)
    assert client.returncode == 0
    assert stdout + later + remaining_stdout == b"old\x00\xffnew\x80tail"
    assert remaining_stderr == b""


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


def _read_logs(
    endpoint: Path, *, tail: int | None = None, follow: bool = False
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _LOGS_CLIENT,
            str(endpoint),
            "all" if tail is None else str(tail),
            "1" if follow else "0",
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )


def test_finite_history_crosses_wire_chunks_and_tail_is_raw(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    broker = _active_logging_broker()
    payload = b"before\n" + b"\xff\x00\r\x1b[1m" * (12 * 1024) + b"\nlast"
    broker._publish(RuntimeLogChunk("stderr", payload))
    with RuntimeControlServer(
        open_runtime_control_listener(endpoint), _running_controller(), broker
    ):
        result = _read_logs(endpoint)
        tail = _read_logs(endpoint, tail=1)
        empty = _read_logs(endpoint, tail=0)
    broker.close()
    assert result.returncode == tail.returncode == empty.returncode == 0
    assert result.stdout == payload
    assert tail.stdout == b"last"
    assert empty.stdout == b""
    assert result.stderr == tail.stderr == empty.stderr == b""


def test_gap_replay_returns_both_sides_and_never_enters_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = _endpoint(tmp_path)
    broker = RuntimeLoggingBroker()
    broker._started = True
    broker.configure(RuntimeLogSettings(directory=str(tmp_path / "logs"), max_size=400))
    old, recent = b"old\n" * 25, b"new\n" * 100
    broker._publish(RuntimeLogChunk("stdout", old))
    assert broker._file_writer.wait_for_prefix(100, deadline=time.monotonic() + 2)
    failed = threading.Event()
    broker.set_storage_warning_observer(lambda _failure: failed.set())

    def fail_append(_start: int, _data: bytes) -> None:
        raise LogStorageError(LogStorageFailure.WRITE)

    monkeypatch.setattr(broker._store, "append", fail_append)
    try:
        broker._publish(RuntimeLogChunk("stdout", b"lost\n" * 100))
        assert failed.wait(2)
        broker._publish(RuntimeLogChunk("stderr", recent))
        with RuntimeControlServer(
            open_runtime_control_listener(endpoint), _running_controller(), broker
        ):
            all_history = _read_logs(endpoint, follow=True)
            tail = _read_logs(endpoint, tail=2)
            incomplete_tail = _read_logs(endpoint, tail=101)
        assert all_history.returncode == incomplete_tail.returncode == 1
        assert all_history.stdout == old + recent
        assert incomplete_tail.stdout == recent
        assert b"incomplete" in all_history.stderr.lower()
        assert tail.returncode == 0
        assert tail.stdout == b"new\n" * 2
        assert b"memory" in tail.stderr.lower()
    finally:
        broker.close()


def test_finite_tail_reports_file_failure_during_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = _endpoint(tmp_path)
    broker = RuntimeLoggingBroker()
    broker._started = True
    broker.configure(RuntimeLogSettings(directory=str(tmp_path / "logs")))
    failed = threading.Event()
    broker.set_storage_warning_observer(lambda _failure: failed.set())
    send = server_module.send_runtime_control_message

    def fail_append(_start: int, _data: bytes) -> None:
        raise LogStorageError(LogStorageFailure.WRITE)

    def fail_recording_after_payload(
        peer: socket.socket, message: RuntimeControlMessage
    ) -> None:
        send(peer, message)
        if isinstance(message, RuntimeLogResponse):
            broker._publish(RuntimeLogChunk("stderr", b"after-query\n"))
            assert failed.wait(2)

    try:
        payload = b"older\nrecent\n"
        broker._publish(RuntimeLogChunk("stdout", payload))
        assert broker._file_writer.wait_for_prefix(
            len(payload), deadline=time.monotonic() + 2
        )
        monkeypatch.setattr(broker._store, "append", fail_append)
        monkeypatch.setattr(
            server_module, "send_runtime_control_message", fail_recording_after_payload
        )
        with RuntimeControlServer(
            open_runtime_control_listener(endpoint), _running_controller(), broker
        ):
            result = _read_logs(endpoint, tail=1)
        assert failed.is_set()
        assert result.returncode == 0
        assert result.stdout == b"recent\n"
        assert result.stderr.count(b"memory") == 1
    finally:
        broker.close()


@pytest.mark.parametrize("shutdown", ["complete", "expired", "forced", "disconnected"])
def test_live_end_distinguishes_complete_and_interrupted_delivery(
    tmp_path: Path, shutdown: str
) -> None:
    endpoint = _endpoint(tmp_path)
    broker = _active_logging_broker()
    server = RuntimeControlServer(
        open_runtime_control_listener(endpoint), _running_controller(), broker
    )
    server.start()
    client = _start_follow(endpoint)
    try:
        _wait_until(lambda: len(broker._followers) == 1)
        broker._publish(RuntimeLogChunk("stderr", b"before-close\xff"))
        assert client.stdout is not None
        assert _read_exact(client.stdout, 13) == b"before-close\xff"
        deadline = time.monotonic() + (-1 if shutdown == "expired" else 0.5)

        def force() -> bool:
            return shutdown == "forced"

        server.stop_accepting(deadline=deadline, force_requested=force)
        if shutdown != "disconnected":
            broker.close(deadline=deadline, force_requested=force)
        server.close(deadline=deadline, force_requested=force)
        stdout, stderr = client.communicate(timeout=3)
        assert stdout == b""
        if shutdown == "complete":
            assert client.returncode == 0
            assert stderr == b""
        else:
            assert client.returncode == 1
            assert b"without a log result" in stderr
    finally:
        server.close(deadline=time.monotonic())
        broker.close()
        _terminate_follow(client)


@pytest.mark.parametrize("late_force", [False, True])
def test_shutdown_drains_queued_bytes_before_single_sender_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_force: bool
) -> None:
    endpoint = _endpoint(tmp_path)
    broker = _active_logging_broker()
    server = RuntimeControlServer(
        open_runtime_control_listener(endpoint), _running_controller(), broker
    )
    entered, release = threading.Event(), threading.Event()
    send = server._send_log_message
    senders: set[int] = set()
    forced = threading.Event()

    def gated_send(peer: socket.socket, message: RuntimeControlMessage) -> None:
        senders.add(threading.get_ident())
        if isinstance(message, RuntimeLogResponse) and not entered.is_set():
            entered.set()
            assert release.wait(2)
        if isinstance(message, RuntimeLogEndResponse) and late_force:
            forced.set()
        send(peer, message)

    monkeypatch.setattr(server, "_send_log_message", gated_send)
    server.start()
    client = _start_follow(endpoint)
    try:
        _wait_until(lambda: len(broker._followers) == 1)
        broker._publish(RuntimeLogChunk("stdout", b"first"))
        assert entered.wait(2)
        broker._publish(RuntimeLogChunk("stderr", b"last\xff"))
        deadline = time.monotonic() + 0.5
        server.stop_accepting(deadline=deadline, force_requested=forced.is_set)
        broker.close(deadline=deadline)
        release.set()
        server.close(deadline=deadline, force_requested=forced.is_set)
        stdout, stderr = client.communicate(timeout=3)
        assert client.returncode == (1 if late_force else 0)
        assert stdout == b"firstlast\xff"
        if late_force:
            assert b"without a log result" in stderr
        else:
            assert stderr == b""
        assert len(senders) == 1
        assert threading.get_ident() not in senders
        assert broker._query_count == 0
    finally:
        release.set()
        server.close(deadline=time.monotonic())
        broker.close()
        _terminate_follow(client)


def test_final_sync_failure_is_diagnosed_before_successful_live_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = _endpoint(tmp_path)
    broker = RuntimeLoggingBroker()
    broker._started = True
    broker.configure(RuntimeLogSettings(directory=str(tmp_path / "logs")))
    waiting, release = threading.Event(), threading.Event()
    receive = RuntimeLogFollower.receive

    def gated_receive(
        self: RuntimeLogFollower, timeout: float | None = None
    ) -> RuntimeLogChunk | None:
        waiting.set()
        assert release.wait(2)
        return receive(self, timeout)

    def failed_sync() -> None:
        raise LogStorageError(LogStorageFailure.SYNC)

    monkeypatch.setattr(RuntimeLogFollower, "receive", gated_receive)
    monkeypatch.setattr(broker._store, "sync", failed_sync)
    server = RuntimeControlServer(
        open_runtime_control_listener(endpoint), _running_controller(), broker
    )
    server.start()
    client = _start_follow(endpoint)
    try:
        assert waiting.wait(2)
        deadline = time.monotonic() + 0.5
        server.stop_accepting(deadline=deadline)
        broker.close(deadline=deadline)
        release.set()
        server.close(deadline=deadline)
        stdout, stderr = client.communicate(timeout=3)
        assert client.returncode == 0
        assert stdout == b""
        assert stderr.count(b"sync") == 1
        assert b"memory" in stderr
    finally:
        release.set()
        server.close(deadline=time.monotonic())
        broker.close()
        _terminate_follow(client)


def test_live_send_failure_before_frame_is_not_successful_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = _endpoint(tmp_path)
    broker = _active_logging_broker()
    controller = _running_controller()
    send = server_module.send_runtime_control_message

    def failed_send(peer: socket.socket, message: RuntimeControlMessage) -> None:
        if isinstance(message, RuntimeLogResponse):
            raise TimeoutError("synthetic send failure before frame")
        send(peer, message)

    monkeypatch.setattr(server_module, "send_runtime_control_message", failed_send)
    client: subprocess.Popen[bytes] | None = None
    try:
        with RuntimeControlServer(
            open_runtime_control_listener(endpoint), controller, broker
        ):
            client = _start_follow(endpoint)
            _wait_until(lambda: len(broker._followers) == 1)
            broker._publish(RuntimeLogChunk("stdout", b"not-delivered"))
            stdout, stderr = client.communicate(timeout=3)
            assert client.returncode == 1
            assert stdout == b""
            assert b"without a log result" in stderr
            assert read_runtime_status(endpoint).state == "running"
            _wait_until(lambda: broker._query_count == 0)
    finally:
        broker.close()
        if client is not None:
            _terminate_follow(client)
