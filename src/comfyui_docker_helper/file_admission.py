"""Platform-native admission for cooperative local regular-file inputs."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_close_descriptor = os.close
_platform_name = os.name
_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class AdmittedRegularFile:
    """Bytes and any reliable platform-observed mode for one admitted file."""

    data: bytes
    mode: int | None


@dataclass(frozen=True, slots=True)
class ObservedRegularFile:
    """Shape observed while streaming one admitted regular file."""

    size: int
    mode: int | None


class FileCloneUnavailableError(OSError):
    """The admitted source cannot be cloned to the requested filesystem."""


@dataclass(frozen=True, slots=True)
class AdmittedRegularFileReader:
    """Metadata and bounded reads valid only during one admitted operation."""

    size: int
    mode: int | None
    _read_chunk: Callable[[int | None], bytes] = field(repr=False)
    _clone_to: Callable[[int], None] | None = field(default=None, repr=False)

    def read_chunk(self, limit: int | None = None) -> bytes:
        """Read at most one fixed-size chunk from the admitted file."""
        return self._read_chunk(limit)

    def clone_to(self, destination_fd: int) -> None:
        """Clone the whole admitted file or report a classified unavailable case."""
        if self._clone_to is None:
            raise FileCloneUnavailableError("copy-on-write clone is unavailable")
        self._clone_to(destination_fd)


def operate_regular_absolute_file[T](
    path: str | os.PathLike[str],
    operation: Callable[[AdmittedRegularFileReader], T],
) -> T:
    """Run one operation while a regular file remains admitted and open."""
    return _operate_regular_absolute_file(path, operation)


def observe_regular_absolute_file(
    path: str | os.PathLike[str],
) -> ObservedRegularFile:
    """Observe one admitted regular file without consuming its content."""
    return operate_regular_absolute_file(
        path, lambda reader: ObservedRegularFile(reader.size, reader.mode)
    )


def consume_regular_absolute_file(
    path: str | os.PathLike[str], consume: Callable[[bytes], None]
) -> ObservedRegularFile:
    """Stream one admitted regular file in fixed chunks to ``consume``."""
    return _consume_regular_absolute_file(path, max_bytes=None, consume=consume)


def read_regular_absolute_file(path: str | os.PathLike[str]) -> bytes:
    """Read one statically checked path without following its final symlink."""
    return _read_regular_absolute_file(path, max_bytes=None).data


def read_bounded_regular_absolute_file(
    path: str | os.PathLike[str], *, max_bytes: int
) -> AdmittedRegularFile:
    """Read one admitted file and fail on its first byte beyond ``max_bytes``."""
    if max_bytes < 0:
        raise ValueError("maximum byte count must not be negative")
    return _read_regular_absolute_file(path, max_bytes=max_bytes)


def _read_regular_absolute_file(
    path: str | os.PathLike[str], *, max_bytes: int | None
) -> AdmittedRegularFile:
    value = os.fspath(path)
    if _platform_name == "nt":
        if not isinstance(value, str):
            raise ValueError("path must be one canonical absolute platform path")
        from comfyui_docker_helper._windows_files import (
            read_regular_absolute_file as read_windows_regular_absolute_file,
        )

        return AdmittedRegularFile(
            read_windows_regular_absolute_file(value, max_bytes=max_bytes),
            mode=None,
        )
    chunks: list[bytes] = []
    observed = _consume_regular_absolute_file(
        path,
        max_bytes=max_bytes,
        consume=chunks.append,
    )
    return AdmittedRegularFile(b"".join(chunks), observed.mode)


def _consume_regular_absolute_file(
    path: str | os.PathLike[str],
    *,
    max_bytes: int | None,
    consume: Callable[[bytes], None],
) -> ObservedRegularFile:
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("maximum byte count must not be negative")

    def operation(reader: AdmittedRegularFileReader) -> ObservedRegularFile:
        if max_bytes is not None and reader.size > max_bytes:
            raise OSError("admitted input exceeds the maximum byte count")
        total_bytes = 0
        while True:
            read_size = None
            if max_bytes is not None:
                read_size = min(_READ_CHUNK_BYTES, max_bytes - total_bytes + 1)
            chunk = reader.read_chunk(read_size)
            if not chunk:
                return ObservedRegularFile(total_bytes, reader.mode)
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise OSError("admitted input exceeds the maximum byte count")
            consume(chunk)

    return _operate_regular_absolute_file(path, operation)


def _operate_regular_absolute_file[T](
    path: str | os.PathLike[str],
    operation: Callable[[AdmittedRegularFileReader], T],
) -> T:
    value = os.fspath(path)
    if not isinstance(value, str):
        raise ValueError("path must be one canonical absolute platform path")
    if _platform_name == "nt":
        from comfyui_docker_helper._windows_files import (
            operate_regular_absolute_file as operate_windows_regular_absolute_file,
        )

        return operate_windows_regular_absolute_file(
            value,
            lambda size, read_chunk: operation(
                AdmittedRegularFileReader(size, None, read_chunk)
            ),
        )

    parsed = PurePosixPath(value)
    if (
        not value
        or not parsed.is_absolute()
        or value.startswith("//")
        or "\\" in value
        or parsed.as_posix() != value
        or len(parsed.parts) < 2
        or any(part in {"", ".", ".."} for part in parsed.parts[1:])
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("path must be one canonical absolute POSIX path")
    if os.name != "posix" or any(
        not hasattr(os, name) for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
    ):
        raise OSError("regular-file admission is unavailable")

    _observe_posix_components(parsed)
    leaf_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    leaf_fd: int | None = None
    primary_error = False
    try:
        leaf_fd = os.open(value, leaf_flags)
        before = os.fstat(leaf_fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("admitted input must be a regular file")
        if before.st_size < 0:
            raise OSError("admitted input has an invalid size")
        total_bytes = 0
        eof = False

        def read_chunk(limit: int | None = None) -> bytes:
            nonlocal eof, total_bytes
            if eof:
                return b""
            if limit is not None and limit < 1:
                raise ValueError("read limit must be positive")
            read_size = (
                _READ_CHUNK_BYTES if limit is None else min(_READ_CHUNK_BYTES, limit)
            )
            chunk = os.read(leaf_fd, read_size)
            if not chunk:
                eof = True
                return b""
            total_bytes += len(chunk)
            return chunk

        def clone_to(destination_fd: int) -> None:
            import errno
            import fcntl

            try:
                fcntl.ioctl(destination_fd, 0x40049409, leaf_fd)
            except OSError as error:
                if error.errno in {
                    errno.EINVAL,
                    errno.ENOTTY,
                    errno.EOPNOTSUPP,
                    errno.EXDEV,
                }:
                    raise FileCloneUnavailableError(
                        "copy-on-write clone is unavailable"
                    ) from error
                raise

        result = operation(
            AdmittedRegularFileReader(
                before.st_size,
                before.st_mode,
                read_chunk,
                clone_to,
            )
        )
        after = os.fstat(leaf_fd)
        if not stat.S_ISREG(after.st_mode):
            raise OSError("admitted input must be a regular file")
        if before.st_size != after.st_size or (eof and total_bytes != before.st_size):
            raise OSError("admitted input changed during its bounded read")
        return result
    except BaseException:
        primary_error = True
        raise
    finally:
        if leaf_fd is not None:
            try:
                _close_descriptor(leaf_fd)
            except OSError as error:
                if not primary_error:
                    raise error


def _observe_posix_components(path: PurePosixPath) -> None:
    """Reject links and special nodes visible during one static path walk."""
    candidate = PurePosixPath("/")
    for index, component in enumerate(path.parts[1:], start=1):
        candidate /= component
        mode = os.lstat(candidate).st_mode
        leaf = index == len(path.parts) - 1
        if stat.S_ISLNK(mode):
            raise OSError(
                "admitted input must be a regular file"
                if leaf
                else "admitted path ancestors must be real directories"
            )
        if leaf:
            if not stat.S_ISREG(mode):
                raise OSError("admitted input must be a regular file")
        elif not stat.S_ISDIR(mode):
            raise OSError("admitted path ancestors must be real directories")
