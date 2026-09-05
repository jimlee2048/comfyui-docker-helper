"""Runtime control client result and interruption coverage."""

from __future__ import annotations

import os
import signal
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from comfyui_docker_helper.container.runtime.control import client as client_module
from comfyui_docker_helper.container.runtime.control.client import (
    RuntimeControlClientError,
    _write_all,
    read_runtime_logs,
    read_runtime_status,
    restart_runtime,
)
from comfyui_docker_helper.container.runtime.control.protocol import (
    RuntimeAcceptedResponse,
    RuntimeAckRequest,
    RuntimeControlProtocolError,
    RuntimeErrorResponse,
    RuntimeLogDiagnosticResponse,
    RuntimeLogEndResponse,
    RuntimeLogReplayCompleteResponse,
    RuntimeLogResponse,
    RuntimeRestartRequest,
    RuntimeStatusResponse,
    RuntimeTerminalResponse,
)


class _FakePeer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


# Restart interruption has different outcomes before and after server acceptance.
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


@pytest.mark.parametrize("result", ["succeeded", "failed"])
def test_terminal_result_wins_when_best_effort_ack_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    result: str,
) -> None:
    peer = _FakePeer()
    responses: Iterator[object] = iter(
        (
            RuntimeAcceptedResponse(operation="op-9"),
            RuntimeTerminalResponse(
                operation="op-9",
                result=result,
                message=None,
            ),
        )
    )
    sent_messages: list[object] = []

    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(
        client_module,
        "_receive_response",
        lambda _peer: next(responses),
    )

    def send(_peer: object, _message: object) -> None:
        sent_messages.append(_message)
        if isinstance(_message, RuntimeAckRequest):
            raise BrokenPipeError("synthetic ACK write failure")

    monkeypatch.setattr(client_module, "send_runtime_control_message", send)

    if result == "succeeded":
        assert restart_runtime(Path("unused")) == "op-9"
    else:
        with pytest.raises(
            RuntimeControlClientError,
            match="ComfyUI did not start after the restart",
        ):
            restart_runtime(Path("unused"))

    request_index = next(
        index
        for index, message in enumerate(sent_messages)
        if isinstance(message, RuntimeRestartRequest)
    )
    ack_index = next(
        index
        for index, message in enumerate(sent_messages)
        if isinstance(message, RuntimeAckRequest)
    )
    assert request_index < ack_index
    assert peer.closed is True


def test_terminal_result_wins_when_best_effort_ack_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = _FakePeer()
    responses: Iterator[object] = iter(
        (
            RuntimeAcceptedResponse(operation="op-9"),
            RuntimeTerminalResponse(
                operation="op-9",
                result="succeeded",
                message=None,
            ),
        )
    )
    sent_messages: list[object] = []

    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(
        client_module,
        "_receive_response",
        lambda _peer: next(responses),
    )

    def send(_peer: object, _message: object) -> None:
        sent_messages.append(_message)
        if isinstance(_message, RuntimeAckRequest):
            signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(client_module, "send_runtime_control_message", send)

    assert restart_runtime(Path("unused")) == "op-9"
    request_index = next(
        index
        for index, message in enumerate(sent_messages)
        if isinstance(message, RuntimeRestartRequest)
    )
    ack_index = next(
        index
        for index, message in enumerate(sent_messages)
        if isinstance(message, RuntimeAckRequest)
    )
    assert request_index < ack_index
    assert peer.closed is True


# Log payload is merged on stdout; diagnostics alone use stderr.
def test_logs_preserve_merged_binary_payload_and_separate_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = _FakePeer()
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    responses: Iterator[object] = iter(
        (
            RuntimeLogResponse.from_bytes(b"out\x00\xff"),
            RuntimeLogResponse.from_bytes(b"err\x80tail"),
            RuntimeLogDiagnosticResponse(message="retention warning"),
            RuntimeLogReplayCompleteResponse(complete=True),
        )
    )
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda _peer, _message: None)
    monkeypatch.setattr(
        client_module,
        "_receive_logs_response",
        lambda _peer: next(responses),
    )
    try:
        assert (
            read_runtime_logs(
                Path("unused"),
                stdout_fd=stdout_write,
                stderr_fd=stderr_write,
            )
            == 0
        )
    finally:
        os.close(stdout_write)
        os.close(stderr_write)

    assert os.read(stdout_read, 1024) == b"out\x00\xfferr\x80tail"
    assert os.read(stderr_read, 1024) == b"retention warning\n"
    os.close(stdout_read)
    os.close(stderr_read)
    assert peer.closed is True


def test_logs_local_output_failure_is_silent_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = _FakePeer()
    responses = iter((RuntimeLogResponse.from_bytes(b"payload"),))
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda _peer, _message: None)
    monkeypatch.setattr(
        client_module,
        "_receive_logs_response",
        lambda _peer: next(responses),
    )

    def fail_output(_fd: int, _data: bytes) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(client_module, "_write_all", fail_output)

    assert read_runtime_logs(Path("unused")) == 1
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
def test_logs_transport_failure_is_concise(
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
        read_runtime_logs(Path("unused"))

    assert peer.closed is True


@pytest.mark.parametrize(
    ("sig", "exit_code"),
    [
        (signal.SIGHUP, 129),
        (signal.SIGINT, 130),
        (signal.SIGTERM, 143),
    ],
)
def test_logs_signal_ends_only_the_local_client(
    monkeypatch: pytest.MonkeyPatch,
    sig: signal.Signals,
    exit_code: int,
) -> None:
    peer = _FakePeer()
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)

    def interrupt_send(_peer: object, _message: object) -> None:
        signal.raise_signal(sig)

    monkeypatch.setattr(client_module, "_send_message", interrupt_send)

    assert read_runtime_logs(Path("unused")) == exit_code
    assert peer.closed is True


@pytest.mark.parametrize(
    "response",
    [
        RuntimeErrorResponse(code="unavailable", message="logs unavailable"),
        RuntimeStatusResponse(
            state="running",
            phase=None,
            generation="gen-1",
            operation=None,
            last_restart=None,
        ),
    ],
)
def test_logs_rejects_typed_error_and_unexpected_response(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
) -> None:
    peer = _FakePeer()
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda _peer, _message: None)
    monkeypatch.setattr(
        client_module,
        "_receive_logs_response",
        lambda _peer: response,
    )

    with pytest.raises(RuntimeControlClientError):
        read_runtime_logs(Path("unused"))

    assert peer.closed is True


def test_local_output_write_retries_interruption_and_partial_progress() -> None:
    writes: list[bytes] = []
    interrupted = False
    partial = False

    def writer(fd: int, data: bytes | memoryview) -> int:
        nonlocal interrupted, partial
        assert fd == 42
        if not interrupted:
            interrupted = True
            raise InterruptedError
        chunk = bytes(data[:2])
        partial = partial or len(chunk) < len(data)
        writes.append(chunk)
        return len(chunk)

    _write_all(42, b"abcdef", writer=writer)

    assert interrupted is True
    assert partial is True
    assert b"".join(writes) == b"abcdef"


@pytest.mark.parametrize("follow", [False, True])
def test_logs_require_explicit_replay_result_before_eof(
    monkeypatch: pytest.MonkeyPatch, follow: bool
) -> None:
    peer = _FakePeer()
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda *_args: None)
    monkeypatch.setattr(client_module, "_receive_logs_response", lambda _peer: None)
    with pytest.raises(RuntimeControlClientError, match="without a log result"):
        read_runtime_logs(Path("unused"), follow=follow)
    assert peer.closed


@pytest.mark.parametrize("follow", [False, True])
def test_incomplete_diagnostic_cannot_be_overridden_by_success(
    monkeypatch: pytest.MonkeyPatch, follow: bool
) -> None:
    peer = _FakePeer()
    responses = iter(
        (
            RuntimeLogDiagnosticResponse(message="history gap", incomplete=True),
            RuntimeLogReplayCompleteResponse(complete=True),
        )
    )
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda *_args: None)
    monkeypatch.setattr(
        client_module, "_receive_logs_response", lambda _peer: next(responses)
    )
    monkeypatch.setattr(client_module, "_write_all", lambda *_args: None)
    assert read_runtime_logs(Path("unused"), follow=follow) == 1
    assert peer.closed


def test_follow_rejects_duplicate_replay_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = _FakePeer()
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda *_args: None)
    monkeypatch.setattr(
        client_module,
        "_receive_logs_response",
        lambda _peer: RuntimeLogReplayCompleteResponse(complete=True),
    )
    with pytest.raises(RuntimeControlClientError, match="invalid response sequence"):
        read_runtime_logs(Path("unused"), follow=True)
    assert peer.closed


@pytest.mark.parametrize("explicit_end", [False, True])
def test_follow_requires_explicit_end_after_replay(
    monkeypatch: pytest.MonkeyPatch, explicit_end: bool
) -> None:
    peer = _FakePeer()
    responses = iter(
        (
            RuntimeLogReplayCompleteResponse(complete=True),
            RuntimeLogEndResponse() if explicit_end else None,
        )
    )
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda *_args: None)
    monkeypatch.setattr(
        client_module, "_receive_logs_response", lambda _peer: next(responses)
    )
    if explicit_end:
        assert read_runtime_logs(Path("unused"), follow=True) == 0
    else:
        with pytest.raises(RuntimeControlClientError, match="without a log result"):
            read_runtime_logs(Path("unused"), follow=True)
    assert peer.closed


@pytest.mark.parametrize("follow", [False, True])
def test_logs_reject_end_before_replay(
    monkeypatch: pytest.MonkeyPatch, follow: bool
) -> None:
    peer = _FakePeer()
    monkeypatch.setattr(client_module, "connect_runtime_control", lambda _path: peer)
    monkeypatch.setattr(client_module, "_send_message", lambda *_args: None)
    monkeypatch.setattr(
        client_module, "_receive_logs_response", lambda _peer: RuntimeLogEndResponse()
    )
    with pytest.raises(RuntimeControlClientError, match="invalid response sequence"):
        read_runtime_logs(Path("unused"), follow=follow)
    assert peer.closed
