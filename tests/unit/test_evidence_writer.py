from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from comfyui_docker_helper.container import evidence_writer
from comfyui_docker_helper.container.evidence_writer import (
    EvidenceFileError,
    write_application_evidence,
)


def test_evidence_creation_uses_fd_verification_and_exact_durability_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    content = b'{"schema_version":1}\n'
    events: list[str] = []
    real_fchmod = os.fchmod
    real_fchown = os.fchown
    real_fsync = os.fsync
    real_link = os.link

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

    def record_link(source, target, **kwargs) -> None:
        events.append("link")
        real_link(source, target, **kwargs)

    monkeypatch.setattr(os, "fchmod", record_fchmod)
    monkeypatch.setattr(os, "fchown", record_fchown)
    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "link", record_link)
    with monkeypatch.context() as path_access:
        path_access.setattr(
            Path,
            "read_bytes",
            lambda item: pytest.fail(f"evidence target reopened: {item}"),
        )
        write_application_evidence(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert events == ["fchmod", "fchown", "file-fsync", "link", "dir-fsync"]
    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o777 == 0o444
    with pytest.raises(EvidenceFileError, match="target already exists"):
        write_application_evidence(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_evidence_rejects_special_preexisting_target_without_opening_it(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / "inventory.json"
    if kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        os.mkfifo(path)

    with pytest.raises(EvidenceFileError, match="target already exists"):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_evidence_verification_failure_cleans_owned_target_and_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    real_verify = evidence_writer._verify_open_file
    calls = 0

    def fail_after_link(*args) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise EvidenceFileError("evidence verification failed")
        real_verify(*args)

    monkeypatch.setattr(evidence_writer, "_verify_open_file", fail_after_link)
    with pytest.raises(EvidenceFileError, match="verification failed"):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert not path.exists()
    assert not list(tmp_path.glob(".inventory.json.*"))


def test_evidence_observable_replacement_race_preserves_foreign_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    real_verify = evidence_writer._verify_open_file
    calls = 0

    def replace_after_first_target_binding(*args) -> None:
        nonlocal calls
        calls += 1
        real_verify(*args)
        if calls == 2:
            path.unlink()
            path.write_bytes(b"replacement")

    monkeypatch.setattr(
        evidence_writer,
        "_verify_open_file",
        replace_after_first_target_binding,
    )
    with pytest.raises(EvidenceFileError, match="target identity changed"):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert path.read_bytes() == b"replacement"
    assert not list(tmp_path.glob(".inventory.json.*"))


def test_evidence_cleanup_never_unlinks_replacement_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.json"
    real_require_identity = evidence_writer._require_path_identity
    replacement_temporary: Path | None = None

    def replace_temporary(item: Path, identity, subject: str) -> None:
        nonlocal replacement_temporary
        if subject == "temporary" and replacement_temporary is None:
            item.unlink()
            item.write_bytes(b"temporary replacement")
            replacement_temporary = item
        real_require_identity(item, identity, subject)

    monkeypatch.setattr(
        evidence_writer,
        "_require_path_identity",
        replace_temporary,
    )
    with pytest.raises(EvidenceFileError, match="temporary identity changed"):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert replacement_temporary is not None
    assert replacement_temporary.read_bytes() == b"temporary replacement"
    assert not path.exists()


@pytest.mark.parametrize("kind", ["regular", "symlink", "fifo"])
def test_evidence_link_race_fails_without_unlinking_foreign_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    path = tmp_path / "inventory.json"

    def replace_link(_source, target, **_kwargs) -> None:
        target = Path(target)
        if kind == "regular":
            target.write_bytes(b"link replacement")
        elif kind == "symlink":
            target.symlink_to(tmp_path / "missing")
        else:
            os.mkfifo(target)

    monkeypatch.setattr(os, "link", replace_link)
    with pytest.raises(EvidenceFileError, match="linked identity changed"):
        write_application_evidence(
            path,
            b"expected\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    metadata = path.lstat()
    assert {
        "regular": stat.S_ISREG,
        "symlink": stat.S_ISLNK,
        "fifo": stat.S_ISFIFO,
    }[kind](metadata.st_mode)
