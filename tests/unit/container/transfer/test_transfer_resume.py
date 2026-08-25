"""Durable target-matrix and filesystem-safety tests for shared transfers."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_docker_helper.container.transfer import core as transfer_core
from comfyui_docker_helper.container.transfer.core import (
    DownloadCancelled,
    DownloadFilesError,
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
    TransportCancelled,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportResumeRejected,
    TransportRetryable,
    TransportSuccess,
    transfer_file,
    transfer_staging_target,
)
from comfyui_docker_helper.container.transfer.events import (
    DownloadRetryReason,
    DownloadVerificationStarted,
)

from ._transfer_core_support import (
    BytesBackend,
    RecordingEventSink,
    _checksum,
    _preserved_request,
    _request,
    _settings,
)


def test_checksum_mismatch_always_discards_invalid_preserved_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _preserved_request(root, checksum=_checksum(b"expected"))
    events = RecordingEventSink()

    with pytest.raises(TransferDownloadFilesError, match="checksum") as raised:
        transfer_file(
            request,
            backend=BytesBackend(
                b"invalid",
                outcome=TransportSuccess(length=7, namespace="aria2"),
            ),
            settings=_settings(),
            event_sink=events,
        )

    assert raised.value.reason is DownloadRetryReason.CHECKSUM_MISMATCH
    assert events.events == [DownloadVerificationStarted()]
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

    with pytest.raises(DownloadFilesError, match="transfer was rejected"):
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
    """A proven temp leaf is cleaned, but never accepted as resume state."""
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
