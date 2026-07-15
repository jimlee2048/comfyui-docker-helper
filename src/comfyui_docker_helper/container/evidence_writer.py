"""Exclusive evidence-file creation for application-owned build records."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class EvidenceFileError(Exception):
    """An application evidence file could not be created safely."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


def write_application_evidence(
    path: Path,
    content: bytes,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    """Create one exact, durable, read-only evidence file without replacement."""
    parent = _require_real_parent(path.parent)
    _require_absent_target(path)

    temporary: Path | None = None
    identity: _FileIdentity | None = None
    linked = False
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        temporary = Path(name)
        opened = os.fstat(descriptor)
        identity = _FileIdentity(opened.st_dev, opened.st_ino)
        try:
            stream = os.fdopen(descriptor, "w+b")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fchown(stream.fileno(), owner_uid, owner_gid)
            _verify_open_file(stream, identity, content, owner_uid, owner_gid)
            os.fsync(stream.fileno())
            _require_path_identity(temporary, identity, "temporary")
            os.link(temporary, path, follow_symlinks=False)
            linked = True
            _require_path_identity(path, identity, "linked")
            _verify_open_file(stream, identity, content, owner_uid, owner_gid)
            _require_path_identity(path, identity, "target")
            _unlink_if_identity(temporary, identity)
            temporary = None
            _require_path_identity(path, identity, "target")
            _verify_open_file(stream, identity, content, owner_uid, owner_gid)
            _require_path_identity(path, identity, "target")
            _fsync_directory(parent)
            _require_path_identity(path, identity, "target")
    except FileExistsError as error:
        raise EvidenceFileError("evidence target already exists") from error
    except EvidenceFileError:
        if linked and identity is not None:
            _unlink_if_identity(path, identity)
        raise
    except OSError as error:
        if linked and identity is not None:
            _unlink_if_identity(path, identity)
        raise EvidenceFileError("evidence file could not be written") from error
    finally:
        if temporary is not None and identity is not None:
            _unlink_if_identity(temporary, identity)


def _require_absent_target(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise EvidenceFileError("evidence target could not be inspected") from error
    raise EvidenceFileError("evidence target already exists")


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
        or (metadata.st_dev, metadata.st_ino) != (identity.device, identity.inode)
        or metadata.st_uid != owner_uid
        or metadata.st_gid != owner_gid
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or observed != content
    ):
        raise EvidenceFileError("evidence verification failed")


def _require_path_identity(
    path: Path,
    identity: _FileIdentity,
    subject: str,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise EvidenceFileError(f"evidence {subject} identity changed") from error
    if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != (
        identity.device,
        identity.inode,
    ):
        raise EvidenceFileError(f"evidence {subject} identity changed")


def _require_real_parent(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise EvidenceFileError("evidence parent is unavailable") from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or resolved != path:
        raise EvidenceFileError("evidence parent must be one real directory")
    return resolved


def _path_has_identity(path: Path, identity: _FileIdentity) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (metadata.st_dev, metadata.st_ino) == (identity.device, identity.inode)


def _unlink_if_identity(path: Path, identity: _FileIdentity) -> None:
    if not _path_has_identity(path, identity):
        return
    with suppress(FileNotFoundError):
        path.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
