"""Image-internal clients for the private runtime controller."""

from __future__ import annotations

import os
import signal
import socket
from collections.abc import Callable
from contextlib import contextmanager, suppress
from pathlib import Path
from types import FrameType
from typing import Never

from comfyui_docker_helper.cli_output.text import control_safe_text
from comfyui_docker_helper.container.runtime.control.protocol import (
    RuntimeAcceptedResponse,
    RuntimeAckRequest,
    RuntimeControlProtocolError,
    RuntimeControlResponse,
    RuntimeErrorResponse,
    RuntimeLogDiagnosticResponse,
    RuntimeLogEndResponse,
    RuntimeLogReplayCompleteResponse,
    RuntimeLogResponse,
    RuntimeLogsRequest,
    RuntimeRestartRequest,
    RuntimeStatusRequest,
    RuntimeStatusResponse,
    RuntimeTerminalResponse,
    receive_runtime_control_response,
    send_runtime_control_message,
)
from comfyui_docker_helper.container.runtime.control.transport import (
    RUNTIME_CONTROL_SOCKET_PATH,
    connect_runtime_control,
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
                            "The runtime service sent an invalid response sequence."
                        )
                    accepted_operation = response.operation
                    continue
                if isinstance(response, RuntimeTerminalResponse):
                    if (
                        accepted_operation is None
                        or response.operation != accepted_operation
                    ):
                        raise RuntimeControlClientError(
                            "The runtime service sent an invalid response sequence."
                        )
                    _send_terminal_ack_best_effort(peer, response.operation)
                    if response.result == "failed":
                        detail = (
                            response.message
                            or "ComfyUI did not start after the restart."
                        )
                        raise RuntimeControlClientError(
                            f"Runtime restart {response.operation} failed: {detail}"
                        )
                    return response.operation
                raise RuntimeControlClientError(
                    "The runtime service sent an unexpected response."
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
    """Read one immutable snapshot of the current runtime status."""
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
    raise RuntimeControlClientError("The runtime service sent an unexpected response.")


def read_runtime_logs(
    path: Path = RUNTIME_CONTROL_SOCKET_PATH,
    *,
    tail: int | None = None,
    follow: bool = False,
    stdout_fd: int = 1,
    stderr_fd: int = 2,
) -> int:
    """Replay retained merged bytes, optionally following subsequent output."""
    peer: socket.socket | None = None
    try:
        with _runtime_client_signal_handlers(
            lambda: None,
            handled_signals=(signal.SIGINT, signal.SIGTERM, signal.SIGHUP),
        ):
            peer = connect_runtime_control(path)
            _send_message(peer, RuntimeLogsRequest(tail=tail, follow=follow))
            replay_complete = False
            incomplete = False
            while True:
                response = _receive_logs_response(peer)
                if response is None:
                    raise RuntimeControlClientError(
                        "The runtime service closed the connection "
                        "without a log result."
                    )
                if isinstance(response, RuntimeLogEndResponse):
                    if not follow or not replay_complete:
                        raise RuntimeControlClientError(
                            "The runtime service sent an invalid response sequence."
                        )
                    return 0
                if isinstance(response, RuntimeLogResponse):
                    try:
                        _write_all(stdout_fd, response.as_bytes())
                    except OSError:
                        return 1
                    continue
                if isinstance(response, RuntimeLogDiagnosticResponse):
                    incomplete = incomplete or response.incomplete
                    try:
                        _write_all(
                            stderr_fd,
                            (control_safe_text(response.message) + "\n").encode(
                                "utf-8"
                            ),
                        )
                    except OSError:
                        return 1
                    if replay_complete and incomplete:
                        return 1
                    continue
                if isinstance(response, RuntimeLogReplayCompleteResponse):
                    if replay_complete:
                        raise RuntimeControlClientError(
                            "The runtime service sent an invalid response sequence."
                        )
                    if incomplete or not response.complete:
                        return 1
                    if not follow:
                        return 0
                    replay_complete = True
                    continue
                if isinstance(response, RuntimeErrorResponse):
                    raise RuntimeControlClientError(response.message)
                raise RuntimeControlClientError(
                    "The runtime service sent an unexpected response."
                )
    except _RuntimeControlClientInterrupted as interrupted:
        return 128 + int(interrupted.signal)
    finally:
        if peer is not None:
            peer.close()


def _receive_response(peer: socket.socket) -> RuntimeControlResponse:
    try:
        response = receive_runtime_control_response(peer)
    except RuntimeControlProtocolError as error:
        raise RuntimeControlClientError(
            "The runtime service sent a malformed response."
        ) from error
    except OSError as error:
        raise RuntimeControlClientError(
            "The connection to the runtime service was lost."
        ) from error
    if response is None:
        raise RuntimeControlClientError(
            "The runtime service closed the connection without a result."
        )
    return response


def _receive_logs_response(
    peer: socket.socket,
) -> RuntimeControlResponse | None:
    try:
        return receive_runtime_control_response(peer)
    except RuntimeControlProtocolError as error:
        raise RuntimeControlClientError(
            "The runtime service sent a malformed response."
        ) from error
    except OSError as error:
        raise RuntimeControlClientError(
            "The connection to the runtime service was lost."
        ) from error


def _send_message(
    peer: socket.socket,
    message: (
        RuntimeRestartRequest
        | RuntimeStatusRequest
        | RuntimeLogsRequest
        | RuntimeAckRequest
    ),
) -> None:
    try:
        send_runtime_control_message(peer, message)
    except OSError as error:
        raise RuntimeControlClientError(
            "The connection to the runtime service was lost."
        ) from error


def _send_terminal_ack_best_effort(peer: socket.socket, operation: str) -> None:
    with suppress(OSError, _RuntimeControlClientInterrupted):
        send_runtime_control_message(
            peer,
            RuntimeAckRequest(operation=operation),
        )


@contextmanager
def _runtime_client_signal_handlers(
    operation: Callable[[], str | None],
    *,
    handled_signals: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM),
):
    previous_handlers = {sig: signal.getsignal(sig) for sig in handled_signals}

    def interrupt(sig: signal.Signals, frame: FrameType | None) -> Never:
        del frame
        raise _RuntimeControlClientInterrupted(signal.Signals(sig), operation())

    try:
        for sig in handled_signals:
            signal.signal(sig, interrupt)
        yield
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


def _interruption_message(operation: str | None) -> str:
    if operation is None:
        return "Restart wait was interrupted before acceptance was confirmed."
    return f"Restart continues in the container: {operation}."


def _write_all(
    fd: int,
    data: bytes,
    *,
    writer: Callable[[int, bytes | memoryview], int] = os.write,
) -> None:
    remaining = memoryview(data)
    while remaining:
        try:
            written = writer(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("local output descriptor made no write progress")
        remaining = remaining[written:]
