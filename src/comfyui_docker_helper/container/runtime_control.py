"""Private same-UID control transport for the container runtime owner."""

from __future__ import annotations

import base64
import binascii
import errno
import json
import os
import socket
import stat
import struct
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from comfyui_docker_helper.config.model_base import ConfigModel
from comfyui_docker_helper.errors import ApplicationError

RUNTIME_CONTROL_DIRECTORY = Path("/run/cdh")
RUNTIME_CONTROL_SOCKET_PATH = RUNTIME_CONTROL_DIRECTORY / "runtime.sock"
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
    code: Literal["invalid_request", "busy"]
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


class RuntimeControlEndpointError(ApplicationError):
    """A safe endpoint or peer-admission failure."""


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


@dataclass(frozen=True, slots=True)
class RuntimePeerCredentials:
    pid: int
    uid: int
    gid: int


type RuntimePeerCredentialReader = Callable[[socket.socket], RuntimePeerCredentials]


def read_runtime_peer_credentials(peer: socket.socket) -> RuntimePeerCredentials:
    """Read Linux credentials for one connected Unix-domain peer."""
    size = struct.calcsize("3i")
    raw = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    pid, uid, gid = struct.unpack("3i", raw)
    return RuntimePeerCredentials(pid=pid, uid=uid, gid=gid)


@dataclass(slots=True)
class RuntimeControlListener:
    """An inode-owned private Unix-domain listener."""

    socket: socket.socket
    path: Path
    owner_uid: int
    endpoint_identity: tuple[int, int]
    peer_credential_reader: RuntimePeerCredentialReader = read_runtime_peer_credentials

    def accept(self) -> socket.socket:
        peer, _address = self.socket.accept()
        try:
            try:
                credentials = self.peer_credential_reader(peer)
            except OSError as error:
                raise RuntimeControlEndpointError(
                    "Runtime control peer credentials could not be verified."
                ) from error
            if credentials.uid != self.owner_uid:
                raise RuntimeControlEndpointError(
                    "Runtime control peer does not match the runtime owner."
                )
            return peer
        except BaseException:
            peer.close()
            raise

    def close(self) -> None:
        self.socket.close()
        _unlink_matching_endpoint(self.path, self.endpoint_identity)

    def __enter__(self) -> RuntimeControlListener:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_runtime_control_listener(
    path: Path = RUNTIME_CONTROL_SOCKET_PATH,
    *,
    peer_credential_reader: RuntimePeerCredentialReader = read_runtime_peer_credentials,
) -> RuntimeControlListener:
    """Create the sole private listener, replacing one proven stale socket."""
    owner_uid = os.geteuid()
    _prepare_control_directory(path.parent, owner_uid=owner_uid)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint_identity: tuple[int, int] | None = None
    try:
        try:
            listener.bind(os.fspath(path))
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise RuntimeControlEndpointError(
                    "Could not bind the runtime control endpoint."
                ) from error
            _recover_stale_socket(path, owner_uid=owner_uid)
            try:
                listener.bind(os.fspath(path))
            except OSError as retry_error:
                raise RuntimeControlEndpointError(
                    "Could not bind the runtime control endpoint."
                ) from retry_error
        try:
            endpoint = _admit_stale_candidate(path, owner_uid=owner_uid)
            if endpoint is None:
                raise RuntimeControlEndpointError(
                    "The runtime control endpoint disappeared after bind."
                )
            endpoint_identity = (endpoint.st_dev, endpoint.st_ino)
            path.chmod(0o600)
            listener.listen()
        except OSError as error:
            raise RuntimeControlEndpointError(
                "Could not secure the runtime control endpoint."
            ) from error
        return RuntimeControlListener(
            socket=listener,
            path=path,
            owner_uid=owner_uid,
            endpoint_identity=endpoint_identity,
            peer_credential_reader=peer_credential_reader,
        )
    except BaseException:
        listener.close()
        if endpoint_identity is not None:
            _unlink_matching_endpoint(path, endpoint_identity)
        raise


def connect_runtime_control(
    path: Path = RUNTIME_CONTROL_SOCKET_PATH,
) -> socket.socket:
    """Connect to the existing owner without any lifecycle fallback."""
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer.connect(os.fspath(path))
        return peer
    except OSError as error:
        peer.close()
        raise RuntimeControlEndpointError(
            "The container runtime controller is not available."
        ) from error


def _prepare_control_directory(path: Path, *, owner_uid: int) -> None:
    try:
        path.mkdir(mode=0o700)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise RuntimeControlEndpointError(
                "The runtime control directory could not be created."
            ) from error
    try:
        directory = path.lstat()
    except OSError as error:
        raise RuntimeControlEndpointError(
            "The runtime control directory is not available."
        ) from error
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner_uid:
        raise RuntimeControlEndpointError("The runtime control directory is unsafe.")
    try:
        path.chmod(0o700)
    except OSError as error:
        raise RuntimeControlEndpointError(
            "The runtime control directory cannot be secured."
        ) from error


def _recover_stale_socket(path: Path, *, owner_uid: int) -> None:
    candidate = _admit_stale_candidate(path, owner_uid=owner_uid)
    if candidate is None:
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(os.fspath(path))
    except OSError as error:
        if error.errno == errno.ENOENT:
            return
        if error.errno != errno.ECONNREFUSED:
            raise RuntimeControlEndpointError(
                "The runtime control endpoint is occupied."
            ) from error
    else:
        raise RuntimeControlEndpointError(
            "Another runtime controller already owns the endpoint."
        )
    finally:
        probe.close()

    confirmed = _admit_stale_candidate(path, owner_uid=owner_uid)
    if confirmed is None:
        return
    if (confirmed.st_dev, confirmed.st_ino) != (candidate.st_dev, candidate.st_ino):
        raise RuntimeControlEndpointError(
            "The runtime control endpoint changed during stale recovery."
        )
    try:
        path.unlink()
    except OSError as error:
        raise RuntimeControlEndpointError(
            "The stale runtime control endpoint could not be removed."
        ) from error


def _admit_stale_candidate(
    path: Path,
    *,
    owner_uid: int,
) -> os.stat_result | None:
    try:
        candidate = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeControlEndpointError(
            "The runtime control endpoint cannot be inspected safely."
        ) from error
    if not stat.S_ISSOCK(candidate.st_mode) or candidate.st_uid != owner_uid:
        raise RuntimeControlEndpointError("The runtime control endpoint is unsafe.")
    return candidate


def _unlink_matching_endpoint(path: Path, identity: tuple[int, int]) -> None:
    try:
        endpoint = path.lstat()
    except OSError:
        return
    if (endpoint.st_dev, endpoint.st_ino) == identity:
        with suppress(OSError):
            path.unlink()
