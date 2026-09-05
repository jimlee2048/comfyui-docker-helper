"""Thin protocol adapter for the single-owner runtime controller."""

from __future__ import annotations

import select
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

from comfyui_docker_helper.container.runtime.control.protocol import (
    RuntimeAcceptedResponse,
    RuntimeAckRequest,
    RuntimeControlProtocolError,
    RuntimeErrorResponse,
    RuntimeLastRestart,
    RuntimeLogDiagnosticResponse,
    RuntimeLogEndResponse,
    RuntimeLogReplayCompleteResponse,
    RuntimeLogResponse,
    RuntimeLogsRequest,
    RuntimeRestartRequest,
    RuntimeStatusRequest,
    RuntimeStatusResponse,
    RuntimeTerminalResponse,
    receive_runtime_control_request,
    send_runtime_control_message,
)
from comfyui_docker_helper.container.runtime.control.transport import (
    RUNTIME_CONTROL_ACK_DRAIN_SECONDS,
    RuntimeControlEndpointError,
    RuntimeControlListener,
)
from comfyui_docker_helper.container.runtime.controller import (
    RuntimeController,
    RuntimeRestartTicket,
    RuntimeRestartTicketSnapshot,
)
from comfyui_docker_helper.container.runtime.logging import (
    LogQueryDiagnostic,
    LogReplayComplete,
    RuntimeLoggingError,
    RuntimeLoggingFollowerLimitError,
    RuntimeLogQuery,
)

_ACCEPT_POLL_SECONDS = 0.1
_TICKET_POLL_SECONDS = 0.05
_LOG_POLL_SECONDS = 0.1
_LOG_SEND_TIMEOUT_SECONDS = 0.1
_WIRE_MESSAGE_MAX_CHARS = 4096
_LOG_WIRE_CHUNK_BYTES = 16 * 1024
_MAX_CONTROL_PEERS = 16
_REQUEST_IDLE_TIMEOUT_SECONDS = 1.0


class RuntimeLogQuerySource(Protocol):
    def logs(
        self, *, tail: int | None = None, follow: bool = False
    ) -> RuntimeLogQuery: ...


class RuntimeControlServer:
    """Accept private peers without owning lifecycle policy."""

    def __init__(
        self,
        listener: RuntimeControlListener,
        controller: RuntimeController,
        logging_broker: RuntimeLogQuerySource,
    ) -> None:
        self._listener = listener
        self._controller = controller
        self._logging_broker = logging_broker
        self._stop = threading.Event()
        self._abort = threading.Event()
        self._close_deadline: float | None = None
        self._force_requested: Callable[[], bool] = lambda: False
        self._peers_lock = threading.Condition()
        self._peers: set[socket.socket] = set()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="cdh-runtime-control",
            daemon=True,
        )

    def start(self) -> None:
        self._listener.socket.settimeout(_ACCEPT_POLL_SECONDS)
        self._accept_thread.start()

    def stop_accepting(
        self,
        *,
        deadline: float | None = None,
        force_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._close_deadline = deadline
        self._force_requested = force_requested
        self._stop.set()
        self._listener.close()

    def close(
        self,
        *,
        deadline: float | None = None,
        force_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._close_deadline = (
            time.monotonic() + RUNTIME_CONTROL_ACK_DRAIN_SECONDS
            if deadline is None
            else deadline
        )
        self._force_requested = force_requested
        self.stop_accepting(
            deadline=self._close_deadline, force_requested=force_requested
        )
        with self._peers_lock:
            while self._peers and not force_requested():
                remaining = self._close_deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._peers_lock.wait(timeout=min(0.02, remaining))
            self._abort.set()
            peers = tuple(self._peers)
        # Workers alone send frames. Closing an unfinished connection cannot
        # manufacture a successful end when its delivery budget is exhausted.
        for peer in peers:
            with suppress(OSError):
                peer.shutdown(socket.SHUT_RDWR)
            peer.close()
        while self._accept_thread.is_alive() and not force_requested():
            remaining = self._close_deadline - time.monotonic()
            if remaining <= 0:
                break
            self._accept_thread.join(timeout=min(0.02, remaining))

    def __enter__(self) -> RuntimeControlServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                peer = self._listener.accept()
            except TimeoutError:
                continue
            except RuntimeControlEndpointError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            worker = threading.Thread(
                target=self._handle_peer,
                args=(peer,),
                name="cdh-runtime-control-peer",
                daemon=True,
            )
            with self._peers_lock:
                if self._stop.is_set():
                    peer.close()
                    return
                if len(self._peers) >= _MAX_CONTROL_PEERS:
                    peer.close()
                    continue
                self._peers.add(peer)
            worker.start()

    def _handle_peer(self, peer: socket.socket) -> None:
        ticket: RuntimeRestartTicket | None = None
        query: RuntimeLogQuery | None = None
        try:
            try:
                peer.settimeout(_REQUEST_IDLE_TIMEOUT_SECONDS)
                request = receive_runtime_control_request(peer)
            except RuntimeControlProtocolError:
                self._send_invalid_request(peer)
                return
            if request is None:
                return
            if isinstance(request, RuntimeStatusRequest):
                self._send_status(peer)
                return
            if isinstance(request, RuntimeRestartRequest):
                submission = self._controller.submit_restart()
                if submission.disposition == "busy":
                    send_runtime_control_message(
                        peer,
                        RuntimeErrorResponse(
                            code="busy",
                            message="A concurrent restart is already in progress.",
                            operation=submission.active_operation,
                        ),
                    )
                    return
                ticket = submission.ticket
                assert ticket is not None
                self._serve_restart(peer, ticket)
                return
            if isinstance(request, RuntimeLogsRequest):
                try:
                    query = self._logging_broker.logs(
                        tail=request.tail, follow=request.follow
                    )
                except RuntimeLoggingFollowerLimitError:
                    send_runtime_control_message(
                        peer,
                        RuntimeErrorResponse(
                            code="busy",
                            message="The log query connection limit has been reached.",
                        ),
                    )
                    return
                except RuntimeLoggingError:
                    send_runtime_control_message(
                        peer,
                        RuntimeErrorResponse(
                            code="unavailable",
                            message="Runtime logging is not available.",
                        ),
                    )
                    return
                self._serve_logs(peer, query)
                return
            if isinstance(request, RuntimeAckRequest):
                self._send_invalid_request(peer)
        except OSError:
            if ticket is not None:
                self._controller.withdraw_restart(ticket)
        finally:
            if ticket is not None:
                ticket.mark_delivery_complete()
            if query is not None:
                query.close()
            with self._peers_lock:
                self._peers.discard(peer)
                self._peers_lock.notify_all()
            peer.close()

    def _serve_logs(self, peer: socket.socket, query: RuntimeLogQuery) -> None:
        peer.settimeout(_LOG_SEND_TIMEOUT_SECONDS)
        for item in query.replay():
            if self._delivery_stopped() or self._peer_has_input_or_eof(peer):
                return
            if isinstance(item, bytes):
                self._send_log_bytes(peer, item)
            elif isinstance(item, LogQueryDiagnostic):
                self._send_log_diagnostic(peer, item)
            elif isinstance(item, LogReplayComplete):
                self._send_log_message(
                    peer, RuntimeLogReplayCompleteResponse(complete=item.complete)
                )
                if not item.complete or query.follower is None:
                    return
        follower = query.follower
        assert follower is not None
        while not self._delivery_stopped():
            if self._peer_has_input_or_eof(peer):
                return
            diagnostic = query.poll_diagnostic()
            if diagnostic is not None:
                self._send_log_diagnostic(peer, diagnostic)
            chunk = follower.receive(timeout=_LOG_POLL_SECONDS)
            if chunk is not None:
                self._send_log_bytes(peer, chunk.data)
                continue
            reason = follower.close_reason()
            if reason == "overflow":
                self._send_log_message(
                    peer,
                    RuntimeErrorResponse(
                        code="unavailable",
                        message=(
                            "This log connection could not keep up "
                            "and was disconnected."
                        ),
                    ),
                )
                return
            if reason == "broker_closed":
                diagnostic = query.poll_diagnostic()
                if diagnostic is not None:
                    self._send_log_diagnostic(peer, diagnostic)
                self._send_log_message(peer, RuntimeLogEndResponse())
                return
            if reason is not None:
                return

    def _send_log_bytes(self, peer: socket.socket, data: bytes) -> None:
        # Base64 expands payloads; history reads can be larger than one wire frame.
        for offset in range(0, len(data), _LOG_WIRE_CHUNK_BYTES):
            self._send_log_message(
                peer,
                RuntimeLogResponse.from_bytes(
                    data[offset : offset + _LOG_WIRE_CHUNK_BYTES]
                ),
            )

    def _send_log_diagnostic(
        self, peer: socket.socket, diagnostic: LogQueryDiagnostic
    ) -> None:
        self._send_log_message(
            peer,
            RuntimeLogDiagnosticResponse(
                message=diagnostic.message, incomplete=diagnostic.incomplete
            ),
        )

    def _delivery_stopped(self) -> bool:
        return (
            self._abort.is_set()
            or self._force_requested()
            or (
                self._close_deadline is not None
                and time.monotonic() >= self._close_deadline
            )
        )

    def _send_log_message(
        self,
        peer: socket.socket,
        message: RuntimeLogResponse
        | RuntimeLogDiagnosticResponse
        | RuntimeLogReplayCompleteResponse
        | RuntimeLogEndResponse
        | RuntimeErrorResponse,
    ) -> None:
        if self._delivery_stopped():
            raise TimeoutError("log delivery deadline exhausted")
        timeout = _LOG_SEND_TIMEOUT_SECONDS
        if self._close_deadline is not None:
            timeout = min(timeout, max(0.0, self._close_deadline - time.monotonic()))
        peer.settimeout(timeout)
        send_runtime_control_message(peer, message)

    def _serve_restart(
        self,
        peer: socket.socket,
        ticket: RuntimeRestartTicket,
    ) -> None:
        snapshot = ticket.snapshot()
        accepted_sent = False
        while True:
            if snapshot.operation is not None and not accepted_sent:
                send_runtime_control_message(
                    peer,
                    RuntimeAcceptedResponse(operation=snapshot.operation),
                )
                accepted_sent = True
            if snapshot.state in {"succeeded", "failed"}:
                assert snapshot.operation is not None
                send_runtime_control_message(
                    peer,
                    RuntimeTerminalResponse(
                        operation=snapshot.operation,
                        result=snapshot.state,
                        message=_bounded_wire_message(snapshot.message),
                    ),
                )
                self._wait_for_ack(peer, snapshot)
                return
            if snapshot.state == "rejected":
                send_runtime_control_message(
                    peer,
                    RuntimeErrorResponse(
                        code="unavailable",
                        message=(
                            _bounded_wire_message(snapshot.message)
                            or "The runtime cannot accept a restart request."
                        ),
                    ),
                )
                return
            if self._peer_has_input_or_eof(peer):
                self._controller.withdraw_restart(ticket)
                return
            snapshot = ticket.wait_for_change(
                snapshot.revision,
                timeout=_TICKET_POLL_SECONDS,
            )

    def _wait_for_ack(
        self,
        peer: socket.socket,
        terminal: RuntimeRestartTicketSnapshot,
    ) -> None:
        if self._controller.external_shutdown_snapshot().signal is not None:
            return
        previous_timeout = peer.gettimeout()
        try:
            peer.settimeout(RUNTIME_CONTROL_ACK_DRAIN_SECONDS)
            request = receive_runtime_control_request(peer)
            if not isinstance(request, RuntimeAckRequest):
                return
            if request.operation != terminal.operation:
                return
        except (OSError, RuntimeControlProtocolError):
            return
        finally:
            peer.settimeout(previous_timeout)

    def _send_status(self, peer: socket.socket) -> None:
        snapshot = self._controller.snapshot()
        last_restart = snapshot.last_restart
        send_runtime_control_message(
            peer,
            RuntimeStatusResponse(
                state=snapshot.state,
                phase=snapshot.phase,
                generation=snapshot.generation,
                operation=snapshot.operation,
                last_restart=(
                    None
                    if last_restart is None
                    else RuntimeLastRestart(
                        id=last_restart.id,
                        result=last_restart.result,
                    )
                ),
            ),
        )

    @staticmethod
    def _send_invalid_request(peer: socket.socket) -> None:
        with suppress(OSError):
            send_runtime_control_message(
                peer,
                RuntimeErrorResponse(
                    code="invalid_request",
                    message="The runtime control request is invalid.",
                ),
            )

    @staticmethod
    def _peer_has_input_or_eof(peer: socket.socket) -> bool:
        try:
            readable, _writable, _exceptional = select.select((peer,), (), (), 0)
        except ValueError:
            # Final bounded close can release this descriptor between polls.
            return True
        return bool(readable)


def _bounded_wire_message(message: str | None) -> str | None:
    if message is None or len(message) <= _WIRE_MESSAGE_MAX_CHARS:
        return message
    return f"{message[: _WIRE_MESSAGE_MAX_CHARS - 3]}..."
