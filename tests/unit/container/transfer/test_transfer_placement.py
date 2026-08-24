"""Durable target-matrix and filesystem-safety tests for shared transfers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from comfyui_docker_helper.container.transfer import core as transfer_core
from comfyui_docker_helper.container.transfer.core import (
    DownloadCancelled,
    DownloadFilesError,
    DownloadStatus,
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
    TransportCancelled,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportRetryable,
    TransportSuccess,
    transfer_file,
    transfer_staging_target,
    verify_required_final,
)
from comfyui_docker_helper.container.transfer.events import (
    DownloadEvent,
    DownloadPlacementCompleted,
    DownloadPlacementStarted,
    DownloadVerificationCompleted,
    DownloadVerificationStarted,
)

from ._transfer_core_support import (
    BytesBackend,
    RecordingEventSink,
    _checksum,
    _request,
    _settings,
)

_LOCAL_COPY_TIMEOUT_SECONDS = 30


def test_success_events_follow_verified_and_durable_placement_boundaries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")

    class ControlBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            control.write_bytes(b"control")
            return TransportSuccess(length=outcome.length, namespace="aria2")

    class ProbingSink(RecordingEventSink):
        def emit(self, event: DownloadEvent, /) -> None:
            if isinstance(
                event,
                (
                    DownloadVerificationStarted,
                    DownloadVerificationCompleted,
                    DownloadPlacementStarted,
                ),
            ):
                assert staging.read_bytes() == b"new"
                assert request.target.read_bytes() == b"old"
            elif isinstance(event, DownloadPlacementCompleted):
                assert not staging.exists()
                assert not control.exists()
                assert request.target.read_bytes() == b"new"
            super().emit(event)

    events = ProbingSink()
    outcome = transfer_file(
        request,
        backend=ControlBackend(b"new"),
        settings=_settings(),
        event_sink=events,
    )

    assert outcome.status is DownloadStatus.DOWNLOADED
    assert events.events == [
        DownloadVerificationStarted(),
        DownloadVerificationCompleted(),
        DownloadPlacementStarted(),
        DownloadPlacementCompleted(),
    ]


def test_precommit_event_failure_preserves_original_and_cleans_owned_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")
    failure = KeyboardInterrupt("event-sink-sentinel")

    class ControlBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            control.write_bytes(b"control")
            return TransportSuccess(length=outcome.length, namespace="aria2")

    class FailingSink(RecordingEventSink):
        def emit(self, event: DownloadEvent, /) -> None:
            super().emit(event)
            if isinstance(event, DownloadPlacementStarted):
                raise failure

    events = FailingSink()
    with pytest.raises(KeyboardInterrupt) as raised:
        transfer_file(
            request,
            backend=ControlBackend(b"new"),
            settings=_settings(),
            event_sink=events,
        )

    assert raised.value is failure
    assert events.events[-1] == DownloadPlacementStarted()
    assert request.target.read_bytes() == b"old"
    assert not staging.exists()
    assert not control.exists()


def test_placement_completed_event_failure_keeps_committed_final(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)
    failure = OSError("event-sink-sentinel")

    class FailingSink(RecordingEventSink):
        def emit(self, event: DownloadEvent, /) -> None:
            super().emit(event)
            if isinstance(event, DownloadPlacementCompleted):
                raise failure

    events = FailingSink()
    with pytest.raises(OSError) as raised:
        transfer_file(
            request,
            backend=BytesBackend(b"new"),
            settings=_settings(),
            event_sink=events,
        )

    assert raised.value is failure
    assert events.events[-1] == DownloadPlacementCompleted()
    assert request.target.read_bytes() == b"new"
    assert not staging.exists()


def test_transport_failure_preserves_old_final_and_cleans_only_owned_staging(
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
                timeout=_LOCAL_COPY_TIMEOUT_SECONDS,
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


def test_missing_target_race_is_preserved_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    original = transfer_core._rename_noreplace
    injected = False

    def race_then_place(*args) -> None:
        nonlocal injected
        if not injected:
            injected = True
            request.target.write_bytes(b"racing")
        original(*args)

    monkeypatch.setattr(transfer_core, "_rename_noreplace", race_then_place)

    with pytest.raises(DownloadFilesError, match="appeared"):
        transfer_file(request, backend=BytesBackend(b"download"), settings=_settings())

    assert request.target.read_bytes() == b"racing"
    assert not transfer_staging_target(request).exists()


def test_existing_target_drift_before_placement_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"initial")

    class TargetDriftBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            request.target.write_bytes(b"racing replacement")
            return outcome

    with pytest.raises(DownloadFilesError, match="changed during transport"):
        transfer_file(
            request,
            backend=TargetDriftBackend(b"download"),
            settings=_settings(),
        )

    assert request.target.read_bytes() == b"racing replacement"
    assert not transfer_staging_target(request).exists()


def test_existing_target_replace_failure_preserves_precommit_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    foreign = request.target.parent / "foreign.part"
    foreign.write_bytes(b"foreign")
    staging = transfer_staging_target(request)
    events = RecordingEventSink()

    def fail_replace(*_args, **_kwargs) -> None:
        raise OSError("replacement denied")

    monkeypatch.setattr(transfer_core.os, "replace", fail_replace)

    with pytest.raises(
        DownloadFilesError, match="atomic download placement failed"
    ) as raised:
        transfer_file(
            request,
            backend=BytesBackend(b"new"),
            settings=_settings(),
            event_sink=events,
        )

    assert isinstance(raised.value.__cause__, OSError)
    assert str(raised.value.__cause__) == "replacement denied"
    assert "replacement denied" not in str(raised.value)
    assert request.target.read_bytes() == b"old"
    assert foreign.read_bytes() == b"foreign"
    assert not staging.exists()
    assert events.events == [
        DownloadVerificationStarted(),
        DownloadVerificationCompleted(),
        DownloadPlacementStarted(),
    ]


def test_staging_leaf_drift_before_placement_never_commits_foreign_inode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _request(root)
    staging = transfer_staging_target(request)

    class StagingDriftBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            replacement = staging.with_name(f"{staging.name}.foreign")
            replacement.write_bytes(b"foreign")
            os.replace(replacement, staging)
            return outcome

    with pytest.raises(DownloadFilesError, match="identity changed"):
        transfer_file(
            request,
            backend=StagingDriftBackend(b"verified"),
            settings=_settings(),
        )

    assert not request.target.exists()
    assert staging.read_bytes() == b"foreign"


def test_existing_target_reader_sees_complete_old_inode_after_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    with request.target.open("rb") as old_reader:
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())
        assert old_reader.read() == b"old"
    assert request.target.read_bytes() == b"new"


def test_required_final_read_failure_is_controlled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "models" / "model.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"content")

    def fail_hash(_fd: int) -> str:
        raise OSError("required-final-read-sentinel")

    monkeypatch.setattr(transfer_core, "_hash_fd", fail_hash)

    with pytest.raises(DownloadFilesError, match="could not be verified") as raised:
        verify_required_final(
            root=root,
            target=target,
            expected_checksum=_checksum(b"content"),
        )

    assert "required-final-read-sentinel" not in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)
    assert str(raised.value.__cause__) == "required-final-read-sentinel"


def test_postcommit_final_proof_failure_keeps_complete_new_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    staging = transfer_staging_target(request)

    def fail_final_proof(*args) -> None:
        del args
        raise DownloadFilesError("injected final proof failure")

    monkeypatch.setattr(
        transfer_core,
        "_require_final_matches_staging",
        fail_final_proof,
    )

    with pytest.raises(DownloadFilesError, match="committed") as raised:
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    assert isinstance(raised.value.__cause__, DownloadFilesError)
    assert "injected final proof failure" in str(raised.value.__cause__)
    assert request.target.read_bytes() == b"new"
    assert not staging.exists()
