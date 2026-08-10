"""Runtime control client result and interruption coverage."""

from __future__ import annotations

import signal
import socket
from pathlib import Path

import pytest

from comfyui_docker_helper.container import runtime_control_client as client_module
from comfyui_docker_helper.container.runtime_control import RuntimeAcceptedResponse
from comfyui_docker_helper.container.runtime_control_client import (
    RuntimeControlClientError,
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
