"""Descriptor-relative admission for trusted regular-file inputs."""

from __future__ import annotations

import os
import stat
from pathlib import PurePosixPath

_close_descriptor = os.close
_descriptor_relative_open_available = os.open in os.supports_dir_fd


def read_regular_absolute_file(path: str | os.PathLike[str]) -> bytes:
    """Read one canonical absolute regular file without following symlinks."""
    value = os.fspath(path)
    if not isinstance(value, str):
        raise ValueError("path must be one canonical absolute POSIX path")
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
    if (
        os.name != "posix"
        or not _descriptor_relative_open_available
        or any(
            not hasattr(os, name)
            for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
        )
    ):
        raise OSError("descriptor-safe regular-file admission is unavailable")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    leaf_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fds: list[int] = []
    leaf_fd: int | None = None
    primary_error = False
    try:
        directory_fds.append(os.open("/", directory_flags))
        for component in parsed.parts[1:-1]:
            directory_fds.append(
                os.open(component, directory_flags, dir_fd=directory_fds[-1])
            )
        leaf_fd = os.open(parsed.name, leaf_flags, dir_fd=directory_fds[-1])
        if not stat.S_ISREG(os.fstat(leaf_fd).st_mode):
            raise OSError("admitted input must be a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(leaf_fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except BaseException:
        primary_error = True
        raise
    finally:
        close_error: OSError | None = None
        for descriptor in (
            *((leaf_fd,) if leaf_fd is not None else ()),
            *reversed(directory_fds),
        ):
            try:
                _close_descriptor(descriptor)
            except OSError as error:
                if close_error is None:
                    close_error = error
        if close_error is not None and not primary_error:
            raise close_error
