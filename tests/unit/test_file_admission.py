"""Secret-only bounded regular-file admission contracts."""

import os
import stat
from pathlib import Path

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
    assert admitted.permissions_unverifiable is False
    # Mode and bytes share one admitted descriptor so a path replacement cannot
    # split the validation result from the content that is consumed.
    assert fstat_descriptors
    assert read_descriptors
    assert set(read_descriptors) == set(fstat_descriptors)


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


class _FakeWindowsHandle:
    def __init__(self, path: str, *, file_id: int) -> None:
        self.path = path
        self.file_id = file_id
        self.offset = 0


class _FakeWindowsApi:
    volume_root = "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\"

    def __init__(self, content: bytes = b"secret bytes") -> None:
        self.content = content
        self.create_calls: list[tuple[str, int, int, int, int]] = []
        self.information_handles: list[_FakeWindowsHandle] = []
        self.read_handles: list[_FakeWindowsHandle] = []
        self.closed_paths: list[str] = []
        self.final_path_overrides: dict[str, str] = {}
        self._next_file_id = 1

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
        handle = _FakeWindowsHandle(path, file_id=self._next_file_id)
        self._next_file_id += 1
        return handle

    def get_drive_type(self, root: str) -> int:
        assert root == "C:\\"
        return _windows_files._DRIVE_FIXED

    def get_file_type(self, _handle: object) -> int:
        return _windows_files._FILE_TYPE_DISK

    def get_file_information(self, handle: object) -> tuple[object, ...]:
        assert isinstance(handle, _FakeWindowsHandle)
        self.information_handles.append(handle)
        is_leaf = handle.path.endswith(("secret.txt", "SECRET~1.TXT"))
        attributes = 0 if is_leaf else _windows_files._FILE_ATTRIBUTE_DIRECTORY
        size = len(self.content) if is_leaf else 0
        return (
            attributes,
            None,
            None,
            None,
            42,
            size >> 32,
            size & 0xFFFFFFFF,
            1,
            handle.file_id >> 32,
            handle.file_id & 0xFFFFFFFF,
        )

    def get_final_path_name(self, handle: object, flags: int) -> str:
        assert isinstance(handle, _FakeWindowsHandle)
        assert flags == _windows_files._VOLUME_NAME_GUID
        if handle.path == "C:\\":
            return self.volume_root
        return self.final_path_overrides.get(handle.path, handle.path)

    def read_file(self, handle: object, size: int) -> bytes:
        assert isinstance(handle, _FakeWindowsHandle)
        self.read_handles.append(handle)
        start = handle.offset
        handle.offset += size
        return self.content[start : start + size]

    def close_handle(self, handle: object) -> None:
        assert isinstance(handle, _FakeWindowsHandle)
        self.closed_paths.append(handle.path)


def test_windows_admission_uses_a_stable_volume_and_one_held_handle_chain() -> None:
    api = _FakeWindowsApi()
    open_counts: list[int] = []

    data = _windows_files._read_regular_absolute_file(
        "C:\\safe\\nested\\secret.txt",
        max_bytes=_SECRET_LIMIT,
        api=api,
        after_directory_open=lambda _path: open_counts.append(
            len(api.create_calls) - len(api.closed_paths)
        ),
    )

    assert data == b"secret bytes"
    assert [call[0] for call in api.create_calls] == [
        "C:\\",
        f"{api.volume_root}safe",
        f"{api.volume_root}safe\\nested",
        f"{api.volume_root}safe\\nested\\secret.txt",
    ]
    assert all(call[2] == _windows_files._FILE_SHARE_READ for call in api.create_calls)
    assert open_counts == [2, 3]
    leaf = api.read_handles[0]
    assert set(api.read_handles) == {leaf}
    assert api.information_handles.count(leaf) == 2
    assert api.closed_paths == [
        f"{api.volume_root}safe\\nested\\secret.txt",
        f"{api.volume_root}safe\\nested",
        f"{api.volume_root}safe",
        "C:\\",
    ]


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
    assert api.create_calls == []


def test_windows_admission_fails_closed_when_ancestor_lineage_differs() -> None:
    api = _FakeWindowsApi()
    expected_ancestor = f"{api.volume_root}safe"
    api.final_path_overrides[expected_ancestor] = f"{api.volume_root}other-parent\\safe"

    with pytest.raises(OSError) as raised:
        _windows_files._read_regular_absolute_file(
            "C:\\safe\\secret.txt", max_bytes=_SECRET_LIMIT, api=api
        )

    assert str(raised.value) == "admitted path lineage cannot be verified"
    assert api.closed_paths == [expected_ancestor, "C:\\"]


def test_windows_admission_fails_closed_when_leaf_lineage_differs() -> None:
    api = _FakeWindowsApi()
    expected_leaf = f"{api.volume_root}safe\\secret.txt"
    api.final_path_overrides[expected_leaf] = f"{api.volume_root}other\\secret.txt"

    with pytest.raises(OSError) as raised:
        _windows_files._read_regular_absolute_file(
            "C:\\safe\\secret.txt", max_bytes=_SECRET_LIMIT, api=api
        )

    assert str(raised.value) == "admitted path lineage cannot be verified"
    assert api.read_handles == []
    assert api.closed_paths == [expected_leaf, f"{api.volume_root}safe", "C:\\"]


def test_windows_admission_accepts_filesystem_normalized_short_names() -> None:
    api = _FakeWindowsApi()
    short_parent = f"{api.volume_root}RUNNER~1"
    long_parent = f"{api.volume_root}Runner Admin"
    short_leaf = f"{long_parent}\\SECRET~1.TXT"
    long_leaf = f"{long_parent}\\secret value.txt"
    api.final_path_overrides[short_parent] = long_parent
    api.final_path_overrides[short_leaf] = long_leaf

    data = _windows_files._read_regular_absolute_file(
        "C:\\RUNNER~1\\SECRET~1.TXT",
        max_bytes=_SECRET_LIMIT,
        api=api,
    )

    assert data == b"secret bytes"
    assert [call[0] for call in api.create_calls] == [
        "C:\\",
        short_parent,
        short_leaf,
    ]


def test_windows_public_admission_marks_source_permissions_unverifiable(
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
    assert admitted.permissions_unverifiable is True
