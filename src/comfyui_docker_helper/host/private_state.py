"""Creation-time private host state with caller-owned descriptors."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath

_platform_name = os.name
_temporary_parent = tempfile.gettempdir


def create_private_directory(*, prefix: str) -> Path:
    """Create a private random directory below the platform temp parent."""
    _require_private_prefix(prefix)
    parent = _temporary_parent()
    if _platform_name == "nt":
        from comfyui_docker_helper._windows_files import (
            create_private_directory as create_windows_private_directory,
        )

        return Path(create_windows_private_directory(parent, prefix=prefix))
    _require_posix_platform()
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
        observed = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            raise OSError("private state root must be a real directory")
        return path
    except BaseException:
        with suppress(OSError):
            os.rmdir(path)
        raise


def create_private_file(path: str | os.PathLike[str]) -> int:
    """Exclusively create a private file and return its caller-owned descriptor."""
    value = _path_string(path)
    if _platform_name == "nt":
        from comfyui_docker_helper._windows_files import (
            create_private_file as create_windows_private_file,
        )

        return create_windows_private_file(value)
    _require_posix_path(value)
    return _open_posix_private_file(value, read_write=False, exclusive=True)


def open_private_lock_file(path: str | os.PathLike[str]) -> int:
    """Create or open a private read-write lock file and return its descriptor."""
    value = _path_string(path)
    if _platform_name == "nt":
        from comfyui_docker_helper._windows_files import (
            open_private_lock_file as open_windows_private_lock_file,
        )

        return open_windows_private_lock_file(value)
    _require_posix_path(value)
    return _open_posix_private_file(value, read_write=True, exclusive=False)


def _path_string(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if not isinstance(value, str):
        raise ValueError("private state path must be a platform string path")
    return value


def _require_posix_platform() -> None:
    if _platform_name != "posix":
        raise OSError("private state creation is unavailable on this platform")


def _require_private_prefix(prefix: str) -> None:
    if (
        not prefix
        or len(prefix) > 64
        or not prefix.isascii()
        or any(not (character.isalnum() or character in "-_") for character in prefix)
    ):
        raise ValueError("private directory prefix must be one short ASCII component")


def _require_posix_path(path: str) -> None:
    _require_posix_platform()
    parsed = PurePosixPath(path)
    if (
        not path
        or not parsed.is_absolute()
        or path.startswith("//")
        or "\\" in path
        or parsed.as_posix() != path
        or len(parsed.parts) < 2
        or any(part in {"", ".", ".."} for part in parsed.parts[1:])
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError("private state path must be one canonical absolute POSIX path")


def _open_posix_private_file(path: str, *, read_write: bool, exclusive: bool) -> int:
    if exclusive == read_write:
        raise ValueError("private file mode is invalid")
    flags = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CREAT
    flags |= os.O_RDWR if read_write else os.O_WRONLY | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise OSError("private state leaf must be a regular file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
