"""Durable target-matrix and filesystem-safety tests for shared transfers."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_docker_helper.container import transfer_core
from comfyui_docker_helper.container.transfer_core import (
    Aria2DownloadSettings,
    DownloadCancelled,
    DownloaderSettings,
    DownloadFilesError,
    DownloadStatus,
    FileTransferRequest,
    HttpxDownloadSettings,
    ResumeAuthority,
    StagingDisposition,
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
    TransportCancelled,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportOutcome,
    TransportRequest,
    TransportResumeRejected,
    TransportRetryable,
    TransportSuccess,
    VerificationStatus,
    transfer_file,
    transfer_staging_target,
    verify_required_final,
)


class BytesBackend:
    """Write controlled bytes only to the staging path supplied by the core."""

    def __init__(
        self,
        content: bytes,
        *,
        error: Exception | None = None,
        outcome: TransportOutcome | None = None,
    ) -> None:
        self.content = content
        self.error = error
        self.outcome = outcome
        self.calls: list[TransportRequest] = []

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        del settings
        self.calls.append(request)
        with request.sink.open_for_write() as output:
            output.write(self.content)
        if self.error is not None:
            raise self.error
        if self.outcome is not None:
            return self.outcome
        return TransportSuccess(
            length=len(self.content), namespace="httpx", http_status=200
        )


def _settings() -> DownloaderSettings:
    return DownloaderSettings(
        default="httpx",
        aria2=Aria2DownloadSettings(
            rpc_port=6800,
            split=16,
            max_connection_per_server=16,
            min_split_size="1M",
            resume_download=True,
        ),
        httpx=HttpxDownloadSettings(timeout=60),
    )


def _checksum(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _request(
    root: Path,
    *,
    overwrite: bool = False,
    checksum: str | None = None,
    disposition: StagingDisposition = StagingDisposition.CLEAN,
    resume_authority: ResumeAuthority | None = None,
) -> FileTransferRequest:
    return FileTransferRequest(
        root=root,
        url="https://example.test/model.bin",
        target=root / "models" / "model.bin",
        overwrite=overwrite,
        expected_checksum=checksum,
        staging_disposition=disposition,
        resume_authority=resume_authority,
    )


def _preserved_request(
    root: Path,
    *,
    checksum: str | None = None,
) -> FileTransferRequest:
    request = _request(root, overwrite=True, checksum=checksum)
    staging = transfer_staging_target(request)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"prior partial")
    metadata = staging.stat()
    control = Path(f"{staging}.aria2")
    control.write_bytes(b"aria2 control")
    control_metadata = control.stat()
    return replace(
        request,
        staging_disposition=StagingDisposition.PRESERVE,
        resume_authority=ResumeAuthority(
            identity_digest=f"sha256:{staging.name.removeprefix('cdh-').removesuffix('.part')}",
            staging_device=metadata.st_dev,
            staging_inode=metadata.st_ino,
            control_device=control_metadata.st_dev,
            control_inode=control_metadata.st_ino,
        ),
    )


# The target matrix is the business authority for skip, replace, and integrity.
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


def test_failed_replacement_preserves_old_final_and_cleans_only_owned_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "models" / "model.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    foreign = target.parent / "foreign.part"
    foreign.write_bytes(b"foreign")
    request = _request(root, overwrite=True)
    backend = BytesBackend(
        b"partial",
        error=TransferDownloadFilesError("network interrupted"),
    )

    with pytest.raises(TransferDownloadFilesError, match="network interrupted"):
        transfer_file(request, backend=backend, settings=_settings())

    assert target.read_bytes() == b"old"
    assert foreign.read_bytes() == b"foreign"
    assert not transfer_staging_target(request).exists()


# The core projects semantic adapter outcomes without backend-specific policy.
@pytest.mark.parametrize(
    ("outcome", "expected_error"),
    [
        (
            TransportRetryable(
                TransportDiagnostic("httpx", "remote retryable failure")
            ),
            TransferDownloadFilesError,
        ),
        (
            TransportOrdinaryTerminal(
                TransportDiagnostic("aria2", "remote terminal failure")
            ),
            TerminalTransferDownloadFilesError,
        ),
        (
            TransportCancelled(TransportDiagnostic("httpx", "download cancelled")),
            DownloadCancelled,
        ),
    ],
)
def test_transfer_core_projects_non_success_transport_outcomes(
    tmp_path: Path,
    outcome: TransportRetryable | TransportOrdinaryTerminal | TransportCancelled,
    expected_error: type[DownloadFilesError],
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)

    class OutcomeBackend:
        def download(self, transport_request, settings):
            del transport_request, settings
            return outcome

    with pytest.raises(expected_error):
        transfer_file(request, backend=OutcomeBackend(), settings=_settings())

    assert not request.target.exists()
    assert not transfer_staging_target(request).exists()


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


def test_transfer_core_revalidates_outcome_before_placement(tmp_path: Path) -> None:
    """A corrupted adapter result fails closed before staged bytes can be placed."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    outcome = TransportRetryable(
        TransportDiagnostic("httpx", "remote retryable failure"),
        http_status=503,
    )
    object.__setattr__(outcome, "http_status", 404)

    class InvalidOutcomeBackend:
        def download(self, transport_request, settings):
            del transport_request, settings
            return outcome

    with pytest.raises(DownloadFilesError, match="invalid outcome"):
        transfer_file(request, backend=InvalidOutcomeBackend(), settings=_settings())

    assert not request.target.exists()
    assert not transfer_staging_target(request).exists()


def test_checksum_mismatch_always_discards_invalid_preserved_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _preserved_request(root, checksum=_checksum(b"expected"))

    with pytest.raises(TransferDownloadFilesError, match="checksum"):
        transfer_file(
            request,
            backend=BytesBackend(
                b"invalid",
                outcome=TransportSuccess(length=7, namespace="aria2"),
            ),
            settings=_settings(),
        )

    assert not request.target.exists()
    assert not transfer_staging_target(request).exists()


def test_retryable_failure_can_preserve_exact_caller_owned_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = replace(_preserved_request(root), preserve_on_retryable=True)

    with pytest.raises(TransferDownloadFilesError) as raised:
        transfer_file(
            request,
            backend=BytesBackend(
                b"partial",
                outcome=TransportRetryable(
                    TransportDiagnostic("aria2", "temporary failure")
                ),
            ),
            settings=_settings(),
        )

    assert raised.value.resume_authority is not None
    assert transfer_staging_target(request).read_bytes() == b"partial"
    assert not request.target.exists()


def test_resume_rejection_cleans_exact_admitted_partial_before_projection(
    tmp_path: Path,
) -> None:
    """The core consumes resume rejection only after exact durable cleanup."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _preserved_request(root)

    class RejectedBackend:
        def download(self, transport, settings):
            del settings
            assert transport.sink.resume_allowed
            return TransportResumeRejected(
                TransportDiagnostic("aria2", "resume rejected")
            )

    with pytest.raises(DownloadFilesError, match="resume rejected"):
        transfer_file(request, backend=RejectedBackend(), settings=_settings())

    assert not transfer_staging_target(request).exists()
    assert not request.target.exists()


def test_resume_rejection_without_authority_fails_closed(tmp_path: Path) -> None:
    """A backend cannot project resume rejection from a clean core request."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)

    class ContradictoryBackend:
        def download(self, transport, settings):
            del transport, settings
            return TransportResumeRejected(
                TransportDiagnostic("aria2", "resume rejected")
            )

    with pytest.raises(DownloadFilesError, match="without exact admitted authority"):
        transfer_file(request, backend=ContradictoryBackend(), settings=_settings())

    assert not transfer_staging_target(request).exists()


def test_aria2_atomic_control_successor_becomes_resume_authority(
    tmp_path: Path,
) -> None:
    """The core, not the adapter, admits aria2's atomic-save successor inode."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = replace(_preserved_request(root), preserve_on_retryable=True)
    staging = transfer_staging_target(request)
    old_control = Path(f"{staging}.aria2")
    old_inode = old_control.stat().st_ino

    class AtomicSaveBackend:
        def download(self, transport, settings):
            del settings
            with transport.sink.display_path.open("ab") as output:
                output.write(b"more")
            temp = Path(f"{old_control}__temp")
            temp.write_bytes(b"new control")
            os.replace(temp, old_control)
            return TransportRetryable(TransportDiagnostic("aria2", "timeout"))

    with pytest.raises(TransferDownloadFilesError) as raised:
        transfer_file(request, backend=AtomicSaveBackend(), settings=_settings())

    authority = raised.value.resume_authority
    assert authority is not None
    assert authority.control_inode == old_control.stat().st_ino != old_inode
    assert staging.read_bytes() == b"prior partialmore"


def test_clean_aria2_success_cleans_admitted_first_control_generation(
    tmp_path: Path,
) -> None:
    """A first control generation is exact cleanup data, not leftover authority."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)

    class FirstGenerationBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            Path(f"{transport.sink.display_path}.aria2").write_bytes(b"control")
            return TransportSuccess(length=outcome.length, namespace="aria2")

    transfer_file(
        request,
        backend=FirstGenerationBackend(b"complete"),
        settings=_settings(),
    )

    assert request.target.read_bytes() == b"complete"
    assert not Path(f"{staging}.aria2").exists()


def test_safe_residual_aria2_temp_is_cleaned_then_fails_closed(tmp_path: Path) -> None:
    """A proven temp leaf is quarantined, but never accepted as resume state."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    temp = Path(f"{staging}.aria2__temp")

    class ResidualTempBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            temp.write_bytes(b"temporary control")
            return TransportSuccess(length=outcome.length, namespace="aria2")

    with pytest.raises(DownloadFilesError, match="temporary control artifact"):
        transfer_file(
            request,
            backend=ResidualTempBackend(b"partial"),
            settings=_settings(),
        )

    assert not temp.exists()
    assert not staging.exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_unsafe_residual_aria2_temp_is_not_deleted(
    tmp_path: Path,
    kind: str,
) -> None:
    """An unsafe temp leaf remains untouched when terminal reconciliation fails."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    temp = Path(f"{staging}.aria2__temp")
    external = tmp_path / "external-temp"
    external.write_bytes(b"external")

    class UnsafeTempBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            if kind == "symlink":
                temp.symlink_to(external)
            else:
                os.mkfifo(temp)
            return TransportSuccess(length=outcome.length, namespace="aria2")

    with pytest.raises(
        DownloadFilesError,
        match=r"cannot be opened safely|not an unaliased regular",
    ):
        transfer_file(
            request,
            backend=UnsafeTempBackend(b"partial"),
            settings=_settings(),
        )

    assert temp.exists() or temp.is_symlink()
    assert external.read_bytes() == b"external"
    assert not staging.exists()


def test_unquiescent_failure_never_admits_or_deletes_control_successor(
    tmp_path: Path,
) -> None:
    """An adapter exception cannot authorize successor adoption by filename."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _preserved_request(root)
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")

    class FailingAtomicSaveBackend:
        def download(self, transport, settings):
            del transport, settings
            temp = Path(f"{control}__temp")
            temp.write_bytes(b"unproven successor")
            os.replace(temp, control)
            raise DownloadFilesError("RPC disconnected")

    with pytest.raises(DownloadFilesError, match="RPC disconnected"):
        transfer_file(
            request,
            backend=FailingAtomicSaveBackend(),
            settings=_settings(),
        )

    assert control.read_bytes() == b"unproven successor"
    assert not staging.exists()


def test_resumed_success_accepts_unlinked_control_without_successor(
    tmp_path: Path,
) -> None:
    """Completed aria2 work may remove its old control before final placement."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _preserved_request(root)
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")

    class CompletedResumeBackend:
        def download(self, transport, settings):
            del settings
            with transport.sink.display_path.open("ab") as output:
                output.write(b"complete")
            control.unlink()
            return TransportSuccess(
                length=transport.sink.current_length(),
                namespace="aria2",
            )

    transfer_file(
        request,
        backend=CompletedResumeBackend(),
        settings=_settings(),
    )

    assert request.target.read_bytes() == b"prior partialcomplete"
    assert not control.exists()


@pytest.mark.parametrize("mutation", ["in_place", "hardlink"])
@pytest.mark.parametrize("outcome_kind", ["success", "retryable"])
def test_unchanged_held_control_rejects_metadata_drift(
    tmp_path: Path,
    mutation: str,
    outcome_kind: str,
) -> None:
    """Held control authority includes stable metadata, not only device/inode."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = replace(
        _preserved_request(root),
        preserve_on_retryable=outcome_kind == "retryable",
    )
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")
    alias = control.with_name(f"{control.name}.alias")

    class DriftBackend:
        def download(self, transport, settings):
            del settings
            with transport.sink.display_path.open("ab") as output:
                output.write(b"more")
            if mutation == "in_place":
                control.write_bytes(b"changed control bytes")
            else:
                os.link(control, alias)
            if outcome_kind == "retryable":
                return TransportRetryable(TransportDiagnostic("aria2", "timeout"))
            return TransportSuccess(
                length=transport.sink.current_length(),
                namespace="aria2",
            )

    with pytest.raises(DownloadFilesError, match=r"held control|unaliased regular"):
        transfer_file(request, backend=DriftBackend(), settings=_settings())

    assert control.exists()
    assert alias.exists() is (mutation == "hardlink")
    assert not staging.exists()


@pytest.mark.parametrize("outcome_kind", ["retryable", "cancelled"])
@pytest.mark.parametrize("preserve", [False, True])
def test_first_control_generation_follows_exact_disposition(
    tmp_path: Path,
    outcome_kind: str,
    preserve: bool,
) -> None:
    """A CLEAN item may preserve its first generation only on explicit handoff."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = replace(
        _request(root),
        preserve_on_retryable=preserve and outcome_kind == "retryable",
        preserve_on_cancellation=preserve and outcome_kind == "cancelled",
    )
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")

    class FirstGenerationBackend:
        def download(self, transport, settings):
            del settings
            with transport.sink.open_for_write() as output:
                output.write(b"partial")
            control.write_bytes(b"control")
            if outcome_kind == "retryable":
                return TransportRetryable(TransportDiagnostic("aria2", "timeout"))
            return TransportCancelled(TransportDiagnostic("aria2", "cancelled"))

    error_type = (
        TransferDownloadFilesError if outcome_kind == "retryable" else DownloadCancelled
    )
    with pytest.raises(error_type) as raised:
        transfer_file(
            request,
            backend=FirstGenerationBackend(),
            settings=_settings(),
        )

    assert (raised.value.resume_authority is not None) is preserve
    assert staging.exists() is preserve
    assert control.exists() is preserve


@pytest.mark.parametrize("outcome_kind", ["success", "terminal"])
def test_control_successor_applies_success_or_terminal_disposition(
    tmp_path: Path,
    outcome_kind: str,
) -> None:
    """An admitted successor is exact cleanup authority for either terminal path."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _preserved_request(root)
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")

    class SuccessorBackend:
        def download(self, transport, settings):
            del settings
            with transport.sink.display_path.open("ab") as output:
                output.write(b"more")
            temp = Path(f"{control}__temp")
            temp.write_bytes(b"successor")
            os.replace(temp, control)
            if outcome_kind == "terminal":
                return TransportOrdinaryTerminal(
                    TransportDiagnostic("aria2", "terminal")
                )
            return TransportSuccess(
                length=transport.sink.current_length(),
                namespace="aria2",
            )

    if outcome_kind == "terminal":
        with pytest.raises(TerminalTransferDownloadFilesError):
            transfer_file(request, backend=SuccessorBackend(), settings=_settings())
        assert not request.target.exists()
    else:
        transfer_file(request, backend=SuccessorBackend(), settings=_settings())
        assert request.target.read_bytes() == b"prior partialmore"
    assert not staging.exists()
    assert not control.exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_unsafe_aria2_control_generation_is_preserved_and_fails_closed(
    tmp_path: Path,
    kind: str,
) -> None:
    """Special control leaves are never adopted or deleted by the core."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")
    external = tmp_path / "external"
    external.write_bytes(b"external")

    class UnsafeGenerationBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            if kind == "symlink":
                control.symlink_to(external)
            else:
                os.mkfifo(control)
            return TransportSuccess(length=outcome.length, namespace="aria2")

    with pytest.raises(
        DownloadFilesError,
        match=r"cannot be opened safely|not an unaliased regular",
    ):
        transfer_file(
            request,
            backend=UnsafeGenerationBackend(b"partial"),
            settings=_settings(),
        )

    assert control.exists() or control.is_symlink()
    assert external.read_bytes() == b"external"
    assert not staging.exists()


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


def test_successor_requires_old_control_to_be_unlinked(tmp_path: Path) -> None:
    """A new named control is ambiguous while the held old generation stays linked."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _preserved_request(root)
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")
    old_alias = control.with_name(f"{control.name}.old")

    class LinkedOldBackend:
        def download(self, transport, settings):
            del settings
            control.rename(old_alias)
            control.write_bytes(b"new control")
            return TransportOrdinaryTerminal(TransportDiagnostic("aria2", "terminal"))

    with pytest.raises(DownloadFilesError, match="without unlinking"):
        transfer_file(request, backend=LinkedOldBackend(), settings=_settings())

    assert old_alias.read_bytes() == b"aria2 control"
    assert control.read_bytes() == b"new control"
    assert not staging.exists()


def test_first_control_generation_requires_effective_uid_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal capture rejects a regular control owned outside the admitted UID."""
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

    with pytest.raises(DownloadFilesError, match="effective user"):
        transfer_file(
            request,
            backend=OwnerDriftBackend(b"partial"),
            settings=_settings(),
        )

    assert control.read_bytes() == b"control"
    assert not staging.exists()


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


@pytest.mark.parametrize("failure", ["fsync", "post_fsync_drift"])
def test_control_generation_requires_durable_stable_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Directory durability and the post-barrier metadata recheck are mandatory."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")
    terminal = False
    triggered = False
    real_fsync = transfer_core.os.fsync

    def fail_or_drift(fd: int) -> None:
        nonlocal triggered
        if not terminal or triggered:
            real_fsync(fd)
            return
        triggered = True
        if failure == "fsync":
            raise OSError("injected control barrier failure")
        real_fsync(fd)
        replacement = control.with_name(f"{control.name}.replacement")
        replacement.write_bytes(b"post-fsync drift")
        os.replace(replacement, control)

    monkeypatch.setattr(transfer_core.os, "fsync", fail_or_drift)

    class ControlBackend(BytesBackend):
        def download(self, transport, settings):
            nonlocal terminal
            outcome = super().download(transport, settings)
            control.write_bytes(b"control")
            terminal = True
            return TransportSuccess(length=outcome.length, namespace="aria2")

    with pytest.raises(DownloadFilesError, match=r"not durable|changed during"):
        transfer_file(
            request,
            backend=ControlBackend(b"partial"),
            settings=_settings(),
        )

    assert control.exists()
    assert not staging.exists()


# Fresh work refuses ambiguous regular partials instead of deleting them.
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


# Resume authority cannot bless a hardlinked partial or final alias.
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


# Resume control state must also be an unaliased authority-bound inode.
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


# Unsafe control creation preserves foreign data but cleans the exact owned partial.
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
    request = replace(_request(root), target=root / "models" / ".cdh-staging")
    backend = BytesBackend(b"new")

    with pytest.raises(DownloadFilesError, match="reserved staging filename"):
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


# The aria2 proc-fd path remains anchored when an external process opens it.
def test_external_transport_path_uses_held_parent_descriptor(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    (root / "models").mkdir(parents=True)
    request = _request(root)
    detached = root / "detached-models"
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "source.bin"
    source.write_bytes(b"external transport")

    class ExternalBackend:
        def download(self, transport, settings) -> TransportSuccess:
            del settings
            (root / "models").rename(detached)
            (root / "models").symlink_to(outside, target_is_directory=True)
            subprocess.run(
                [
                    "cp",
                    str(source),
                    f"{transport.sink.aria2_directory}/{transport.sink.aria2_name}",
                ],
                check=True,
            )
            return TransportSuccess(
                length=transport.sink.current_length(),
                namespace="aria2",
                http_status=None,
            )

    with pytest.raises(DownloadFilesError, match="directory changed"):
        transfer_file(request, backend=ExternalBackend(), settings=_settings())

    assert tuple(outside.iterdir()) == ()
    assert not (detached / "model.bin").exists()


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


def test_transport_length_mismatch_is_retryable_and_never_placed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)

    class WrongLengthBackend(BytesBackend):
        def download(self, request, settings) -> TransportSuccess:
            super().download(request, settings)
            return TransportSuccess(length=999, namespace="httpx", http_status=200)

    with pytest.raises(TransferDownloadFilesError, match="length"):
        transfer_file(request, backend=WrongLengthBackend(b"new"), settings=_settings())

    assert not request.target.exists()
    assert not transfer_staging_target(request).exists()


# Conditional placement must preserve a final that appears after preflight.
def test_missing_target_race_is_preserved_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    original = transfer_core._renameat2
    injected = False

    def race_then_place(*args, flags: int) -> None:
        nonlocal injected
        if flags == 1 and not injected:
            injected = True
            request.target.write_bytes(b"racing")
        original(*args, flags=flags)

    monkeypatch.setattr(transfer_core, "_renameat2", race_then_place)

    with pytest.raises(DownloadFilesError, match="appeared"):
        transfer_file(request, backend=BytesBackend(b"download"), settings=_settings())

    assert request.target.read_bytes() == b"racing"
    assert not transfer_staging_target(request).exists()


# Exchange placement detects and restores a replacement racing after preflight.
def test_existing_target_race_is_restored_before_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"initial")
    original = transfer_core._renameat2
    injected = False

    def race_then_exchange(*args, flags: int) -> None:
        nonlocal injected
        if flags == 2 and not injected:
            injected = True
            request.target.write_bytes(b"racing replacement")
        original(*args, flags=flags)

    monkeypatch.setattr(
        transfer_core,
        "_renameat2",
        race_then_exchange,
    )

    with pytest.raises(DownloadFilesError, match="changed during atomic placement"):
        transfer_file(request, backend=BytesBackend(b"download"), settings=_settings())

    assert request.target.read_bytes() == b"racing replacement"
    assert not transfer_staging_target(request).exists()


# The commit claim binds the verified inode even if its staging name is replaced.
def test_staging_leaf_replacement_before_claim_never_commits_foreign_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    original = transfer_core._link_fd_noreplace

    def replace_leaf_then_claim(*args) -> None:
        staging.unlink()
        staging.write_bytes(b"foreign")
        original(*args)

    monkeypatch.setattr(transfer_core, "_link_fd_noreplace", replace_leaf_then_claim)

    with pytest.raises(DownloadFilesError, match="artifact identity changed"):
        transfer_file(request, backend=BytesBackend(b"verified"), settings=_settings())

    assert not request.target.exists()
    assert staging.read_bytes() == b"foreign"
    assert not list(staging.parent.glob(f".{staging.name}.commit-*"))


# Cleanup first quarantines a raced leaf and restores foreign bytes by identity.
def test_owned_cleanup_race_never_unlinks_foreign_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    original = transfer_core._renameat2
    injected = False

    def replace_before_quarantine(*args, flags: int) -> None:
        nonlocal injected
        source_name = args[1]
        target_name = args[3]
        if (
            flags == 1
            and source_name == staging.name
            and ".cleanup-" in target_name
            and not injected
        ):
            injected = True
            staging.unlink()
            staging.write_bytes(b"foreign")
        original(*args, flags=flags)

    monkeypatch.setattr(transfer_core, "_renameat2", replace_before_quarantine)

    with pytest.raises(DownloadFilesError, match="artifact identity changed"):
        transfer_file(
            request,
            backend=BytesBackend(
                b"partial",
                error=TransferDownloadFilesError("network interrupted"),
            ),
            settings=_settings(),
        )

    assert staging.read_bytes() == b"foreign"
    assert not list(staging.parent.glob("*.cleanup-*"))
    assert not request.target.exists()


# Claim cleanup uses the same quarantine rule when the original name is raced.
def test_claim_cleanup_race_preserves_foreign_original_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    original = transfer_core._renameat2
    injected = False

    def replace_original_before_quarantine(*args, flags: int) -> None:
        nonlocal injected
        source_name = args[1]
        target_name = args[3]
        if (
            flags == 1
            and source_name == staging.name
            and ".cleanup-" in target_name
            and not injected
        ):
            injected = True
            staging.unlink()
            staging.write_bytes(b"foreign")
        original(*args, flags=flags)

    monkeypatch.setattr(
        transfer_core,
        "_renameat2",
        replace_original_before_quarantine,
    )

    with pytest.raises(DownloadFilesError, match="artifact identity changed"):
        transfer_file(request, backend=BytesBackend(b"verified"), settings=_settings())

    assert staging.read_bytes() == b"foreign"
    assert not list(staging.parent.glob(f".{staging.name}.commit-*"))
    assert not list(staging.parent.glob("*.cleanup-*"))
    assert not request.target.exists()


# An ambiguous exchange preserves the displaced old final under the commit claim.
def test_uncertain_replacement_never_cleans_displaced_old_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    staging = transfer_staging_target(request)
    original = transfer_core._renameat2
    injected = False

    def exchange_then_report_error(*args, flags: int) -> None:
        nonlocal injected
        original(*args, flags=flags)
        if flags == 2 and not injected:
            injected = True
            raise OSError("ambiguous exchange result")

    monkeypatch.setattr(
        transfer_core,
        "_renameat2",
        exchange_then_report_error,
    )

    with pytest.raises(DownloadFilesError, match="replacement is uncertain"):
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    assert request.target.read_bytes() == b"new"
    claims = list(staging.parent.glob(f".{staging.name}.commit-*"))
    assert len(claims) == 1
    assert claims[0].read_bytes() == b"old"


# Displaced-old cleanup cannot delete a replacement raced under its claim name.
def test_displaced_old_cleanup_race_preserves_foreign_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    staging = transfer_staging_target(request)
    original = transfer_core._renameat2
    injected = False

    def replace_displaced_before_quarantine(*args, flags: int) -> None:
        nonlocal injected
        source_fd, source_name = args[:2]
        target_name = args[3]
        if (
            flags == 1
            and ".commit-" in source_name
            and ".cleanup-" in target_name
            and not injected
        ):
            injected = True
            os.unlink(source_name, dir_fd=source_fd)
            fd = os.open(
                source_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=source_fd,
            )
            try:
                os.write(fd, b"foreign")
            finally:
                os.close(fd)
        original(*args, flags=flags)

    monkeypatch.setattr(
        transfer_core,
        "_renameat2",
        replace_displaced_before_quarantine,
    )

    with pytest.raises(DownloadFilesError, match="artifact identity changed"):
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    assert request.target.read_bytes() == b"new"
    claims = list(staging.parent.glob(f".{staging.name}.commit-*"))
    assert len(claims) == 1
    assert claims[0].read_bytes() == b"foreign"
    assert not list(staging.parent.glob("*.cleanup-*"))


# A failed directory durability barrier rolls placement back before reporting failure.
@pytest.mark.parametrize("existing", [None, b"old"])
def test_durability_failure_restores_precommit_target_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bytes | None,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    if existing is not None:
        request.target.write_bytes(existing)
    original_fsync = transfer_core.os.fsync
    staged_bytes_synced = False
    failed = False

    def fail_first_directory_barrier(fd: int) -> None:
        nonlocal staged_bytes_synced, failed
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            staged_bytes_synced = True
        elif staged_bytes_synced and not failed:
            failed = True
            raise OSError("durability unavailable")
        original_fsync(fd)

    monkeypatch.setattr(transfer_core.os, "fsync", fail_first_directory_barrier)

    with pytest.raises(DownloadFilesError, match="durably placed"):
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    if existing is None:
        assert not request.target.exists()
    else:
        assert request.target.read_bytes() == existing
    assert not transfer_staging_target(request).exists()


# Rollback is not reported safe unless both exchanged directories are durable.
def test_exchange_rollback_barrier_failure_preserves_both_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    staging = transfer_staging_target(request)
    original_rename = transfer_core._renameat2
    original_fsync = transfer_core.os.fsync
    exchange_calls = 0

    def race_then_exchange(*args, flags: int) -> None:
        nonlocal exchange_calls
        if flags == 2:
            exchange_calls += 1
            if exchange_calls == 1:
                request.target.write_bytes(b"racing old")
        original_rename(*args, flags=flags)

    def fail_rollback_barrier(fd: int) -> None:
        if exchange_calls == 2:
            raise OSError("rollback barrier unavailable")
        original_fsync(fd)

    monkeypatch.setattr(transfer_core, "_renameat2", race_then_exchange)
    monkeypatch.setattr(transfer_core.os, "fsync", fail_rollback_barrier)

    with pytest.raises(DownloadFilesError, match="rollback durability is uncertain"):
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    assert request.target.read_bytes() == b"racing old"
    claims = list(staging.parent.glob(f".{staging.name}.commit-*"))
    assert len(claims) == 1
    assert claims[0].read_bytes() == b"new"


# New nested directory entries are persisted deepest-first before transport starts.
def test_created_target_directory_chain_is_durable_before_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = replace(_request(root), target=root / "models" / "nested" / "model.bin")
    original_fsync = transfer_core.os.fsync
    durable_directories: list[Path] = []

    def record_directory_barrier(fd: int) -> None:
        metadata = os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode):
            durable_directories.append(
                Path(os.readlink(f"/proc/{os.getpid()}/fd/{fd}"))
            )
        original_fsync(fd)

    class ObserveDurabilityBackend(BytesBackend):
        def download(self, request, settings) -> TransportSuccess:
            assert [path.name for path in durable_directories] == [
                "nested",
                "models",
                "ComfyUI",
            ]
            return super().download(request, settings)

    monkeypatch.setattr(transfer_core.os, "fsync", record_directory_barrier)

    outcome = transfer_file(
        request,
        backend=ObserveDurabilityBackend(b"new"),
        settings=_settings(),
    )

    assert outcome.status is DownloadStatus.DOWNLOADED
    assert request.target.read_bytes() == b"new"


# A failed ancestor barrier stops before any transport-owned bytes are created.
def test_created_target_directory_durability_failure_precedes_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = replace(_request(root), target=root / "models" / "nested" / "model.bin")
    backend = BytesBackend(b"new")

    def fail_directory_barrier(fd: int) -> None:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        raise OSError("directory barrier unavailable")

    monkeypatch.setattr(transfer_core.os, "fsync", fail_directory_barrier)

    with pytest.raises(DownloadFilesError, match="directory could not be made durable"):
        transfer_file(request, backend=backend, settings=_settings())

    assert backend.calls == []
    assert not transfer_staging_target(request).exists()
    assert not request.target.exists()


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
