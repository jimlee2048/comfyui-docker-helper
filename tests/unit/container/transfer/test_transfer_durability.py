"""Durable target-matrix and filesystem-safety tests for shared transfers."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_docker_helper.container.transfer import core as transfer_core
from comfyui_docker_helper.container.transfer.core import (
    DownloadFilesError,
    DownloadStatus,
    TransportSuccess,
    transfer_file,
    transfer_staging_target,
)
from comfyui_docker_helper.container.transfer.events import (
    DownloadPlacementStarted,
    DownloadVerificationCompleted,
    DownloadVerificationStarted,
)

from ._transfer_core_support import (
    BytesBackend,
    RecordingEventSink,
    _request,
    _settings,
)


def test_staging_file_durability_failure_preserves_old_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    original_fsync = transfer_core.os.fsync
    failed = False

    def fail_staging_file_barrier(fd: int) -> None:
        nonlocal failed
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode) and not failed:
            failed = True
            raise OSError("staging file durability unavailable")
        original_fsync(fd)

    monkeypatch.setattr(transfer_core.os, "fsync", fail_staging_file_barrier)

    with pytest.raises(DownloadFilesError, match="staging verification") as raised:
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    assert "staging file durability unavailable" not in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)
    assert str(raised.value.__cause__) == "staging file durability unavailable"
    assert request.target.read_bytes() == b"old"
    assert not transfer_staging_target(request).exists()


@pytest.mark.parametrize("barrier", ["staging", "target"])
def test_postcommit_directory_durability_failure_keeps_complete_new_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    barrier: str,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    staging = transfer_staging_target(request)
    original_fsync = transfer_core.os.fsync
    staged_bytes_synced = False
    failed = False
    selected = staging.parent if barrier == "staging" else request.target.parent

    def fail_selected_directory_barrier(fd: int) -> None:
        nonlocal staged_bytes_synced, failed
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            staged_bytes_synced = True
        elif (
            staged_bytes_synced
            and not failed
            and Path(os.readlink(f"/proc/self/fd/{fd}")) == selected
        ):
            failed = True
            raise OSError(f"{barrier} directory durability unavailable")
        original_fsync(fd)

    monkeypatch.setattr(
        transfer_core.os,
        "fsync",
        fail_selected_directory_barrier,
    )

    with pytest.raises(DownloadFilesError, match="committed") as raised:
        transfer_file(request, backend=BytesBackend(b"new"), settings=_settings())

    assert isinstance(raised.value.__cause__, OSError)
    assert request.target.read_bytes() == b"new"
    assert not staging.exists()


def test_postcommit_control_identity_drift_preserves_foreign_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")
    original_unlink = transfer_core._unlink_owned_leaf

    class ControlBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            control.write_bytes(b"admitted control")
            return TransportSuccess(length=outcome.length, namespace="aria2")

    def drift_control_then_unlink(leaf) -> None:
        if leaf.display_path == control:
            replacement = control.with_name(f"{control.name}.foreign")
            replacement.write_bytes(b"foreign control")
            os.replace(replacement, control)
        original_unlink(leaf)

    monkeypatch.setattr(
        transfer_core,
        "_unlink_owned_leaf",
        drift_control_then_unlink,
    )

    with pytest.raises(DownloadFilesError, match="identity changed"):
        transfer_file(request, backend=ControlBackend(b"new"), settings=_settings())

    assert request.target.read_bytes() == b"new"
    assert not staging.exists()
    assert control.read_bytes() == b"foreign control"


def test_postcommit_control_cleanup_durability_failure_keeps_complete_new_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    request = _request(root, overwrite=True)
    request.target.parent.mkdir(parents=True)
    request.target.write_bytes(b"old")
    staging = transfer_staging_target(request)
    control = Path(f"{staging}.aria2")
    events = RecordingEventSink()
    original_fsync = transfer_core.os.fsync
    failed = False

    class ControlBackend(BytesBackend):
        def download(self, transport, settings):
            outcome = super().download(transport, settings)
            control.write_bytes(b"control")
            return TransportSuccess(length=outcome.length, namespace="aria2")

    def fail_control_cleanup_barrier(fd: int) -> None:
        nonlocal failed
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if (
            not failed
            and path == staging.parent
            and request.target.exists()
            and not control.exists()
        ):
            failed = True
            raise OSError("control cleanup durability unavailable")
        original_fsync(fd)

    monkeypatch.setattr(
        transfer_core.os,
        "fsync",
        fail_control_cleanup_barrier,
    )

    with pytest.raises(DownloadFilesError, match="control cleanup"):
        transfer_file(
            request,
            backend=ControlBackend(b"new"),
            settings=_settings(),
            event_sink=events,
        )

    assert request.target.read_bytes() == b"new"
    assert not staging.exists()
    assert not control.exists()
    assert events.events == [
        DownloadVerificationStarted(),
        DownloadVerificationCompleted(),
        DownloadPlacementStarted(),
    ]


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
