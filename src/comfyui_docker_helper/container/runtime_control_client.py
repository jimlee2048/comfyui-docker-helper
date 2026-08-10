"""Image-internal clients for the private runtime controller."""

from __future__ import annotations

import signal
import socket
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Never

from comfyui_docker_helper.container.runtime_control import (
    RUNTIME_CONTROL_SOCKET_PATH,
    RuntimeAcceptedResponse,
    RuntimeAckRequest,
    RuntimeControlProtocolError,
    RuntimeControlResponse,
    RuntimeErrorResponse,
    RuntimeRestartRequest,
    RuntimeStatusRequest,
    RuntimeStatusResponse,
    RuntimeTerminalResponse,
    connect_runtime_control,
    receive_runtime_control_response,
    send_runtime_control_message,
)
from comfyui_docker_helper.errors import ApplicationError


class RuntimeControlClientError(ApplicationError):
    """A concise failure from an image-internal runtime client."""


class _RuntimeControlClientInterrupted(BaseException):
    def __init__(self, sig: signal.Signals, operation: str | None) -> None:
        self.signal = sig
        self.operation = operation
        super().__init__(sig.name)


def restart_runtime(
    path: Path = RUNTIME_CONTROL_SOCKET_PATH,
) -> str:
    """Request one complete restart and wait for its terminal result."""
    accepted_operation: str | None = None
    peer: socket.socket | None = None
    try:
        with _runtime_client_signal_handlers(
            lambda: accepted_operation,
        ):
            peer = connect_runtime_control(path)
            _send_message(peer, RuntimeRestartRequest())
            while True:
                response = _receive_response(peer)
                if isinstance(response, RuntimeErrorResponse):
                    if response.code == "busy":
                        active = (
                            ""
                            if response.operation is None
                            else f" ({response.operation})"
                        )
                        raise RuntimeControlClientError(f"{response.message}{active}")
                    raise RuntimeControlClientError(response.message)
                if isinstance(response, RuntimeAcceptedResponse):
                    if accepted_operation is not None:
                        raise RuntimeControlClientError(
                            "The runtime controller sent an invalid response sequence."
                        )
                    accepted_operation = response.operation
                    continue
                if isinstance(response, RuntimeTerminalResponse):
                    if (
                        accepted_operation is None
                        or response.operation != accepted_operation
                    ):
                        raise RuntimeControlClientError(
                            "The runtime controller sent an invalid response sequence."
                        )
                    _send_message(
                        peer,
                        RuntimeAckRequest(operation=response.operation),
                    )
                    if response.result == "failed":
                        detail = response.message or "The successor did not start."
                        raise RuntimeControlClientError(
                            f"Runtime restart {response.operation} failed: {detail}"
                        )
                    return response.operation
                raise RuntimeControlClientError(
                    "The runtime controller sent an unexpected response."
                )
    except _RuntimeControlClientInterrupted as interrupted:
        raise RuntimeControlClientError(
            _interruption_message(interrupted.operation),
            exit_code=128 + int(interrupted.signal),
        ) from None
    finally:
        if peer is not None:
            peer.close()


def read_runtime_status(
    path: Path = RUNTIME_CONTROL_SOCKET_PATH,
) -> RuntimeStatusResponse:
    """Read one immutable status snapshot from the active owner."""
    peer = connect_runtime_control(path)
    try:
        _send_message(peer, RuntimeStatusRequest())
        response = _receive_response(peer)
    finally:
        peer.close()
    if isinstance(response, RuntimeStatusResponse):
        return response
    if isinstance(response, RuntimeErrorResponse):
        raise RuntimeControlClientError(response.message)
    raise RuntimeControlClientError(
        "The runtime controller sent an unexpected response."
    )


def _receive_response(peer: socket.socket) -> RuntimeControlResponse:
    try:
        response = receive_runtime_control_response(peer)
    except RuntimeControlProtocolError as error:
        raise RuntimeControlClientError(
            "The runtime controller sent a malformed response."
        ) from error
    except OSError as error:
        raise RuntimeControlClientError(
            "The connection to the runtime controller was lost."
        ) from error
    if response is None:
        raise RuntimeControlClientError(
            "The runtime controller closed the connection without a result."
        )
    return response


def _send_message(
    peer: socket.socket,
    message: RuntimeRestartRequest | RuntimeStatusRequest | RuntimeAckRequest,
) -> None:
    try:
        send_runtime_control_message(peer, message)
    except OSError as error:
        raise RuntimeControlClientError(
            "The connection to the runtime controller was lost."
        ) from error


@contextmanager
def _runtime_client_signal_handlers(operation: Callable[[], str | None]):
    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
    }

    def interrupt(sig: signal.Signals, frame: FrameType | None) -> Never:
        del frame
        raise _RuntimeControlClientInterrupted(signal.Signals(sig), operation())

    try:
        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        yield
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


def _interruption_message(operation: str | None) -> str:
    if operation is None:
        return "Restart wait was interrupted before acceptance was confirmed."
    return f"Restart continues in the container: {operation}."
