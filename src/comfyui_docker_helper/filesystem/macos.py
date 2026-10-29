"""Native clone creation for admitted macOS files in an owned directory."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from functools import lru_cache

from comfyui_docker_helper.filesystem.admission import FileCloneUnavailableError

_CLONE_NOOWNERCOPY = 0x0002
# Darwin user/system immutable and append flags. Keep imports usable on other hosts.
_BLOCKING_FLAGS = 0x00000002 | 0x00000004 | 0x00020000 | 0x00040000
_close_descriptor = os.close


@lru_cache(maxsize=1)
def _clone_function() -> Callable[[int, int, bytes, int], int]:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    try:
        function = library.fclonefileat
    except AttributeError:
        raise FileCloneUnavailableError("copy-on-write clone is unavailable") from None
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    return function


def clone_new_file(source_fd: int, directory_fd: int, name: str, mode: int) -> int:
    """Create a clone and return its owned descriptor; never replace a target."""
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ValueError("clone target must be one leaf name")
    # These flags let clone succeed but prevent ordinary normalization or cleanup.
    if os.fstat(source_fd).st_flags & _BLOCKING_FLAGS:
        raise FileCloneUnavailableError("copy-on-write clone is unavailable")
    if (
        _clone_function()(
            source_fd, directory_fd, os.fsencode(name), _CLONE_NOOWNERCOPY
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOTSUP, errno.EXDEV}:
            raise FileCloneUnavailableError("copy-on-write clone is unavailable")
        raise OSError(error_number, "native file clone failed")

    # A read-only source yields a read-only clone. The private directory owns this
    # newly created leaf, so normalize its mode before acquiring a read/write fd.
    os.chmod(name, mode, dir_fd=directory_fd, follow_symlinks=False)
    descriptor = os.open(
        name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("cloned target must be a regular file")
    except BaseException:
        # Preserve the operation error; the host owns whole-stage cleanup.
        with suppress(OSError):
            _close_descriptor(descriptor)
        raise
    return descriptor
