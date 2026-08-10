"""Runtime control client result and interruption coverage."""

from __future__ import annotations

import os
import signal
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from comfyui_docker_helper.container import runtime_control_client as client_module
from comfyui_docker_helper.container.runtime_control import (
    RuntimeAcceptedResponse,
    RuntimeControlProtocolError,
    RuntimeErrorResponse,
    RuntimeLogResponse,
    RuntimeStatusResponse,
)
from comfyui_docker_helper.container.runtime_control_client import (
    RuntimeControlClientError,
    _write_all,
    follow_runtime,
    read_runtime_status,
    restart_runtime,
)


class _FakePeer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("sig", "exit_code"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
)
def test_interruption_before_acceptance_reports_only_unknown_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    sig: signal.Signals,
    exit_code: int,
) -> None:
    def interrupt_connect(_path: Path) -> socket.socket:
        signal.raise_signal(sig)
        raise AssertionError("signal handler did not interrupt connect")

    monkeypatch.setattr(client_module, "connect_runtime_control", interrupt_connect)

    with pytest.raises(RuntimeControlClientError) as raised:
        restart_runtime(Path("unused"))

    assert raised.value.exit_code == exit_code
    assert str(raised.value) == (
        "Restart wait was interrupted before acceptance was confirmed."
    )


@pytest.mark.parametrize(
    ("sig", "exit_code"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
)
def test_interruption_after_acceptance_reports_continuing_operation(
    monkeypatch: pytest.MonkeyPatch,
    sig: signal.Signals,
    exit_code: int,
) -> None:
    peer = _FakePeer()
    responses = 0

    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda _peer, _message: None)

    def receive(_peer: object):
        nonlocal responses
        responses += 1
        if responses == 1:
            return RuntimeAcceptedResponse(operation="op-9")
        signal.raise_signal(sig)
        raise AssertionError("signal handler did not interrupt receive")

    monkeypatch.setattr(client_module, "_receive_response", receive)

    with pytest.raises(RuntimeControlClientError) as raised:
        restart_runtime(Path("unused"))

    assert raised.value.exit_code == exit_code
    assert str(raised.value) == "Restart continues in the container: op-9."
    assert peer.closed is True


@pytest.mark.parametrize("operation", ["restart", "status"])
def test_send_failure_is_a_concise_client_error(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    peer = _FakePeer()
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)

    def fail_send(_peer: object, _message: object) -> None:
        raise BrokenPipeError("synthetic write failure")

    monkeypatch.setattr(client_module, "send_runtime_control_message", fail_send)

    with pytest.raises(RuntimeControlClientError, match=r"connection.*lost"):
        if operation == "restart":
            restart_runtime(Path("unused"))
        else:
            read_runtime_status(Path("unused"))

    assert peer.closed is True


def test_follow_preserves_binary_stdout_and_stderr_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = _FakePeer()
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    responses: Iterator[object] = iter(
        (
            RuntimeLogResponse.from_bytes("stdout", b"out\x00\xff"),
            RuntimeLogResponse.from_bytes("stderr", b"err\x80tail"),
            None,
        )
    )
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda _peer, _message: None)
    monkeypatch.setattr(
        client_module,
        "_receive_follow_response",
        lambda _peer: next(responses),
    )
    try:
        assert (
            follow_runtime(
                Path("unused"),
                stdout_fd=stdout_write,
                stderr_fd=stderr_write,
            )
            == 0
        )
    finally:
        os.close(stdout_write)
        os.close(stderr_write)

    assert os.read(stdout_read, 1024) == b"out\x00\xff"
    assert os.read(stderr_read, 1024) == b"err\x80tail"
    os.close(stdout_read)
    os.close(stderr_read)
    assert peer.closed is True


def test_follow_local_output_failure_is_silent_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = _FakePeer()
    responses = iter((RuntimeLogResponse.from_bytes("stdout", b"payload"),))
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda _peer, _message: None)
    monkeypatch.setattr(
        client_module,
        "_receive_follow_response",
        lambda _peer: next(responses),
    )

    def fail_output(_fd: int, _data: bytes) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(client_module, "_write_all", fail_output)

    assert follow_runtime(Path("unused")) == 1
    assert peer.closed is True


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            RuntimeControlProtocolError("invalid_message"),
            "malformed response",
        ),
        (ConnectionResetError("synthetic reset"), "connection.*lost"),
    ],
)
def test_follow_transport_failure_is_concise(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    message: str,
) -> None:
    peer = _FakePeer()
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda _peer, _message: None)

    def fail_receive(_peer: object) -> object:
        raise error

    monkeypatch.setattr(client_module, "receive_runtime_control_response", fail_receive)

    with pytest.raises(RuntimeControlClientError, match=message):
        follow_runtime(Path("unused"))

    assert peer.closed is True


@pytest.mark.parametrize(
    ("sig", "exit_code"),
    [
        (signal.SIGHUP, 129),
        (signal.SIGINT, 130),
        (signal.SIGTERM, 143),
    ],
)
def test_follow_signal_ends_only_the_local_client(
    monkeypatch: pytest.MonkeyPatch,
    sig: signal.Signals,
    exit_code: int,
) -> None:
    peer = _FakePeer()
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)

    def interrupt_send(_peer: object, _message: object) -> None:
        signal.raise_signal(sig)

    monkeypatch.setattr(client_module, "_send_message", interrupt_send)

    assert follow_runtime(Path("unused")) == exit_code
    assert peer.closed is True


@pytest.mark.parametrize(
    "response",
    [
        RuntimeErrorResponse(code="unavailable", message="follow unavailable"),
        RuntimeStatusResponse(
            state="running",
            phase=None,
            generation="gen-1",
            operation=None,
            last_restart=None,
        ),
    ],
)
def test_follow_rejects_typed_error_and_unexpected_response(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
) -> None:
    peer = _FakePeer()
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda _peer, _message: None)
    monkeypatch.setattr(
        client_module,
        "_receive_follow_response",
        lambda _peer: response,
    )

    with pytest.raises(RuntimeControlClientError):
        follow_runtime(Path("unused"))

    assert peer.closed is True


def test_local_output_write_retries_interruption_and_partial_progress() -> None:
    writes: list[bytes] = []
    attempts = 0

    def writer(fd: int, data: bytes | memoryview) -> int:
        nonlocal attempts
        assert fd == 42
        attempts += 1
        if attempts == 1:
            raise InterruptedError
        chunk = bytes(data[:2])
        writes.append(chunk)
        return len(chunk)

    _write_all(42, b"abcdef", writer=writer)

    assert attempts == 4
    assert b"".join(writes) == b"abcdef"
