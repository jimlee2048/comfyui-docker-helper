"""Private same-UID endpoint and socket transport for the runtime owner."""

from __future__ import annotations

import errno
import os
import socket
import stat
import struct
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from comfyui_docker_helper.errors import ApplicationError

RUNTIME_CONTROL_DIRECTORY = Path("/run/cdh")
RUNTIME_CONTROL_SOCKET_PATH = RUNTIME_CONTROL_DIRECTORY / "runtime.sock"
RUNTIME_CONTROL_ACK_DRAIN_SECONDS = 0.5


class RuntimeControlEndpointError(ApplicationError):
    """A safe endpoint or peer-admission failure."""


@dataclass(frozen=True, slots=True)
class RuntimePeerCredentials:
    pid: int
    uid: int
    gid: int


type RuntimePeerCredentialReader = Callable[[socket.socket], RuntimePeerCredentials]
type RuntimeControlEndpointIdentity = tuple[int, int, int]


def read_runtime_peer_credentials(peer: socket.socket) -> RuntimePeerCredentials:
    """Read Linux credentials for one connected Unix-domain peer."""
    size = struct.calcsize("3i")
    raw = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    pid, uid, gid = struct.unpack("3i", raw)
    return RuntimePeerCredentials(pid=pid, uid=uid, gid=gid)


@dataclass(slots=True)
class RuntimeControlListener:
    """An identity-owned private Unix-domain listener."""

    socket: socket.socket
    path: Path
    owner_uid: int
    endpoint_identity: RuntimeControlEndpointIdentity
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
    endpoint_identity: RuntimeControlEndpointIdentity | None = None
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
            path.chmod(0o600)
            endpoint = _admit_stale_candidate(path, owner_uid=owner_uid)
            if endpoint is None:
                raise RuntimeControlEndpointError(
                    "The runtime control endpoint disappeared while being secured."
                )
            endpoint_identity = _endpoint_identity(endpoint)
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
    except BaseException:
        peer.close()
        raise


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
    if _endpoint_identity(confirmed) != _endpoint_identity(candidate):
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


def _endpoint_identity(endpoint: os.stat_result) -> RuntimeControlEndpointIdentity:
    return (endpoint.st_dev, endpoint.st_ino, endpoint.st_ctime_ns)


def _unlink_matching_endpoint(
    path: Path,
    identity: RuntimeControlEndpointIdentity,
) -> None:
    try:
        endpoint = path.lstat()
    except OSError:
        return
    if _endpoint_identity(endpoint) == identity:
        with suppress(OSError):
            path.unlink()
