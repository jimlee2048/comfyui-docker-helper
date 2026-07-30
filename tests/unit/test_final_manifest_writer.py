"""Final-manifest file creation contracts."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from comfyui_docker_helper.container import final_manifest_writer
from comfyui_docker_helper.container.final_manifest_writer import (
    FinalManifestWriteError,
    write_final_manifest_file,
)


def test_final_manifest_writer_creates_exact_read_only_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    content = b'{"schema_version":1}\n'
    target_open: tuple[str, int, tuple[object, ...]] | None = None
    real_open = os.open

    def observe_open(file, flags, *args, **kwargs):
        nonlocal target_open
        if kwargs.get("dir_fd") is not None:
            target_open = (file, flags, args)
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(final_manifest_writer.os, "open", observe_open)
    write_final_manifest_file(
        path,
        content,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert target_open is not None
    name, flags, mode = target_open
    assert name == "manifest.json"
    assert flags & (
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    ) == (os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC)
    assert mode == (0o600,)
    assert path.read_bytes() == content
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_uid == os.getuid()
    assert metadata.st_gid == os.getgid()
    assert stat.S_IMODE(metadata.st_mode) == 0o444
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("kind", ["regular", "symlink", "directory"])
def test_final_manifest_writer_never_replaces_an_occupied_target(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / "manifest.json"
    if kind == "regular":
        path.write_bytes(b"foreign")
    elif kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.mkdir()
    before = path.lstat()

    with pytest.raises(FinalManifestWriteError, match="target already exists"):
        write_final_manifest_file(
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
    if kind == "regular":
        assert path.read_bytes() == b"foreign"
    elif kind == "symlink":
        assert path.readlink() == tmp_path / "missing"


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_final_manifest_writer_rejects_an_invalid_parent(
    tmp_path: Path,
    kind: str,
) -> None:
    parent = tmp_path / "build"
    if kind == "file":
        parent.write_bytes(b"not a directory")
    elif kind == "symlink":
        real_parent = tmp_path / "real-build"
        real_parent.mkdir()
        parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(FinalManifestWriteError, match=r"errno \d+"):
        write_final_manifest_file(
            parent / "manifest.json",
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    if kind == "symlink":
        assert list(real_parent.iterdir()) == []


def test_final_manifest_writer_detects_identity_substitution_without_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    real_stat = os.stat
    substituted = False

    def substitute_before_stat(file, *args, **kwargs):
        nonlocal substituted
        if (
            not substituted
            and file == "manifest.json"
            and kwargs.get("dir_fd") is not None
        ):
            substituted = True
            path.unlink()
            path.write_bytes(b"foreign")
        return real_stat(file, *args, **kwargs)

    monkeypatch.setattr(final_manifest_writer.os, "stat", substitute_before_stat)
    with pytest.raises(FinalManifestWriteError, match="target identity changed"):
        write_final_manifest_file(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert substituted
    assert path.read_bytes() == b"foreign"
    assert list(tmp_path.iterdir()) == [path]


def test_final_manifest_writer_succeeds_when_otmpfile_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    content = b"expected\n"
    otmpfile = getattr(os, "O_TMPFILE", None)
    real_open = os.open

    def reject_anonymous_open(file, flags, *args, **kwargs):
        if otmpfile is not None and flags & otmpfile == otmpfile:
            raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(
        final_manifest_writer.os,
        "open",
        reject_anonymous_open,
    )
    write_final_manifest_file(
        path,
        content,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert path.read_bytes() == content


def test_final_manifest_writer_surfaces_post_creation_error_without_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"

    def fail_fchown(_descriptor: int, _uid: int, _gid: int) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(final_manifest_writer.os, "fchown", fail_fchown)
    with pytest.raises(
        FinalManifestWriteError,
        match=r"errno 5.*Input/output error",
    ):
        write_final_manifest_file(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert path.exists()
    assert list(tmp_path.iterdir()) == [path]
