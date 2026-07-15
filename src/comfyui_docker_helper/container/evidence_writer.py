"""Exclusive evidence-file creation for application-owned build records."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_AT_EMPTY_PATH = 0x1000
_O_TMPFILE = getattr(os, "O_TMPFILE", None)


class ApplicationEvidenceError(Exception):
    """An application evidence file could not be created safely."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


def _load_linkat():
    try:
        function = ctypes.CDLL(None, use_errno=True).linkat
    except (AttributeError, OSError) as error:  # pragma: no cover - Linux owns it.
        raise ApplicationEvidenceError(
            "linkat(AT_EMPTY_PATH) is unavailable on this Linux runtime"
        ) from error
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    )
    function.restype = ctypes.c_int
    return function


def write_application_evidence(
    path: Path,
    content: bytes,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    """Publish one durable read-only evidence inode without a named temp file."""
    parent_fd, parent_identity = _open_bound_parent(
        path.parent,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    try:
        descriptor = _open_anonymous_file(parent_fd)
        try:
            stream = os.fdopen(descriptor, "w+b")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            identity = _identity(os.fstat(stream.fileno()))
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fchown(stream.fileno(), owner_uid, owner_gid)
            _verify_open_file(stream, identity, content, owner_uid, owner_gid)
            os.fsync(stream.fileno())
            _verify_parent_binding(path.parent, parent_fd, parent_identity)
            _publish_anonymous_file(stream.fileno(), parent_fd, path.name)
            _verify_published_target(parent_fd, path.name, identity)
            _verify_open_file(stream, identity, content, owner_uid, owner_gid)
            _verify_published_target(parent_fd, path.name, identity)
            _verify_parent_binding(path.parent, parent_fd, parent_identity)
            os.fsync(parent_fd)
            _verify_published_target(parent_fd, path.name, identity)
            _verify_parent_binding(path.parent, parent_fd, parent_identity)
    except ApplicationEvidenceError:
        raise
    except OSError as error:
        raise ApplicationEvidenceError(
            f"application evidence failed: [errno {error.errno}] {error.strerror}"
        ) from error
    finally:
        os.close(parent_fd)


def _open_bound_parent(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> tuple[int, _FileIdentity]:
    try:
        path_metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ApplicationEvidenceError(
            "application evidence parent is unavailable"
        ) from error
    if (
        path.is_symlink()
        or not stat.S_ISDIR(path_metadata.st_mode)
        or resolved != path
        or path_metadata.st_uid != owner_uid
        or path_metadata.st_gid != owner_gid
    ):
        raise ApplicationEvidenceError(
            "application evidence parent must be one real owned directory"
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise ApplicationEvidenceError(
            f"application evidence parent could not be opened: "
            f"[errno {error.errno}] {error.strerror}"
        ) from error
    identity = _identity(path_metadata)
    try:
        _verify_parent_binding(path, descriptor, identity)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identity


def _open_anonymous_file(parent_fd: int) -> int:
    if _O_TMPFILE is None:
        raise ApplicationEvidenceError(
            "O_TMPFILE is unavailable on this Linux Python runtime"
        )
    try:
        return os.open(
            ".",
            os.O_RDWR | os.O_CLOEXEC | _O_TMPFILE,
            0o600,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise ApplicationEvidenceError(
            "O_TMPFILE is unsupported for the application evidence directory: "
            f"[errno {error.errno}] {error.strerror}"
        ) from error


def _publish_anonymous_file(file_fd: int, parent_fd: int, name: str) -> None:
    if not name or name in {".", ".."} or os.sep in name:
        raise ApplicationEvidenceError("application evidence target name is invalid")
    linkat = _load_linkat()
    ctypes.set_errno(0)
    result = linkat(
        file_fd,
        b"",
        parent_fd,
        os.fsencode(name),
        _AT_EMPTY_PATH,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ApplicationEvidenceError("application evidence target already exists")
    if error_number in {
        errno.EINVAL,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        errno.EPERM,
    }:
        raise ApplicationEvidenceError(
            "linkat(AT_EMPTY_PATH) is unsupported for application evidence: "
            f"[errno {error_number}] {os.strerror(error_number)}"
        )
    raise ApplicationEvidenceError(
        "application evidence publication failed: "
        f"[errno {error_number}] {os.strerror(error_number)}"
    )


def _verify_open_file(
    stream: BinaryIO,
    identity: _FileIdentity,
    content: bytes,
    owner_uid: int,
    owner_gid: int,
) -> None:
    metadata = os.fstat(stream.fileno())
    stream.seek(0)
    observed = stream.read(len(content) + 1)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _identity(metadata) != identity
        or metadata.st_uid != owner_uid
        or metadata.st_gid != owner_gid
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or observed != content
    ):
        raise ApplicationEvidenceError("application evidence verification failed")


def _verify_published_target(
    parent_fd: int,
    name: str,
    identity: _FileIdentity,
) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise ApplicationEvidenceError(
            "application evidence target identity changed"
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or _identity(metadata) != identity:
        raise ApplicationEvidenceError("application evidence target identity changed")


def _verify_parent_binding(
    path: Path,
    descriptor: int,
    identity: _FileIdentity,
) -> None:
    try:
        path_metadata = path.lstat()
        descriptor_metadata = os.fstat(descriptor)
    except OSError as error:
        raise ApplicationEvidenceError(
            "application evidence parent identity changed"
        ) from error
    if (
        path.is_symlink()
        or not stat.S_ISDIR(path_metadata.st_mode)
        or not stat.S_ISDIR(descriptor_metadata.st_mode)
        or _identity(path_metadata) != identity
        or _identity(descriptor_metadata) != identity
    ):
        raise ApplicationEvidenceError("application evidence parent identity changed")


def _identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(metadata.st_dev, metadata.st_ino)
