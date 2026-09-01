"""Canonical host-local tree admission and aggregate identity contracts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

import pytest

from comfyui_docker_helper.config.planning.local_tree import (
    LocalTreeInventory,
    LocalTreeMember,
    canonical_local_tree_bytes,
    local_tree_digest,
)
from comfyui_docker_helper.filesystem import admission as file_admission
from comfyui_docker_helper.filesystem import windows as _windows_files


def test_empty_tree_uses_the_canonical_digest_vector() -> None:
    assert canonical_local_tree_bytes(LocalTreeInventory()) == (
        b'{"domain":"cdh-local-tree-sha256-v1","records":[{"kind":"directory",'
        b'"mode":"0755","path":"."}]}'
    )
    assert local_tree_digest(LocalTreeInventory()) == (
        "sha256:bfc5b459d61053042f6cc32617c7c26524963209696bbc6297794722dcabc95d"
    )


def test_inventory_is_sorted_by_utf8_path_and_requires_real_parents() -> None:
    members = (
        LocalTreeMember("é.txt", "file", 1, "sha256:" + "a" * 64),
        LocalTreeMember("nested", "directory"),
        LocalTreeMember("nested/é.txt", "file", 1, "sha256:" + "b" * 64),
    )
    inventory = LocalTreeInventory(
        tuple(
            sorted(members, key=lambda member: member.relative_path.as_posix().encode())
        )
    )

    assert [member.relative_path.as_posix() for member in inventory.members] == [
        "nested",
        "nested/é.txt",
        "é.txt",
    ]
    assert canonical_local_tree_bytes(inventory) == (
        b'{"domain":"cdh-local-tree-sha256-v1","records":[{"kind":"directory",'
        b'"mode":"0755","path":"."},{"kind":"directory","mode":"0755",'
        b'"path":"nested"},{"digest":"sha256:'
        b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        b'","kind":"file","mode":"0644","path":"nested/\\u00e9.txt",'
        b'"size":1},{"digest":"sha256:'
        b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        b'","kind":"file","mode":"0644","path":"\\u00e9.txt","size":1}]}'
    )
    with pytest.raises(ValueError, match="parents"):
        LocalTreeInventory(
            (LocalTreeMember("missing/file", "file", 1, "sha256:" + "a" * 64),)
        )


def test_inventory_rejects_non_member_types() -> None:
    with pytest.raises(ValueError, match="LocalTreeMember"):
        LocalTreeInventory(
            (
                {
                    "relative_path": "payload",
                    "kind": "file",
                },
            )
        )  # type: ignore[arg-type]


def test_local_tree_digest_rejects_unhashed_regular_members() -> None:
    inventory = LocalTreeInventory((LocalTreeMember("payload", "file"),))

    with pytest.raises(ValueError, match="require size and digest"):
        canonical_local_tree_bytes(inventory)


class _FakeWindowsDirectoryApi:
    def __init__(self) -> None:
        self.drive_type_calls: list[str] = []
        self.attribute_calls: list[str] = []

    def get_drive_type(self, root: str) -> int:
        self.drive_type_calls.append(root)
        return _windows_files._DRIVE_FIXED

    def get_file_attributes(self, path: str) -> int:
        self.attribute_calls.append(path)
        return _windows_files._FILE_ATTRIBUTE_DIRECTORY


def test_windows_directory_preflight_checks_drive_and_every_directory_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsDirectoryApi()
    monkeypatch.setattr(_windows_files, "_PyWin32Api", lambda: api)

    _windows_files.validate_local_directory_absolute_path("c:\\safe\\nested")

    assert api.drive_type_calls == ["C:\\"]
    assert api.attribute_calls == ["C:\\safe", "C:\\safe\\nested"]


def test_posix_admission_includes_hidden_and_empty_directories_and_optional_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    (source / ".hidden" / "empty").mkdir(parents=True)
    (source / "nested").mkdir()
    (source / ".hidden" / "payload").write_bytes(b"hidden")
    (source / "nested" / "payload").write_bytes(b"nested")

    unlocked = file_admission.admit_local_tree(source, content_lock=False)
    assert [item.relative_path.as_posix() for item in unlocked.members] == [
        ".hidden",
        ".hidden/empty",
        ".hidden/payload",
        "nested",
        "nested/payload",
    ]
    assert all(
        item.size is None and item.digest is None
        for item in unlocked.members
        if item.kind == "file"
    )

    locked = file_admission.admit_local_tree(source, content_lock=True)
    payload = next(
        item
        for item in locked.members
        if item.relative_path.as_posix() == "nested/payload"
    )
    assert payload.size == len(b"nested")
    assert payload.digest == f"sha256:{hashlib.sha256(b'nested').hexdigest()}"
    assert locked != unlocked


@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX symlink and special nodes"
)
def test_tree_admission_rejects_links_special_nodes_and_reserved_members(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    (source / "linked").symlink_to(tmp_path / "outside")
    with pytest.raises(file_admission.TreeAdmissionError) as raised:
        file_admission.admit_local_tree(source)
    assert raised.value.code == "member_reparse"
    (source / "linked").unlink()

    (source / ".cdh-staging").mkdir()
    with pytest.raises(file_admission.TreeAdmissionError) as raised:
        file_admission.admit_local_tree(source)
    assert raised.value.code == "reserved_member"
    assert raised.value.relative_path == PurePosixPath(".cdh-staging")

    (source / ".cdh-staging").rmdir()
    fifo = source / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(file_admission.TreeAdmissionError) as raised:
        file_admission.admit_local_tree(source)
    assert raised.value.code == "member_type"


def test_tree_admission_surfaces_traversal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    (source / "payload").write_bytes(b"payload")

    def fail_scandir(_path: str) -> object:
        raise OSError("synthetic traversal failure")

    monkeypatch.setattr(file_admission.os, "scandir", fail_scandir)
    with pytest.raises(file_admission.TreeAdmissionError) as raised:
        file_admission.admit_local_tree(source)
    assert raised.value.code == "traversal_failed"


@pytest.mark.parametrize("kind", ["file", "tree"])
@pytest.mark.parametrize("locked", [False, True])
def test_local_admission_reads_content_only_to_establish_locked_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    locked: bool,
) -> None:
    source = tmp_path / "source"
    if kind == "tree":
        source.mkdir()
    payload = source / "payload" if kind == "tree" else source
    content = b"model content"
    payload.write_bytes(content)
    read_bytes = 0
    original_read = file_admission.AdmittedRegularFileReader.read_chunk

    def count_read(reader, limit=None):
        nonlocal read_bytes
        chunk = original_read(reader, limit)
        read_bytes += len(chunk)
        return chunk

    monkeypatch.setattr(
        file_admission.AdmittedRegularFileReader, "read_chunk", count_read
    )

    admitted = file_admission.admit_local_source(source, content_lock=locked)

    assert admitted.kind == kind
    if kind == "tree":
        assert admitted.tree is not None
        record = admitted.tree.members[0]
    else:
        assert admitted.file is not None
        record = admitted.file
    assert record.digest == (
        f"sha256:{hashlib.sha256(content).hexdigest()}" if locked else None
    )
    assert read_bytes == (len(content) if locked else 0)


def test_unlocked_admission_requires_a_successful_safe_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"payload")

    def fail_open(path, _operation):
        assert Path(path) == source
        raise PermissionError("synthetic open failure")

    monkeypatch.setattr(file_admission, "operate_regular_absolute_file", fail_open)

    with pytest.raises(file_admission.TreeAdmissionError) as raised:
        file_admission.admit_local_source(source)

    assert raised.value.code == "source_unreadable"


def test_tree_revalidation_observes_structure_without_reading_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    payload = source / "payload"
    payload.write_bytes(b"initial")
    expected = file_admission.admit_local_tree(source, content_lock=True)
    payload.write_bytes(b"different content and size")

    def reject_read(*_args, **_kwargs):
        pytest.fail("structural revalidation consumed file content")

    monkeypatch.setattr(
        file_admission.AdmittedRegularFileReader, "read_chunk", reject_read
    )

    observed = file_admission.revalidate_local_tree(source, expected)

    assert observed == LocalTreeInventory((LocalTreeMember("payload", "file"),))


@pytest.mark.parametrize("change", ["add", "remove", "change-kind"])
def test_tree_revalidation_rejects_changed_member_paths_or_kinds(
    tmp_path: Path,
    change: str,
) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    payload = source / "payload"
    payload.write_bytes(b"content")
    expected = file_admission.admit_local_tree(source, content_lock=True)
    if change == "add":
        (source / "new").mkdir()
        changed_path = "new"
    else:
        payload.unlink()
        if change == "change-kind":
            payload.mkdir()
        changed_path = "payload"

    with pytest.raises(file_admission.TreeAdmissionError) as raised:
        file_admission.revalidate_local_tree(source, expected)

    assert raised.value.code == "membership_drift"
    assert raised.value.relative_path == PurePosixPath(changed_path)
