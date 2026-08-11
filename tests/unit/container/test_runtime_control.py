"""Private runtime control protocol unit coverage."""

from __future__ import annotations

import json
import socket
import struct
import threading

import pytest

from comfyui_docker_helper.container import runtime_control as control_module
from comfyui_docker_helper.container.runtime_control import (
    RUNTIME_CONTROL_MAX_FRAME_BYTES,
    RUNTIME_CONTROL_MAX_PAYLOAD_BYTES,
    RuntimeAcceptedResponse,
    RuntimeAckRequest,
    RuntimeControlProtocolError,
    RuntimeErrorResponse,
    RuntimeFollowRequest,
    RuntimeLastRestart,
    RuntimeLogResponse,
    RuntimeRestartRequest,
    RuntimeStatusRequest,
    RuntimeStatusResponse,
    RuntimeTerminalResponse,
    connect_runtime_control,
    encode_runtime_control_frame,
    receive_runtime_control_request,
    receive_runtime_control_response,
)


@pytest.mark.parametrize(
    "message",
    [
        RuntimeRestartRequest(),
        RuntimeStatusRequest(),
        RuntimeFollowRequest(),
        RuntimeAckRequest(operation="op-1"),
    ],
)
def test_request_frames_round_trip(message: object) -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(encode_runtime_control_frame(message))  # type: ignore[arg-type]
        assert receive_runtime_control_request(receiver) == message
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize(
    "message",
    [
        RuntimeAcceptedResponse(operation="op-1"),
        RuntimeStatusResponse(
            state="restarting",
            phase="stopping_generation",
            generation="gen-1",
            operation="op-1",
            last_restart=RuntimeLastRestart(id="op-0", result="succeeded"),
        ),
        RuntimeLogResponse.from_bytes("stderr", b"\x00\xffpartial"),
        RuntimeTerminalResponse(operation="op-1", result="failed", message="failed"),
        RuntimeErrorResponse(code="busy", message="restart busy", operation="op-1"),
    ],
)
def test_response_frames_round_trip(message: object) -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(encode_runtime_control_frame(message))  # type: ignore[arg-type]
        assert receive_runtime_control_response(receiver) == message
    finally:
        sender.close()
        receiver.close()


def test_log_response_preserves_binary_bytes() -> None:
    message = RuntimeLogResponse.from_bytes("stdout", b"\x00\xffno-newline")

    assert message.as_bytes() == b"\x00\xffno-newline"


@pytest.mark.parametrize(
    ("document", "receive_response"),
    [
        ({"version": 2, "type": "restart"}, False),
        ({"version": 1, "type": "future"}, False),
        ({"version": 1, "type": "restart", "extra": "marker"}, False),
        ({"version": "1", "type": "restart"}, False),
        (
            {
                "version": 1,
                "type": "log",
                "stream": "stdout",
                "data": "not-base64!",
            },
            True,
        ),
    ],
)
def test_invalid_message_shapes_fail_content_free(
    document: dict[str, object],
    receive_response: bool,
) -> None:
    payload = json.dumps(document).encode()
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(struct.pack(">I", len(payload)) + payload)
        receive = (
            receive_runtime_control_response
            if receive_response
            else receive_runtime_control_request
        )
        with pytest.raises(RuntimeControlProtocolError) as raised:
            receive(receiver)
    finally:
        sender.close()
        receiver.close()

    assert raised.value.code == "invalid_message"
    assert payload.decode() not in str(raised.value)
    assert "validation" not in str(raised.value).lower()


def test_fragmented_frame_is_reassembled() -> None:
    frame = encode_runtime_control_frame(RuntimeAckRequest(operation="op-7"))
    sender, receiver = socket.socketpair()

    def send_fragments() -> None:
        try:
            for byte in frame:
                sender.sendall(bytes((byte,)))
        finally:
            sender.close()

    thread = threading.Thread(target=send_fragments)
    thread.start()
    try:
        assert receive_runtime_control_request(receiver) == RuntimeAckRequest(
            operation="op-7"
        )
    finally:
        receiver.close()
        thread.join(timeout=2)

    assert not thread.is_alive()


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (b"", None),
        (b"\x00\x00", "truncated_frame"),
        (struct.pack(">I", 3) + b"x", "truncated_frame"),
        (struct.pack(">I", 0), "empty_frame"),
    ],
)
def test_clean_eof_is_distinct_from_invalid_or_partial_frames(
    wire: bytes,
    expected: str | None,
) -> None:
    sender, receiver = socket.socketpair()
    sender.sendall(wire)
    sender.close()
    try:
        if expected is None:
            assert receive_runtime_control_request(receiver) is None
        else:
            with pytest.raises(RuntimeControlProtocolError) as raised:
                receive_runtime_control_request(receiver)
            assert raised.value.code == expected
    finally:
        receiver.close()


def test_exact_maximum_frame_is_accepted() -> None:
    empty = RuntimeErrorResponse(code="invalid_request", message="")
    overhead = len(encode_runtime_control_frame(empty)) - 4
    message = RuntimeErrorResponse(
        code="invalid_request",
        message="x" * (RUNTIME_CONTROL_MAX_PAYLOAD_BYTES - overhead),
    )
    frame = encode_runtime_control_frame(message)
    assert len(frame) == RUNTIME_CONTROL_MAX_FRAME_BYTES

    sender, receiver = socket.socketpair()
    try:
        sender.sendall(frame)
        assert receive_runtime_control_response(receiver) == message
    finally:
        sender.close()
        receiver.close()


def test_oversize_header_is_rejected_without_reading_payload() -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(struct.pack(">I", RUNTIME_CONTROL_MAX_PAYLOAD_BYTES + 1))
        with pytest.raises(RuntimeControlProtocolError) as raised:
            receive_runtime_control_request(receiver)
    finally:
        sender.close()
        receiver.close()

    assert raised.value.code == "frame_too_large"


def test_connect_closes_its_socket_when_interrupted_by_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SyntheticInterrupt(BaseException):
        pass

    class InterruptedSocket:
        def __init__(self) -> None:
            self.closed = False

        def connect(self, _path: str) -> None:
            raise SyntheticInterrupt

        def close(self) -> None:
            self.closed = True

    peer = InterruptedSocket()
    monkeypatch.setattr(control_module.socket, "socket", lambda *_args: peer)

    with pytest.raises(SyntheticInterrupt):
        connect_runtime_control()

    assert peer.closed is True
