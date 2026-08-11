"""Platform-native admission for cooperative local regular-file inputs."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

_close_descriptor = os.close
_platform_name = os.name
_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class AdmittedRegularFile:
    """Bytes and any reliable platform-observed mode for one admitted file."""

    data: bytes
    mode: int | None


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
    if not isinstance(value, str):
        raise ValueError("path must be one canonical absolute platform path")
    if _platform_name == "nt":
        from comfyui_docker_helper._windows_files import (
            read_regular_absolute_file as read_windows_regular_absolute_file,
        )

        return AdmittedRegularFile(
            read_windows_regular_absolute_file(value, max_bytes=max_bytes),
            mode=None,
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
        mode = os.fstat(leaf_fd).st_mode
        if not stat.S_ISREG(mode):
            raise OSError("admitted input must be a regular file")
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            read_size = _READ_CHUNK_BYTES
            if max_bytes is not None:
                read_size = min(read_size, max_bytes - total_bytes + 1)
            chunk = os.read(leaf_fd, read_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise OSError("admitted input exceeds the maximum byte count")
            chunks.append(chunk)
        return AdmittedRegularFile(b"".join(chunks), mode)
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
