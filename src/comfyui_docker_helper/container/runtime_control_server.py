"""Thin protocol adapter for the single-owner runtime controller."""

from __future__ import annotations

import select
import socket
import threading
from contextlib import suppress

from comfyui_docker_helper.container.runtime_control import (
    RUNTIME_CONTROL_ACK_DRAIN_SECONDS,
    RuntimeAcceptedResponse,
    RuntimeAckRequest,
    RuntimeControlEndpointError,
    RuntimeControlListener,
    RuntimeControlProtocolError,
    RuntimeErrorResponse,
    RuntimeFollowRequest,
    RuntimeLastRestart,
    RuntimeLogResponse,
    RuntimeRestartRequest,
    RuntimeStatusRequest,
    RuntimeStatusResponse,
    RuntimeTerminalResponse,
    receive_runtime_control_request,
    send_runtime_control_message,
)
from comfyui_docker_helper.container.runtime_controller import (
    RuntimeController,
    RuntimeRestartTicket,
    RuntimeRestartTicketSnapshot,
)
from comfyui_docker_helper.container.runtime_logging import (
    RuntimeLogFollower,
    RuntimeLogFollowerSource,
    RuntimeLoggingError,
    RuntimeLoggingFollowerLimitError,
)

_ACCEPT_POLL_SECONDS = 0.1
_TICKET_POLL_SECONDS = 0.05
_FOLLOW_POLL_SECONDS = 0.1
_FOLLOW_SEND_TIMEOUT_SECONDS = 0.1
_WIRE_MESSAGE_MAX_CHARS = 4096


class RuntimeControlServer:
    """Accept private peers without owning lifecycle policy."""

    def __init__(
        self,
        listener: RuntimeControlListener,
        controller: RuntimeController,
        logging_broker: RuntimeLogFollowerSource,
    ) -> None:
        self._listener = listener
        self._controller = controller
        self._logging_broker = logging_broker
        self._stop = threading.Event()
        self._peers_lock = threading.Lock()
        self._peers: set[socket.socket] = set()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="cdh-runtime-control",
            daemon=True,
        )

    def start(self) -> None:
        self._listener.socket.settimeout(_ACCEPT_POLL_SECONDS)
        self._accept_thread.start()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        with self._peers_lock:
            peers = tuple(self._peers)
        for peer in peers:
            with suppress(OSError):
                peer.shutdown(socket.SHUT_RDWR)
            peer.close()
        self._accept_thread.join(timeout=RUNTIME_CONTROL_ACK_DRAIN_SECONDS)

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
                self._peers.add(peer)
            worker.start()

    def _handle_peer(self, peer: socket.socket) -> None:
        ticket: RuntimeRestartTicket | None = None
        follower: RuntimeLogFollower | None = None
        try:
            try:
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
            if isinstance(request, RuntimeFollowRequest):
                try:
                    follower = self._logging_broker.follow()
                except RuntimeLoggingFollowerLimitError:
                    send_runtime_control_message(
                        peer,
                        RuntimeErrorResponse(
                            code="busy",
                            message="The live log connection limit has been reached.",
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
                self._serve_follow(peer, follower)
                return
            if isinstance(request, RuntimeAckRequest):
                self._send_invalid_request(peer)
        except OSError:
            if ticket is not None:
                self._controller.withdraw_restart(ticket)
        finally:
            if ticket is not None:
                ticket.mark_delivery_complete()
            if follower is not None:
                follower.close()
            with self._peers_lock:
                self._peers.discard(peer)
            peer.close()

    def _serve_follow(
        self,
        peer: socket.socket,
        follower: RuntimeLogFollower,
    ) -> None:
        peer.settimeout(_FOLLOW_SEND_TIMEOUT_SECONDS)
        while not self._stop.is_set():
            if self._peer_has_input_or_eof(peer):
                return
            chunk = follower.receive(timeout=_FOLLOW_POLL_SECONDS)
            if chunk is not None:
                send_runtime_control_message(
                    peer,
                    RuntimeLogResponse.from_bytes(chunk.stream, chunk.data),
                )
                continue
            reason = follower.close_reason()
            if reason == "overflow":
                with suppress(OSError):
                    send_runtime_control_message(
                        peer,
                        RuntimeErrorResponse(
                            code="unavailable",
                            message=(
                                "This live log connection could not keep up and "
                                "was disconnected."
                            ),
                        ),
                    )
                return
            if reason is not None:
                return

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
        readable, _writable, _exceptional = select.select((peer,), (), (), 0)
        return bool(readable)


def _bounded_wire_message(message: str | None) -> str | None:
    if message is None or len(message) <= _WIRE_MESSAGE_MAX_CHARS:
        return message
    return f"{message[: _WIRE_MESSAGE_MAX_CHARS - 3]}..."
