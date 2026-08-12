"""Private runtime control server and client integration coverage."""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import pytest

from comfyui_docker_helper.container.runtime_control import (
    RuntimeAcceptedResponse,
    RuntimeAckRequest,
    RuntimeControlListener,
    RuntimeControlResponse,
    RuntimeErrorResponse,
    RuntimeFollowRequest,
    RuntimeLogResponse,
    RuntimePeerCredentials,
    RuntimeRestartRequest,
    RuntimeStatusRequest,
    RuntimeStatusResponse,
    RuntimeTerminalResponse,
    connect_runtime_control,
    open_runtime_control_listener,
    receive_runtime_control_response,
    send_runtime_control_message,
)
from comfyui_docker_helper.container.runtime_control_client import (
    RuntimeControlClientError,
    read_runtime_status,
    restart_runtime,
)
from comfyui_docker_helper.container.runtime_control_server import (
    RuntimeControlServer,
)
from comfyui_docker_helper.container.runtime_controller import RuntimeController
from comfyui_docker_helper.container.runtime_logging import (
    RUNTIME_LOG_MAX_FOLLOWERS,
    RuntimeLogChunk,
    RuntimeLogFollower,
    RuntimeLoggingBroker,
    RuntimeLoggingError,
)


class _UnavailableLoggingBroker:
    def follow(self) -> RuntimeLogFollower:
        raise RuntimeLoggingError("not used by this test")


def _server(
    listener: RuntimeControlListener,
    controller: RuntimeController,
) -> RuntimeControlServer:
    return RuntimeControlServer(
        listener,
        controller,
        _UnavailableLoggingBroker(),
    )


def _active_logging_broker() -> RuntimeLoggingBroker:
    broker = RuntimeLoggingBroker()
    broker._started = True
    return broker


def _endpoint(tmp_path: Path) -> Path:
    return tmp_path / "runtime-control" / "runtime.sock"


def _running_controller() -> RuntimeController:
    controller = RuntimeController()
    controller.begin_initial_admission()
    controller.mark_initial_generation_running()
    return controller


def _receive(client: socket.socket) -> RuntimeControlResponse:
    response = receive_runtime_control_response(client)
    assert response is not None
    return response


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition did not become true before the test deadline")
        wake.wait(0.01)


# Status and restart tests keep typed protocol fields and delivery order exact.
def test_status_observes_starting_then_running_snapshot(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    controller = RuntimeController()
    listener = open_runtime_control_listener(endpoint)

    with _server(listener, controller):
        starting = read_runtime_status(endpoint)
        controller.begin_initial_admission()
        controller.mark_initial_generation_running()
        running = read_runtime_status(endpoint)

    assert starting == RuntimeStatusResponse(
        state="starting",
        phase="admitting",
        generation=None,
        operation=None,
        last_restart=None,
    )
    assert running == RuntimeStatusResponse(
        state="running",
        phase=None,
        generation="gen-1",
        operation=None,
        last_restart=None,
    )


def test_restart_sends_accepted_before_terminal_and_drains_matching_ack(
    tmp_path: Path,
) -> None:
    endpoint = _endpoint(tmp_path)
    controller = _running_controller()
    listener = open_runtime_control_listener(endpoint)

    with _server(listener, controller):
        client = connect_runtime_control(endpoint)
        try:
            send_runtime_control_message(client, RuntimeRestartRequest())
            assert controller.wait(1.0) is True
            assert controller.accept_if_requested(accepted_at=1.0) is True
            assert controller.allocate_restart_successor() == "gen-2"
            controller.publish_restart_terminal("succeeded")

            assert _receive(client) == RuntimeAcceptedResponse(operation="op-1")
            assert _receive(client) == RuntimeTerminalResponse(
                operation="op-1",
                result="succeeded",
            )
            send_runtime_control_message(client, RuntimeAckRequest(operation="op-1"))

            assert controller.wait_for_terminal_delivery(1.0) is True
            assert controller.release_successful_restart() is True
        finally:
            client.close()


def test_preaccept_terminal_rejects_restart_without_invalid_request(
    tmp_path: Path,
) -> None:
    endpoint = _endpoint(tmp_path)
    controller = _running_controller()
    listener = open_runtime_control_listener(endpoint)

    with _server(listener, controller):
        client = connect_runtime_control(endpoint)
        try:
            send_runtime_control_message(client, RuntimeRestartRequest())
            assert controller.wait(1.0) is True
            controller.mark_generation_terminal("ComfyUI exited.")

            assert _receive(client) == RuntimeErrorResponse(
                code="unavailable",
                message="ComfyUI exited.",
            )
        finally:
            client.close()


def test_pending_disconnect_withdraws_but_accepted_disconnect_continues(
    tmp_path: Path,
) -> None:
    endpoint = _endpoint(tmp_path)
    controller = _running_controller()
    listener = open_runtime_control_listener(endpoint)

    with _server(listener, controller):
        pending = connect_runtime_control(endpoint)
        send_runtime_control_message(pending, RuntimeRestartRequest())
        assert controller.wait(1.0) is True
        pending.close()
        _wait_until(lambda: not controller.wait(0))
        assert controller.accept_if_requested(accepted_at=1.0) is False

        accepted = connect_runtime_control(endpoint)
        send_runtime_control_message(accepted, RuntimeRestartRequest())
        assert controller.wait(1.0) is True
        assert controller.accept_if_requested(accepted_at=2.0) is True
        accepted.close()
        controller.publish_restart_terminal("failed", message="successor failed")

        assert controller.wait_for_terminal_delivery(1.0) is True
        assert controller.snapshot().operation == "op-1"
        assert controller.snapshot().last_restart is not None
        assert controller.snapshot().last_restart.result == "failed"


def test_rejected_peer_and_malformed_frame_do_not_stop_accept_loop(
    tmp_path: Path,
) -> None:
    endpoint = _endpoint(tmp_path)
    credential_calls = 0

    def admit_after_first(_peer: socket.socket) -> RuntimePeerCredentials:
        nonlocal credential_calls
        credential_calls += 1
        uid = os.geteuid() + 1 if credential_calls == 1 else os.geteuid()
        return RuntimePeerCredentials(pid=1, uid=uid, gid=os.getegid())

    controller = RuntimeController()
    listener = open_runtime_control_listener(
        endpoint,
        peer_credential_reader=admit_after_first,
    )
    with _server(listener, controller):
        rejected = connect_runtime_control(endpoint)
        rejected.sendall(struct.pack(">I", 0))
        with suppress(ConnectionResetError):
            assert rejected.recv(1) == b""
        rejected.close()

        malformed = connect_runtime_control(endpoint)
        malformed.sendall(struct.pack(">I", 0))
        assert _receive(malformed) == RuntimeErrorResponse(
            code="invalid_request",
            message="The runtime control request is invalid.",
        )
        malformed.close()

        status = connect_runtime_control(endpoint)
        send_runtime_control_message(status, RuntimeStatusRequest())
        assert isinstance(_receive(status), RuntimeStatusResponse)
        status.close()


def test_restart_client_reports_success_failure_and_busy(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    controller = _running_controller()
    listener = open_runtime_control_listener(endpoint)

    def complete_success() -> None:
        assert controller.wait(1.0) is True
        assert controller.accept_if_requested(accepted_at=1.0) is True
        assert controller.allocate_restart_successor() == "gen-2"
        controller.publish_restart_terminal("succeeded")
        assert controller.release_successful_restart() is True

    with _server(listener, controller):
        driver = threading.Thread(target=complete_success)
        driver.start()
        assert restart_runtime(endpoint) == "op-1"
        driver.join(timeout=1.0)
        assert not driver.is_alive()

        def complete_failure() -> None:
            assert controller.wait(1.0) is True
            assert controller.accept_if_requested(accepted_at=2.0) is True
            assert controller.allocate_restart_successor() == "gen-3"
            controller.publish_restart_terminal(
                "failed", message="synthetic restart failure"
            )
            assert controller.wait_for_terminal_delivery(1.0) is True

        failure_driver = threading.Thread(target=complete_failure)
        failure_driver.start()
        with pytest.raises(
            RuntimeControlClientError, match="synthetic restart failure"
        ):
            restart_runtime(endpoint)
        failure_driver.join(timeout=1.0)
        assert not failure_driver.is_alive()

    busy_root = tmp_path / "busy"
    busy_root.mkdir()
    busy_endpoint = _endpoint(busy_root)
    busy_controller = RuntimeController()
    busy_listener = open_runtime_control_listener(busy_endpoint)
    with (
        _server(busy_listener, busy_controller),
        pytest.raises(RuntimeControlClientError, match="concurrent restart"),
    ):
        restart_runtime(busy_endpoint)


# Live log connections span restarts while limits and disconnects stay isolated.
def test_follow_streams_live_binary_frames_across_runtime_boundaries(
    tmp_path: Path,
) -> None:
    endpoint = _endpoint(tmp_path)
    controller = _running_controller()
    broker = _active_logging_broker()
    broker._publish(RuntimeLogChunk("stdout", b"before-subscription"))
    listener = open_runtime_control_listener(endpoint)

    with RuntimeControlServer(listener, controller, broker):
        client = connect_runtime_control(endpoint)
        try:
            send_runtime_control_message(client, RuntimeFollowRequest())
            _wait_until(lambda: len(broker._followers) == 1)
            broker._publish(RuntimeLogChunk("stdout", b"old\x00\xff"))
            assert _receive(client) == RuntimeLogResponse.from_bytes(
                "stdout", b"old\x00\xff"
            )

            submission = controller.submit_restart(delivery_expected=False)
            assert submission.disposition == "submitted"
            assert controller.accept_if_requested(accepted_at=1.0) is True
            assert controller.allocate_restart_successor() == "gen-2"
            controller.publish_restart_terminal("succeeded")
            assert controller.release_successful_restart() is True

            broker._publish(RuntimeLogChunk("stderr", b"new-generation"))
            assert _receive(client) == RuntimeLogResponse.from_bytes(
                "stderr", b"new-generation"
            )
        finally:
            client.close()


def test_follow_limit_is_busy_and_disconnected_slot_is_reused(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    controller = _running_controller()
    broker = _active_logging_broker()
    listener = open_runtime_control_listener(endpoint)
    followers: list[socket.socket] = []

    with RuntimeControlServer(listener, controller, broker):
        for _ in range(RUNTIME_LOG_MAX_FOLLOWERS):
            client = connect_runtime_control(endpoint)
            send_runtime_control_message(client, RuntimeFollowRequest())
            followers.append(client)
        _wait_until(lambda: len(broker._followers) == RUNTIME_LOG_MAX_FOLLOWERS)

        excess = connect_runtime_control(endpoint)
        send_runtime_control_message(excess, RuntimeFollowRequest())
        response = _receive(excess)
        assert isinstance(response, RuntimeErrorResponse)
        assert response.code == "busy"
        assert response.operation is None
        assert "live log" in response.message.lower()
        assert "limit" in response.message.lower()
        excess.close()

        followers.pop().close()
        _wait_until(lambda: len(broker._followers) == RUNTIME_LOG_MAX_FOLLOWERS - 1)
        replacement = connect_runtime_control(endpoint)
        send_runtime_control_message(replacement, RuntimeFollowRequest())
        followers.append(replacement)
        _wait_until(lambda: len(broker._followers) == RUNTIME_LOG_MAX_FOLLOWERS)

        broker._publish(RuntimeLogChunk("stdout", b"replacement"))
        assert _receive(replacement) == RuntimeLogResponse.from_bytes(
            "stdout", b"replacement"
        )

    for follower in followers:
        follower.close()
