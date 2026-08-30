"""Durable target-matrix and filesystem-safety tests for shared transfers."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_docker_helper.container.transfer import core as transfer_core
from comfyui_docker_helper.container.transfer.core import (
    DownloadFilesError,
    DownloadStatus,
    ResumeAuthority,
    StagingDisposition,
    TerminalTransferDownloadFilesError,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportRetryable,
    TransportSuccess,
    VerificationStatus,
    transfer_file,
    transfer_staging_target,
    verify_required_final,
)

from ._transfer_core_support import (
    BytesBackend,
    _checksum,
    _preserved_request,
    _request,
    _settings,
)


@pytest.mark.parametrize(
    ("existing", "checksum_kind", "overwrite", "status", "verified", "calls"),
    [
        (None, None, False, DownloadStatus.DOWNLOADED, False, 1),
        (None, "new", True, DownloadStatus.DOWNLOADED, True, 1),
        (b"old", None, False, DownloadStatus.SKIPPED, False, 0),
        (b"old", None, True, DownloadStatus.DOWNLOADED, False, 1),
        (b"new", "new", False, DownloadStatus.SKIPPED, True, 0),
        (b"new", "new", True, DownloadStatus.SKIPPED, True, 0),
        (b"old", "new", True, DownloadStatus.DOWNLOADED, True, 1),
    ],
)
def test_transfer_core_applies_existing_target_matrix(
    tmp_path: Path,
    existing: bytes | None,
    checksum_kind: str | None,
    overwrite: bool,
    status: DownloadStatus,
    verified: bool,
    calls: int,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(
        root,
        overwrite=overwrite,
        checksum=_checksum(b"new") if checksum_kind else None,
    )
    if existing is not None:
        request.target.parent.mkdir()
        request.target.write_bytes(existing)
    backend = BytesBackend(b"new")

    outcome = transfer_file(request, backend=backend, settings=_settings())

    assert outcome.status is status
    assert outcome.verification is (
        VerificationStatus.VERIFIED if verified else VerificationStatus.UNVERIFIED
    )
    assert outcome.observed_checksum == (_checksum(b"new") if verified else None)
    assert outcome.observed_length == len(
        b"new" if status == "downloaded" else existing
    )
    assert request.target.read_bytes() == (b"new" if calls else existing)
    assert len(backend.calls) == calls
    assert not outcome.staging_target.exists()


def test_existing_checksum_mismatch_without_overwrite_is_terminal_and_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "models" / "model.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    request = _request(root, checksum=_checksum(b"new"))
    backend = BytesBackend(b"new")

    with pytest.raises(
        TerminalTransferDownloadFilesError,
        match=r"existing.*checksum",
    ):
        transfer_file(request, backend=backend, settings=_settings())

    assert target.read_bytes() == b"old"
    assert backend.calls == []
    assert not transfer_staging_target(request).parent.exists()


@pytest.mark.parametrize(
    ("checksum", "overwrite"),
    [(None, False), (_checksum(b"existing"), True)],
)
def test_existing_target_skip_exactly_discards_admitted_resume_artifacts(
    tmp_path: Path,
    checksum: str | None,
    overwrite: bool,
) -> None:
    request = _preserved_request(tmp_path / "ComfyUI", checksum=checksum)
    request = replace(request, overwrite=overwrite)
    request.target.parent.mkdir(parents=True, exist_ok=True)
    request.target.write_bytes(b"existing")
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")
    backend = BytesBackend(b"unused")

    outcome = transfer_file(request, backend=backend, settings=_settings())

    assert outcome.status is DownloadStatus.SKIPPED
    assert backend.calls == []
    assert not staging.exists()
    assert not control.exists()
    assert request.target.read_bytes() == b"existing"


def test_existing_target_skip_durably_accepts_missing_staging_namespace(
    tmp_path: Path,
) -> None:
    request = replace(_preserved_request(tmp_path / "ComfyUI"), overwrite=False)
    request.target.parent.mkdir(parents=True, exist_ok=True)
    request.target.write_bytes(b"existing")
    staging = transfer_staging_target(request)
    Path(f"{staging}.aria2").unlink()
    staging.unlink()
    staging.parent.rmdir()
    backend = BytesBackend(b"unused")

    outcome = transfer_file(request, backend=backend, settings=_settings())

    assert outcome.status is DownloadStatus.SKIPPED
    assert backend.calls == []
    assert not staging.parent.exists()
    assert request.target.read_bytes() == b"existing"


def test_existing_target_skip_fails_closed_when_exact_discard_identity_drifts(
    tmp_path: Path,
) -> None:
    request = _preserved_request(tmp_path / "ComfyUI")
    request = replace(request, overwrite=False)
    request.target.parent.mkdir(parents=True, exist_ok=True)
    request.target.write_bytes(b"existing")
    staging = transfer_staging_target(request)
    replacement = staging.with_name(f"{staging.name}.foreign")
    replacement.write_bytes(b"foreign replacement")
    os.replace(replacement, staging)
    control = Path(f"{staging}.aria2")
    backend = BytesBackend(b"unused")

    with pytest.raises(DownloadFilesError, match="identity does not match authority"):
        transfer_file(request, backend=backend, settings=_settings())

    assert backend.calls == []
    assert staging.read_bytes() == b"foreign replacement"
    assert control.read_bytes() == b"aria2 control"
    assert request.resume_authority is not None
    assert request.target.read_bytes() == b"existing"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TransportDiagnostic("unknown", "summary"),
        lambda: TransportDiagnostic("httpx", ""),
        lambda: TransportDiagnostic("aria2", "   "),
        lambda: TransportDiagnostic("httpx", 1),
        lambda: TransportSuccess(length=1, namespace="httpx", http_status=404),
        lambda: TransportSuccess(length=1, namespace="aria2", http_status=200),
        lambda: TransportSuccess(length=1, namespace="httpx", http_status=None),
        lambda: TransportRetryable(
            TransportDiagnostic("httpx", "retry"), http_status=404
        ),
        lambda: TransportRetryable(
            TransportDiagnostic("aria2", "retry"), http_status=503
        ),
        lambda: TransportOrdinaryTerminal(
            TransportDiagnostic("httpx", "terminal"), http_status=408
        ),
        lambda: TransportOrdinaryTerminal(
            TransportDiagnostic("httpx", "terminal"), http_status=503
        ),
        lambda: TransportOrdinaryTerminal(
            TransportDiagnostic("aria2", "terminal"), http_status=404
        ),
    ],
)
def test_transport_outcomes_reject_semantically_invalid_combinations(
    factory: Callable[[], object],
) -> None:
    """Typed outcomes admit only backend-capable status/category combinations."""
    with pytest.raises(ValueError):
        factory()


def test_preexisting_aria2_temp_fails_before_backend_call(tmp_path: Path) -> None:
    """CLEAN admission rejects an existing temp leaf before mutation starts."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    temp = Path(f"{staging}.aria2__temp")
    temp.parent.mkdir(parents=True)
    temp.write_bytes(b"foreign")
    backend = BytesBackend(b"content")

    with pytest.raises(DownloadFilesError, match="temporary control artifact"):
        transfer_file(request, backend=backend, settings=_settings())

    assert backend.calls == []
    assert temp.read_bytes() == b"foreign"


def test_terminal_artifacts_require_effective_uid_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal capture rejects transfer artifacts outside the effective UID."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")
    admitted_uid = os.geteuid()
    observed_uid = admitted_uid
    monkeypatch.setattr(transfer_core.os, "geteuid", lambda: observed_uid)

    class OwnerDriftBackend(BytesBackend):
        def download(self, transport, settings):
            nonlocal observed_uid
            outcome = super().download(transport, settings)
            control.write_bytes(b"control")
            observed_uid = admitted_uid + 1
            return TransportSuccess(length=outcome.length, namespace="aria2")

    with pytest.raises(DownloadFilesError, match="unexpected owner"):
        transfer_file(
            request,
            backend=OwnerDriftBackend(b"partial"),
            settings=_settings(),
        )

    assert control.read_bytes() == b"control"
    assert staging.read_bytes() == b"partial"


@pytest.mark.parametrize("drift", ["root", "parent"])
def test_control_admission_rejects_anchored_directory_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    """Control generations cannot survive root or target-parent identity drift."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    outside = tmp_path / "outside"
    outside.mkdir()
    detached = tmp_path / "detached-root" if drift == "root" else root / "detached"

    class DirectoryDriftBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            Path(f"{transport.sink.display_path}.aria2").write_bytes(b"control")
            if drift == "root":
                root.rename(detached)
                root.symlink_to(outside, target_is_directory=True)
            else:
                (root / "models").rename(detached)
                (root / "models").symlink_to(outside, target_is_directory=True)
            return TransportSuccess(length=outcome.length, namespace="aria2")

    with pytest.raises(DownloadFilesError, match="directory changed"):
        transfer_file(
            request,
            backend=DirectoryDriftBackend(b"partial"),
            settings=_settings(),
        )

    detached_staging = (
        detached / staging.relative_to(root)
        if drift == "root"
        else detached / ".cdh-staging" / staging.name
    )
    assert not detached_staging.exists()
    assert Path(f"{detached_staging}.aria2").read_bytes() == b"control"
    assert not (outside / "model.bin").exists()


def test_fresh_transfer_rejects_foreign_regular_staging(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"foreign")

    with pytest.raises(DownloadFilesError, match="foreign download staging"):
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    assert staging.read_bytes() == b"foreign"


def test_preserved_staging_rejects_hardlink_alias(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"final")
    staging = transfer_staging_target(request)
    staging.parent.mkdir()
    os.link(request.target, staging)
    metadata = staging.stat()
    digest = staging.name.removeprefix("cdh-").removesuffix(".part")
    request = replace(
        request,
        staging_disposition=StagingDisposition.PRESERVE,
        resume_authority=ResumeAuthority(
            identity_digest=f"sha256:{digest}",
            staging_device=metadata.st_dev,
            staging_inode=metadata.st_ino,
        ),
    )

    with pytest.raises(DownloadFilesError, match="unaliased regular"):
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    assert request.target.read_bytes() == b"final"
    assert staging.read_bytes() == b"final"


def test_preserved_staging_rejects_wrong_owner_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _preserved_request(root)
    staging = transfer_staging_target(request)
    backend = BytesBackend(b"new")
    actual_uid = staging.stat().st_uid
    monkeypatch.setattr(transfer_core.os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(DownloadFilesError, match="unexpected owner"):
        transfer_file(request, backend=backend, settings=_settings())

    assert backend.calls == []
    assert staging.read_bytes() == b"prior partial"
    assert Path(f"{staging}.aria2").read_bytes() == b"aria2 control"


def test_preserved_control_rejects_foreign_hardlink(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _preserved_request(root)
    staging = transfer_staging_target(request)
    foreign = tmp_path / "foreign-control"
    foreign.write_bytes(b"foreign")
    control = Path(f"{staging}.aria2")
    control.unlink()
    os.link(foreign, control)
    metadata = control.stat()
    assert request.resume_authority is not None
    request = replace(
        request,
        resume_authority=replace(
            request.resume_authority,
            control_device=metadata.st_dev,
            control_inode=metadata.st_ino,
        ),
    )

    with pytest.raises(DownloadFilesError, match="unaliased regular"):
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    assert foreign.read_bytes() == b"foreign"
    assert control.read_bytes() == b"foreign"


def test_unsafe_new_control_never_redirects_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    external = tmp_path / "external-control"
    external.write_bytes(b"external")

    class UnsafeControlBackend(BytesBackend):
        def download(self, request, settings) -> TransportSuccess:
            result = super().download(request, settings)
            Path(f"{request.sink.display_path}.aria2").symlink_to(external)
            return result

    with pytest.raises(DownloadFilesError, match="unauthorized control artifact"):
        transfer_file(
            request,
            backend=UnsafeControlBackend(b"partial"),
            settings=_settings(),
        )

    assert external.read_bytes() == b"external"
    assert not transfer_staging_target(request).exists()
    assert Path(f"{transfer_staging_target(request)}.aria2").is_symlink()


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
def test_non_regular_final_fails_before_transport(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "models" / "model.bin"
    target.parent.mkdir(parents=True)
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(tmp_path / "outside")
    else:
        os.mkfifo(target)
    backend = BytesBackend(b"new")

    with pytest.raises(DownloadFilesError, match="not a regular file"):
        transfer_file(
            _request(root, overwrite=True), backend=backend, settings=_settings()
        )

    assert backend.calls == []


@pytest.mark.parametrize(
    ("checksum_kind", "overwrite"),
    [
        (None, False),
        (None, True),
        ("match", False),
        ("match", True),
        ("mismatch", False),
        ("mismatch", True),
    ],
)
def test_hardlinked_existing_final_fails_every_target_matrix_admission(
    tmp_path: Path,
    checksum_kind: str | None,
    overwrite: bool,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(
        root,
        overwrite=overwrite,
        checksum=(
            _checksum(b"old" if checksum_kind == "match" else b"new")
            if checksum_kind is not None
            else None
        ),
    )
    request.target.parent.mkdir(parents=True)
    external = tmp_path / "external.bin"
    external.write_bytes(b"old")
    os.link(external, request.target)
    backend = BytesBackend(b"new")

    with pytest.raises(DownloadFilesError, match="unaliased regular file"):
        transfer_file(request, backend=backend, settings=_settings())

    assert backend.calls == []
    assert request.target.read_bytes() == b"old"
    assert external.read_bytes() == b"old"


def test_required_final_rejects_hardlink_alias(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "models" / "model.bin"
    target.parent.mkdir(parents=True)
    external = tmp_path / "external.bin"
    external.write_bytes(b"content")
    os.link(external, target)

    with pytest.raises(DownloadFilesError, match="unaliased regular file"):
        verify_required_final(root=root, target=target, expected_checksum=None)

    assert target.read_bytes() == b"content"
    assert external.read_bytes() == b"content"


def test_existing_checksum_open_race_to_fifo_is_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, checksum=_checksum(b"old"))
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    backend = BytesBackend(b"new")
    original_open = transfer_core.os.open
    injected = False

    def replace_with_fifo_before_open(path, flags, *args, **kwargs):
        nonlocal injected
        if (
            path == request.target.name
            and kwargs.get("dir_fd") is not None
            and not injected
        ):
            injected = True
            request.target.unlink()
            os.mkfifo(request.target)
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(transfer_core.os, "open", replace_with_fifo_before_open)

    with pytest.raises(DownloadFilesError, match="not a regular file"):
        transfer_file(request, backend=backend, settings=_settings())

    assert backend.calls == []
    assert stat.S_ISFIFO(request.target.lstat().st_mode)


def test_required_final_open_race_to_fifo_is_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "models" / "model.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    original_open = transfer_core.os.open
    injected = False

    def replace_with_fifo_before_open(path, flags, *args, **kwargs):
        nonlocal injected
        if path == target.name and kwargs.get("dir_fd") is not None and not injected:
            injected = True
            target.unlink()
            os.mkfifo(target)
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(transfer_core.os, "open", replace_with_fifo_before_open)

    with pytest.raises(DownloadFilesError, match="not a regular file"):
        verify_required_final(
            root=root,
            target=target,
            expected_checksum=_checksum(b"old"),
        )

    assert stat.S_ISFIFO(target.lstat().st_mode)


def test_reserved_staging_final_fails_before_parent_or_transport_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = replace(
        _request(root), target=root / "models" / ".cdh-staging" / "model.bin"
    )
    backend = BytesBackend(b"new")

    with pytest.raises(DownloadFilesError, match="reserved staging path component"):
        transfer_file(request, backend=backend, settings=_settings())

    assert backend.calls == []
    assert not (root / "models").exists()


def test_symlinked_parent_fails_before_staging_or_transport(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "models").symlink_to(outside, target_is_directory=True)
    backend = BytesBackend(b"new")

    with pytest.raises(DownloadFilesError, match="not a real directory"):
        transfer_file(_request(root), backend=backend, settings=_settings())

    assert backend.calls == []
    assert tuple(outside.iterdir()) == ()


def test_unsafe_staging_leaf_is_rejected_without_removal(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    (root / "models" / ".cdh-staging").mkdir(parents=True)
    request = _request(root)
    staging = transfer_staging_target(request)
    staging.symlink_to(tmp_path / "outside")
    backend = BytesBackend(b"new")

    with pytest.raises(DownloadFilesError, match="staging artifact"):
        transfer_file(request, backend=backend, settings=_settings())

    assert staging.is_symlink()
    assert backend.calls == []


def test_staging_identity_changes_with_content_identity_only(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root, checksum=_checksum(b"one"))

    assert transfer_staging_target(request) == transfer_staging_target(
        replace(
            request, overwrite=True, staging_disposition=StagingDisposition.PRESERVE
        )
    )
    assert transfer_staging_target(request) != transfer_staging_target(
        replace(request, expected_checksum=_checksum(b"two"))
    )
