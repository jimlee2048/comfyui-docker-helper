"""Secret-only bounded regular-file admission contracts."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_docker_helper import _windows_files, file_admission

_SECRET_LIMIT = 65_525


@pytest.mark.parametrize(
    ("size", "accepted"),
    [(_SECRET_LIMIT, True), (_SECRET_LIMIT + 1, False)],
)
def test_bounded_admission_accepts_the_limit_and_rejects_the_next_byte(
    tmp_path: Path, size: int, accepted: bool
) -> None:
    source = tmp_path / "secret"
    source.write_bytes(b"x" * size)

    if accepted:
        admitted = file_admission.read_bounded_regular_absolute_file(
            source, max_bytes=_SECRET_LIMIT
        )
        assert admitted.data == b"x" * size
    else:
        with pytest.raises(OSError) as raised:
            file_admission.read_bounded_regular_absolute_file(
                source, max_bytes=_SECRET_LIMIT
            )
        assert str(raised.value) == "admitted input exceeds the maximum byte count"


@pytest.mark.skipif(
    os.name != "posix", reason="exercises the POSIX descriptor admission backend"
)
def test_bounded_admission_observes_mode_and_bytes_through_the_same_leaf_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "secret"
    source.write_bytes(b"secret bytes")
    source.chmod(0o640)
    real_fstat = os.fstat
    real_read = os.read
    fstat_descriptors: list[int] = []
    read_descriptors: list[int] = []

    def observed_fstat(descriptor: int) -> os.stat_result:
        fstat_descriptors.append(descriptor)
        return real_fstat(descriptor)

    def observed_read(descriptor: int, length: int) -> bytes:
        read_descriptors.append(descriptor)
        return real_read(descriptor, length)

    monkeypatch.setattr(file_admission.os, "fstat", observed_fstat)
    monkeypatch.setattr(file_admission.os, "read", observed_read)

    admitted = file_admission.read_bounded_regular_absolute_file(
        source, max_bytes=_SECRET_LIMIT
    )

    assert admitted.data == b"secret bytes"
    assert admitted.mode is not None
    assert stat.S_IMODE(admitted.mode) == 0o640
    # Mode and bytes share one admitted descriptor so a path replacement cannot
    # split the validation result from the content that is consumed.
    assert fstat_descriptors
    assert read_descriptors
    assert set(read_descriptors) == set(fstat_descriptors)


@pytest.mark.skipif(
    os.name != "posix", reason="exercises the POSIX static admission backend"
)
def test_posix_admission_statically_observes_components_then_opens_only_the_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "secret"
    source.write_bytes(b"secret")
    real_lstat = os.lstat
    real_open = os.open
    observed_paths: list[str] = []
    opened: list[tuple[str, int | None]] = []

    def observe_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        observed_paths.append(os.fspath(path))
        return real_lstat(path)

    def observe_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        opened.append((path, dir_fd))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(file_admission.os, "lstat", observe_lstat)
    monkeypatch.setattr(file_admission.os, "open", observe_open)

    assert file_admission.read_regular_absolute_file(source) == b"secret"
    assert observed_paths[-1] == os.fspath(source)
    assert opened == [(os.fspath(source), None)]


@pytest.mark.parametrize(
    "path",
    [
        "secret",
        "C:secret",
        "\\\\server\\share\\secret",
        "\\\\?\\C:\\secret",
        "\\\\.\\C:\\secret",
        "\\??\\C:\\secret",
        "C:/secret",
        "C:\\safe\\..\\secret",
        "C:\\safe\\\\secret",
        "C:\\safe\\secret.",
        "C:\\safe\\secret ",
        "C:\\safe\\secret:stream",
        "C:\\safe\\NUL.txt",
        "C:\\safe\\CLOCK$",
        "C:\\safe\\COM1",
        "C:\\safe\\bad?.txt",
        "C:\\safe\\bad\x00.txt",
        "\u0131:\\secret",
        "\u017f:\\secret",
    ],
)
def test_windows_admission_rejects_noncanonical_device_and_stream_paths(
    path: str,
) -> None:
    with pytest.raises(ValueError) as raised:
        _windows_files._parse_windows_regular_file_path(path)

    assert str(raised.value) == (
        "path must be one canonical absolute local Windows path"
    )


def test_windows_admission_accepts_one_canonical_drive_absolute_path() -> None:
    parsed = _windows_files._parse_windows_regular_file_path(
        "c:\\Users\\Example User\\密钥.txt"
    )

    assert parsed.drive_root == "C:\\"
    assert parsed.components == ("Users", "Example User", "密钥.txt")


def test_windows_local_path_preflight_accepts_a_local_drive_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    monkeypatch.setattr(_windows_files, "_PyWin32Api", lambda: api)

    _windows_files.validate_local_absolute_path("C:\\")

    assert api.create_calls == []
    assert api.attribute_calls == []


def test_windows_attributes_adapter_rejects_the_failure_sentinel() -> None:
    api = _windows_files._PyWin32Api.__new__(_windows_files._PyWin32Api)
    api._error_type = OSError
    api._win32file = SimpleNamespace(
        GetFileAttributes=lambda _path: _windows_files._INVALID_FILE_ATTRIBUTES
    )

    with pytest.raises(OSError, match="Win32 GetFileAttributes failed"):
        api.get_file_attributes("C:\\safe\\secret.txt")


class _FakeWindowsHandle:
    def __init__(self, path: str) -> None:
        self.path = path
        self.offset = 0


class _FakeWindowsApi:
    def __init__(self, content: bytes = b"secret bytes") -> None:
        self.content = content
        self.create_calls: list[tuple[str, int, int, int, int]] = []
        self.attribute_calls: list[str] = []
        self.attribute_overrides: dict[str, int] = {}
        self.information_handles: list[_FakeWindowsHandle] = []
        self.read_handles: list[_FakeWindowsHandle] = []
        self.closed_paths: list[str] = []
        self.handle_attributes = 0

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
        return _FakeWindowsHandle(path)

    def get_drive_type(self, root: str) -> int:
        assert root == "C:\\"
        return _windows_files._DRIVE_FIXED

    def get_file_attributes(self, path: str) -> int:
        self.attribute_calls.append(path)
        if path in self.attribute_overrides:
            return self.attribute_overrides[path]
        return (
            0
            if path.endswith(("secret.txt", "SECRET~1.TXT"))
            else _windows_files._FILE_ATTRIBUTE_DIRECTORY
        )

    def get_file_type(self, _handle: object) -> int:
        return _windows_files._FILE_TYPE_DISK

    def get_file_information(self, handle: object) -> tuple[object, ...]:
        assert isinstance(handle, _FakeWindowsHandle)
        self.information_handles.append(handle)
        size = len(self.content)
        return (
            self.handle_attributes,
            None,
            None,
            None,
            42,
            size >> 32,
            size & 0xFFFFFFFF,
            1,
            0,
            1,
        )

    def read_file(self, handle: object, size: int) -> bytes:
        assert isinstance(handle, _FakeWindowsHandle)
        self.read_handles.append(handle)
        start = handle.offset
        handle.offset += size
        return self.content[start : start + size]

    def close_handle(self, handle: object) -> None:
        assert isinstance(handle, _FakeWindowsHandle)
        self.closed_paths.append(handle.path)


def test_windows_admission_statically_observes_components_and_reads_one_handle() -> (
    None
):
    api = _FakeWindowsApi()

    data = _windows_files._read_regular_absolute_file(
        "C:\\safe\\nested\\secret.txt",
        max_bytes=_SECRET_LIMIT,
        api=api,
    )

    assert data == b"secret bytes"
    assert api.attribute_calls == [
        "C:\\safe",
        "C:\\safe\\nested",
        "C:\\safe\\nested\\secret.txt",
    ]
    assert len(api.create_calls) == 1
    assert api.create_calls[0][0] == "C:\\safe\\nested\\secret.txt"
    assert api.create_calls[0][2] == (
        _windows_files._FILE_SHARE_READ
        | _windows_files._FILE_SHARE_WRITE
        | _windows_files._FILE_SHARE_DELETE
    )
    assert api.create_calls[0][4] & _windows_files._FILE_FLAG_OPEN_REPARSE_POINT
    leaf = api.read_handles[0]
    assert set(api.read_handles) == {leaf}
    assert api.information_handles.count(leaf) == 2
    assert api.closed_paths == ["C:\\safe\\nested\\secret.txt"]


def test_windows_admission_rejects_an_unverifiable_drive_before_opening() -> None:
    api = _FakeWindowsApi()
    api.get_drive_type = lambda _root: _windows_files._DRIVE_REMOTE  # type: ignore[method-assign]

    with pytest.raises(OSError) as raised:
        _windows_files._read_regular_absolute_file(
            "C:\\safe\\secret.txt", max_bytes=_SECRET_LIMIT, api=api
        )

    assert str(raised.value) == (
        "regular-file admission requires a verifiable local drive"
    )
    assert api.attribute_calls == []
    assert api.create_calls == []


def test_windows_admission_rejects_a_statically_observed_ancestor_reparse() -> None:
    api = _FakeWindowsApi()
    api.attribute_overrides["C:\\safe"] = (
        _windows_files._FILE_ATTRIBUTE_DIRECTORY
        | _windows_files._FILE_ATTRIBUTE_REPARSE_POINT
    )

    with pytest.raises(OSError) as raised:
        _windows_files._read_regular_absolute_file(
            "C:\\safe\\secret.txt", max_bytes=_SECRET_LIMIT, api=api
        )

    assert str(raised.value) == (
        "admitted path ancestors must be real local directories"
    )
    assert api.create_calls == []


def test_windows_admission_rejects_leaf_reparse_from_the_opened_handle() -> None:
    api = _FakeWindowsApi()
    api.handle_attributes = _windows_files._FILE_ATTRIBUTE_REPARSE_POINT

    with pytest.raises(OSError) as raised:
        _windows_files._read_regular_absolute_file(
            "C:\\safe\\secret.txt", max_bytes=_SECRET_LIMIT, api=api
        )

    assert str(raised.value) == "admitted input must be a regular local file"
    assert api.read_handles == []
    assert api.closed_paths == ["C:\\safe\\secret.txt"]


def test_windows_public_admission_returns_bytes_without_posix_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_admission, "_platform_name", "nt")
    monkeypatch.setattr(
        _windows_files,
        "read_regular_absolute_file",
        lambda _path, *, max_bytes: b"secret",
    )

    admitted = file_admission.read_bounded_regular_absolute_file(
        "C:\\safe\\secret.txt", max_bytes=_SECRET_LIMIT
    )

    assert admitted.data == b"secret"
    assert admitted.mode is None
