"""Darwin clone ABI, capability classification and preflight policy."""

import ctypes
import errno
from types import SimpleNamespace

import pytest

from comfyui_docker_helper.filesystem import macos
from comfyui_docker_helper.filesystem.admission import FileCloneUnavailableError


@pytest.mark.parametrize(
    ("number", "unavailable"),
    [
        (errno.ENOTSUP, True),
        (errno.EXDEV, True),
        (errno.EINVAL, False),
        (errno.EEXIST, False),
        (errno.EPERM, False),
        (errno.ENOSPC, False),
        (errno.EIO, False),
    ],
)
def test_clone_errors_and_arguments(monkeypatch, number, unavailable):
    monkeypatch.setattr(macos.os, "fstat", lambda _fd: SimpleNamespace(st_flags=0))

    def fail(source, directory, name, flags):
        assert (source, directory, name, flags) == (10, 11, b"target", 2)
        ctypes.set_errno(number)
        return -1

    monkeypatch.setattr(macos, "_clone_function", lambda: fail)
    with pytest.raises(FileCloneUnavailableError if unavailable else OSError) as error:
        macos.clone_new_file(10, 11, "target", 0o644)
    if not unavailable:
        assert not isinstance(error.value, FileCloneUnavailableError)
        assert error.value.errno == number


@pytest.mark.parametrize("flags", [0x2, 0x4, 0x20000, 0x40000])
def test_blocking_flags_decline_before_native_creation(monkeypatch, flags):
    monkeypatch.setattr(macos.os, "fstat", lambda _fd: SimpleNamespace(st_flags=flags))
    monkeypatch.setattr(
        macos, "_clone_function", lambda: pytest.fail("native clone called")
    )
    with pytest.raises(FileCloneUnavailableError):
        macos.clone_new_file(10, 11, "target", 0o644)


def test_missing_symbol_and_explicit_abi(monkeypatch):
    macos._clone_function.cache_clear()
    monkeypatch.setattr(
        macos.ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace()
    )
    with pytest.raises(FileCloneUnavailableError):
        macos._clone_function()

    function = SimpleNamespace()

    def load(path, *, use_errno):
        assert path == "/usr/lib/libSystem.B.dylib"
        assert use_errno is True
        return SimpleNamespace(fclonefileat=function)

    monkeypatch.setattr(macos.ctypes, "CDLL", load)
    try:
        assert macos._clone_function() is function
        assert function.argtypes == [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        assert function.restype is ctypes.c_int
    finally:
        macos._clone_function.cache_clear()
