"""Strict bounded messages and framing for the private runtime control protocol."""

from __future__ import annotations

import base64
import binascii
import json
import socket
import struct
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from comfyui_docker_helper.config.model_base import ConfigModel

RUNTIME_CONTROL_PROTOCOL_VERSION = 1
RUNTIME_CONTROL_MAX_FRAME_BYTES = 64 * 1024
RUNTIME_CONTROL_FRAME_HEADER_BYTES = 4
RUNTIME_CONTROL_MAX_PAYLOAD_BYTES = (
    RUNTIME_CONTROL_MAX_FRAME_BYTES - RUNTIME_CONTROL_FRAME_HEADER_BYTES
)

type RuntimeControllerState = Literal[
    "starting",
    "running",
    "restarting",
    "stopping",
]
type RuntimeControllerPhase = Literal[
    "admitting",
    "stopping_generation",
    "starting_generation",
    "finalizing",
]
type RuntimeRestartResult = Literal["succeeded", "failed"]


class _RuntimeControlModel(ConfigModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_default=True,
        frozen=True,
    )


class _RuntimeControlMessage(_RuntimeControlModel):
    version: Literal[1] = RUNTIME_CONTROL_PROTOCOL_VERSION


class RuntimeRestartRequest(_RuntimeControlMessage):
    type: Literal["restart"] = "restart"


class RuntimeStatusRequest(_RuntimeControlMessage):
    type: Literal["status"] = "status"


class RuntimeFollowRequest(_RuntimeControlMessage):
    type: Literal["follow"] = "follow"


class RuntimeAckRequest(_RuntimeControlMessage):
    type: Literal["ack"] = "ack"
    operation: str


class RuntimeAcceptedResponse(_RuntimeControlMessage):
    type: Literal["accepted"] = "accepted"
    operation: str


class RuntimeLastRestart(_RuntimeControlModel):
    id: str
    result: RuntimeRestartResult


class RuntimeStatusResponse(_RuntimeControlMessage):
    type: Literal["status"] = "status"
    state: RuntimeControllerState
    phase: RuntimeControllerPhase | None
    generation: str | None
    operation: str | None
    last_restart: RuntimeLastRestart | None


class RuntimeLogResponse(_RuntimeControlMessage):
    type: Literal["log"] = "log"
    stream: Literal["stdout", "stderr"]
    data: str

    @field_validator("data")
    @classmethod
    def validate_base64_data(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("log data must be valid base64") from error
        return value

    @classmethod
    def from_bytes(
        cls,
        stream: Literal["stdout", "stderr"],
        data: bytes,
    ) -> RuntimeLogResponse:
        return cls(
            stream=stream,
            data=base64.b64encode(data).decode("ascii"),
        )

    def as_bytes(self) -> bytes:
        return base64.b64decode(self.data, validate=True)


class RuntimeTerminalResponse(_RuntimeControlMessage):
    type: Literal["terminal"] = "terminal"
    operation: str
    result: RuntimeRestartResult
    message: str | None = None


class RuntimeErrorResponse(_RuntimeControlMessage):
    type: Literal["error"] = "error"
    code: Literal["invalid_request", "busy", "unavailable"]
    message: str
    operation: str | None = None


type RuntimeControlRequest = Annotated[
    RuntimeRestartRequest
    | RuntimeStatusRequest
    | RuntimeFollowRequest
    | RuntimeAckRequest,
    Field(discriminator="type"),
]
type RuntimeControlResponse = Annotated[
    RuntimeAcceptedResponse
    | RuntimeStatusResponse
    | RuntimeLogResponse
    | RuntimeTerminalResponse
    | RuntimeErrorResponse,
    Field(discriminator="type"),
]
type RuntimeControlMessage = RuntimeControlRequest | RuntimeControlResponse

_REQUEST_ADAPTER = TypeAdapter(RuntimeControlRequest)
_RESPONSE_ADAPTER = TypeAdapter(RuntimeControlResponse)

type RuntimeControlProtocolErrorCode = Literal[
    "truncated_frame",
    "empty_frame",
    "frame_too_large",
    "invalid_message",
]


class RuntimeControlProtocolError(ValueError):
    """A content-free failure scoped to one control connection."""

    def __init__(self, code: RuntimeControlProtocolErrorCode) -> None:
        self.code = code
        super().__init__(f"Runtime control protocol failed ({code})")


def encode_runtime_control_frame(message: RuntimeControlMessage) -> bytes:
    """Encode one strict typed message with its bounded length envelope."""
    payload = json.dumps(
        message.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not payload:
        raise RuntimeControlProtocolError("empty_frame")
    if len(payload) > RUNTIME_CONTROL_MAX_PAYLOAD_BYTES:
        raise RuntimeControlProtocolError("frame_too_large")
    return struct.pack(">I", len(payload)) + payload


def receive_runtime_control_request(
    peer: socket.socket,
) -> RuntimeControlRequest | None:
    """Receive one request, returning None only for clean pre-frame EOF."""
    payload = _receive_payload(peer)
    if payload is None:
        return None
    return _validate_message(_REQUEST_ADAPTER, payload)


def receive_runtime_control_response(
    peer: socket.socket,
) -> RuntimeControlResponse | None:
    """Receive one response, returning None only for clean pre-frame EOF."""
    payload = _receive_payload(peer)
    if payload is None:
        return None
    return _validate_message(_RESPONSE_ADAPTER, payload)


def send_runtime_control_message(
    peer: socket.socket,
    message: RuntimeControlMessage,
) -> None:
    """Send one complete framed message."""
    peer.sendall(encode_runtime_control_frame(message))


def _receive_payload(peer: socket.socket) -> bytes | None:
    header = _receive_exact(peer, RUNTIME_CONTROL_FRAME_HEADER_BYTES)
    if header is None:
        return None
    length = struct.unpack(">I", header)[0]
    if length == 0:
        raise RuntimeControlProtocolError("empty_frame")
    if length > RUNTIME_CONTROL_MAX_PAYLOAD_BYTES:
        raise RuntimeControlProtocolError("frame_too_large")
    payload = _receive_exact(peer, length)
    if payload is None:
        raise RuntimeControlProtocolError("truncated_frame")
    return payload


def _receive_exact(peer: socket.socket, length: int) -> bytes | None:
    chunks: list[bytes] = []
    received = 0
    while received < length:
        chunk = peer.recv(length - received)
        if not chunk:
            if received == 0:
                return None
            raise RuntimeControlProtocolError("truncated_frame")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def _validate_message[MessageT](
    adapter: TypeAdapter[MessageT],
    payload: bytes,
) -> MessageT:
    try:
        return adapter.validate_json(payload, strict=True)
    except (ValidationError, ValueError) as error:
        raise RuntimeControlProtocolError("invalid_message") from error
