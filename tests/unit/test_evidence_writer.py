"""Current anonymous evidence publication and failure contracts."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
from pathlib import Path

import pytest

from comfyui_docker_helper.container import evidence_writer
from comfyui_docker_helper.container.evidence_writer import (
    ApplicationEvidenceError,
    write_application_evidence,
)


# Evidence publication is anonymous, exclusive, descriptor-bound, and durable.
def test_anonymous_evidence_publish_is_exact_durable_and_leaves_no_temp_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    content = b'{"schema_version":1}\n'
    events: list[str] = []
    real_fchmod = os.fchmod
    real_fchown = os.fchown
    real_fsync = os.fsync
    real_publish = evidence_writer._publish_anonymous_file

    def record_fchmod(descriptor: int, mode: int) -> None:
        events.append("fchmod")
        real_fchmod(descriptor, mode)

    def record_fchown(descriptor: int, uid: int, gid: int) -> None:
        events.append("fchown")
        real_fchown(descriptor, uid, gid)

    def record_fsync(descriptor: int) -> None:
        kind = (
            "file-fsync" if stat.S_ISREG(os.fstat(descriptor).st_mode) else "dir-fsync"
        )
        events.append(kind)
        real_fsync(descriptor)

    def record_publish(file_fd: int, parent_fd: int, name: str) -> None:
        assert list(tmp_path.iterdir()) == []
        events.append("publish")
        real_publish(file_fd, parent_fd, name)

    monkeypatch.setattr(os, "fchmod", record_fchmod)
    monkeypatch.setattr(os, "fchown", record_fchown)
    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(evidence_writer, "_publish_anonymous_file", record_publish)
    write_application_evidence(
        path,
        content,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert events == ["fchmod", "fchown", "file-fsync", "publish", "dir-fsync"]
    assert path.read_bytes() == content
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_uid == os.getuid()
    assert metadata.st_gid == os.getgid()
    assert stat.S_IMODE(metadata.st_mode) == 0o444
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("kind", ["regular", "symlink", "fifo"])
def test_anonymous_publish_is_no_replace_for_every_existing_entry(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / "inventory.json"
    if kind == "regular":
        path.write_bytes(b"foreign")
    elif kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        os.mkfifo(path)
    before = path.lstat()

    with pytest.raises(ApplicationEvidenceError, match="target already exists"):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    after = path.lstat()
    assert (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
    )
    assert list(tmp_path.iterdir()) == [path]


def test_publish_callback_replacement_preserves_foreign_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    real_publish = evidence_writer._publish_anonymous_file

    def publish_then_replace(file_fd: int, parent_fd: int, name: str) -> None:
        real_publish(file_fd, parent_fd, name)
        path.unlink()
        path.write_bytes(b"foreign")

    monkeypatch.setattr(
        evidence_writer,
        "_publish_anonymous_file",
        publish_then_replace,
    )
    with pytest.raises(ApplicationEvidenceError, match="target identity changed"):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert path.read_bytes() == b"foreign"
    assert list(tmp_path.iterdir()) == [path]


def test_post_publish_aba_boundary_detects_same_content_replacement_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    content = b"same bytes\n"
    real_verify = evidence_writer._verify_open_file
    calls = 0

    def replace_after_first_target_check(*args) -> None:
        nonlocal calls
        calls += 1
        real_verify(*args)
        if calls == 2:
            path.unlink()
            path.write_bytes(content)
            path.chmod(0o444)

    monkeypatch.setattr(
        evidence_writer,
        "_verify_open_file",
        replace_after_first_target_check,
    )
    with pytest.raises(ApplicationEvidenceError, match="target identity changed"):
        write_application_evidence(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert path.read_bytes() == content
    assert stat.S_IMODE(path.lstat().st_mode) == 0o444
    assert list(tmp_path.iterdir()) == [path]


def test_directory_fsync_failure_preserves_exact_published_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    content = b'{"schema_version":1}\n'
    real_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(ApplicationEvidenceError, match=r"errno 5.*Input/output error"):
        write_application_evidence(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    metadata = path.lstat()
    assert path.read_bytes() == content
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_uid == os.getuid()
    assert metadata.st_gid == os.getgid()
    assert stat.S_IMODE(metadata.st_mode) == 0o444
    assert list(tmp_path.iterdir()) == [path]


def test_unsupported_otmpfile_fails_without_named_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    monkeypatch.setattr(evidence_writer, "_O_TMPFILE", None)

    with pytest.raises(ApplicationEvidenceError, match="O_TMPFILE is unavailable"):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert list(tmp_path.iterdir()) == []


def test_filesystem_without_otmpfile_support_fails_without_named_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    real_open = os.open

    def reject_anonymous_open(file, flags, *args, **kwargs):
        if (
            evidence_writer._O_TMPFILE is not None
            and flags & evidence_writer._O_TMPFILE == evidence_writer._O_TMPFILE
        ):
            raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", reject_anonymous_open)
    with pytest.raises(
        ApplicationEvidenceError,
        match=r"O_TMPFILE is unsupported.*errno 95",
    ):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert list(tmp_path.iterdir()) == []


def test_unsupported_linkat_fails_closed_without_named_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"

    def unsupported_linkat(*_args) -> int:
        ctypes.set_errno(errno.ENOSYS)
        return -1

    monkeypatch.setattr(evidence_writer, "_load_linkat", lambda: unsupported_linkat)
    with pytest.raises(
        ApplicationEvidenceError,
        match=r"linkat\(AT_EMPTY_PATH\).*unsupported.*errno 38",
    ):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert list(tmp_path.iterdir()) == []
