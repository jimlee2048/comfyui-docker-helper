from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from comfyui_docker_helper import _windows_files
from comfyui_docker_helper.host import private_state


@pytest.mark.skipif(
    os.name != "posix", reason="exercises the POSIX private-state backend"
)
def test_posix_private_state_preserves_modes_and_caller_owned_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_state, "_temporary_parent", lambda: os.fspath(tmp_path))
    root = private_state.create_private_directory(prefix="cdh-private-")

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    secret = root / "secret"
    secret_fd = private_state.create_private_file(secret)
    try:
        assert os.get_inheritable(secret_fd) is False
        os.write(secret_fd, b"secret")
        assert stat.S_IMODE(os.fstat(secret_fd).st_mode) == 0o600
    finally:
        os.close(secret_fd)
    with pytest.raises(FileExistsError):
        private_state.create_private_file(secret)

    lock = root / "lock"
    first_lock_fd = private_state.open_private_lock_file(lock)
    second_lock_fd = private_state.open_private_lock_file(lock)
    try:
        os.write(first_lock_fd, b"x")
        os.lseek(second_lock_fd, 0, os.SEEK_SET)
        assert os.read(second_lock_fd, 1) == b"x"
        assert stat.S_IMODE(os.fstat(second_lock_fd).st_mode) == 0o600
    finally:
        os.close(second_lock_fd)
        os.close(first_lock_fd)

    linked = root / "linked"
    linked.symlink_to(secret)
    with pytest.raises(OSError):
        private_state.open_private_lock_file(linked)


class _FakeHandle:
    def __init__(self, path: str, identity: int) -> None:
        self.path = path
        self.identity = identity


class _FakePrivateWindowsApi:
    volume_root = "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\"

    def __init__(self) -> None:
        self.directory_security = object()
        self.file_security = object()
        self.create_calls: list[tuple[str, int, int, int, int]] = []
        self.create_directory_calls: list[tuple[str, object]] = []
        self.private_file_calls: list[tuple[str, int, int, int, int, object]] = []
        self.security_verifications: list[tuple[str, bool]] = []
        self.closed_paths: list[str] = []
        self.detached_paths: list[str] = []
        self.raw_closes: list[int] = []
        self.inheritability: list[tuple[int, bool]] = []
        self.closed_fds: list[int] = []
        self.removed_directories: list[str] = []
        self.deleted_files: list[str] = []
        self.existing_private_file = False
        self._identity = 1

    def _handle(self, path: str) -> _FakeHandle:
        handle = _FakeHandle(path, self._identity)
        self._identity += 1
        return handle

    def create_file(
        self,
        path: str,
        desired_access: int,
        share_mode: int,
        creation_disposition: int,
        flags_and_attributes: int,
    ) -> object:
        self.create_calls.append(
            (
                path,
                desired_access,
                share_mode,
                creation_disposition,
                flags_and_attributes,
            )
        )
        return self._handle(path)

    def get_drive_type(self, root: str) -> int:
        assert root == "C:\\"
        return _windows_files._DRIVE_FIXED

    def get_file_type(self, _handle: object) -> int:
        return _windows_files._FILE_TYPE_DISK

    def get_file_information(self, handle: object) -> tuple[object, ...]:
        assert isinstance(handle, _FakeHandle)
        is_file = handle.path.endswith(("\\secret", "\\lock"))
        return (
            0 if is_file else _windows_files._FILE_ATTRIBUTE_DIRECTORY,
            None,
            None,
            None,
            42,
            0,
            0,
            1,
            0,
            handle.identity,
        )

    def get_final_path_name(self, handle: object, flags: int) -> str:
        assert isinstance(handle, _FakeHandle)
        assert flags == _windows_files._VOLUME_NAME_GUID
        return self.volume_root if handle.path == "C:\\" else handle.path

    def read_file(self, _handle: object, _size: int) -> bytes:
        raise AssertionError("private-state tests do not read through Win32")

    def close_handle(self, handle: object) -> None:
        assert isinstance(handle, _FakeHandle)
        self.closed_paths.append(handle.path)

    def get_volume_flags(self, root: str) -> int:
        assert root == self.volume_root
        return _windows_files._FILE_PERSISTENT_ACLS

    def private_security_attributes(self, *, directory: bool) -> object:
        return self.directory_security if directory else self.file_security

    def create_directory(self, path: str, security_attributes: object) -> bool:
        self.create_directory_calls.append((path, security_attributes))
        return True

    def create_private_file(
        self,
        path: str,
        desired_access: int,
        share_mode: int,
        creation_disposition: int,
        flags_and_attributes: int,
        security_attributes: object,
    ) -> object | None:
        self.private_file_calls.append(
            (
                path,
                desired_access,
                share_mode,
                creation_disposition,
                flags_and_attributes,
                security_attributes,
            )
        )
        if (
            self.existing_private_file
            and creation_disposition == _windows_files._CREATE_NEW
        ):
            return None
        return self._handle(path)

    def verify_private_security(self, handle: object, *, directory: bool) -> None:
        assert isinstance(handle, _FakeHandle)
        self.security_verifications.append((handle.path, directory))

    def remove_directory(self, path: str) -> None:
        self.removed_directories.append(path)

    def delete_file(self, path: str) -> None:
        self.deleted_files.append(path)

    def detach_handle(self, handle: object) -> int:
        assert isinstance(handle, _FakeHandle)
        self.detached_paths.append(handle.path)
        return 1000 + handle.identity

    def open_osfhandle(self, _handle: int, _flags: int) -> int:
        return 700

    def close_raw_handle(self, handle: int) -> None:
        self.raw_closes.append(handle)

    def set_fd_inheritable(self, descriptor: int, inheritable: bool) -> None:
        self.inheritability.append((descriptor, inheritable))

    def close_fd(self, descriptor: int) -> None:
        self.closed_fds.append(descriptor)


def test_windows_private_directory_passes_protected_security_at_creation() -> None:
    api = _FakePrivateWindowsApi()

    result = _windows_files._create_private_directory_windows(
        "C:\\Temp",
        prefix="cdh-private-",
        api=api,
        candidate_suffix=lambda: "token",
    )

    candidate = f"{api.volume_root}Temp\\cdh-private-token"
    assert result == "C:\\Temp\\cdh-private-token"
    assert api.create_directory_calls == [(candidate, api.directory_security)]
    assert api.security_verifications == [(candidate, True)]
    assert api.closed_paths == [candidate, f"{api.volume_root}Temp", "C:\\"]


def test_windows_exclusive_private_file_transfers_handle_ownership_once() -> None:
    api = _FakePrivateWindowsApi()

    descriptor = _windows_files._open_private_file_windows(
        "C:\\Temp\\secret",
        exclusive=True,
        read_write=False,
        api=api,
    )

    internal_path = f"{api.volume_root}Temp\\secret"
    assert descriptor == 700
    assert api.private_file_calls[0][0] == internal_path
    assert api.private_file_calls[0][3] == _windows_files._CREATE_NEW
    assert api.private_file_calls[0][5] is api.file_security
    assert api.security_verifications == [(internal_path, False)]
    assert api.detached_paths == [internal_path]
    assert internal_path not in api.closed_paths
    assert api.raw_closes == []
    assert api.inheritability == [(descriptor, False)]
    assert api.closed_paths == [f"{api.volume_root}Temp", "C:\\"]


def test_windows_existing_lock_is_opened_and_security_verified() -> None:
    api = _FakePrivateWindowsApi()
    api.existing_private_file = True

    descriptor = _windows_files._open_private_file_windows(
        "C:\\Temp\\lock",
        exclusive=False,
        read_write=True,
        api=api,
    )

    assert descriptor == 700
    assert [call[3] for call in api.private_file_calls] == [
        _windows_files._CREATE_NEW,
        _windows_files._OPEN_EXISTING,
    ]
    assert all(
        call[2] == _windows_files._FILE_SHARE_READ | _windows_files._FILE_SHARE_WRITE
        for call in api.private_file_calls
    )
    assert all(call[5] is api.file_security for call in api.private_file_calls)
    assert api.security_verifications == [(f"{api.volume_root}Temp\\lock", False)]


def test_windows_fd_conversion_failure_closes_only_the_detached_handle() -> None:
    api = _FakePrivateWindowsApi()

    def fail_conversion(handle: int, _flags: int) -> int:
        raise OSError(f"conversion failed for {handle}")

    api.open_osfhandle = fail_conversion  # type: ignore[method-assign]

    with pytest.raises(OSError):
        _windows_files._open_private_file_windows(
            "C:\\Temp\\secret",
            exclusive=True,
            read_write=False,
            api=api,
        )

    internal_path = f"{api.volume_root}Temp\\secret"
    assert len(api.raw_closes) == 1
    assert internal_path not in api.closed_paths
    assert api.closed_fds == []
    assert api.deleted_files == [internal_path]


def test_windows_inheritability_failure_closes_only_the_transferred_fd() -> None:
    api = _FakePrivateWindowsApi()

    def fail_inheritability(_descriptor: int, _inheritable: bool) -> None:
        raise OSError("inheritability failed")

    api.set_fd_inheritable = fail_inheritability  # type: ignore[method-assign]

    with pytest.raises(OSError):
        _windows_files._open_private_file_windows(
            "C:\\Temp\\secret",
            exclusive=True,
            read_write=False,
            api=api,
        )

    internal_path = f"{api.volume_root}Temp\\secret"
    assert api.raw_closes == []
    assert api.closed_fds == [700]
    assert internal_path not in api.closed_paths
    assert api.deleted_files == [internal_path]
