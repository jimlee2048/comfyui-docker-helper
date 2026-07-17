"""Required build-file orchestration through the shared transfer core."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from comfyui_docker_helper.config.build_plan import (
    Aria2Plan,
    DownloaderPlan,
    FilePlan,
    FilesPhase,
    HttpxPlan,
)
from comfyui_docker_helper.container.download_files import (
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    DownloadStatus,
    FileDownloadItem,
    FileDownloadPlan,
    HttpxDownloadSettings,
    TransferDownloadFilesError,
    TransportDiagnostic,
    TransportOutcome,
    TransportRequest,
    TransportRetryable,
    TransportSuccess,
    file_download_plan,
    process_file_downloads,
)


class RecordingBackend:
    """Write supplied bytes and record only the adapter-visible request."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[TransportRequest] = []

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        self.calls.append(request)
        with request.sink.open_for_write() as output:
            output.write(b"downloaded")
        if self.fail_times:
            self.fail_times -= 1
            return TransportRetryable(TransportDiagnostic("httpx", "network failed"))
        return TransportSuccess(
            length=len(b"downloaded"), namespace="httpx", http_status=200
        )


def _settings() -> DownloaderSettings:
    return DownloaderSettings(
        default="httpx",
        aria2=Aria2DownloadSettings(
            rpc_port=6811,
            split=8,
            max_connection_per_server=4,
            min_split_size="2M",
            resume_download=False,
        ),
        httpx=HttpxDownloadSettings(timeout=90.5, retries=5),
    )


def _item(root: Path, name: str, *, downloader: str = "httpx") -> FileDownloadItem:
    return FileDownloadItem(
        url=f"https://example.test/{name}",
        filename=name,
        target=root / "models" / name,
        overwrite=False,
        downloader=downloader,
    )


def test_file_download_plan_projects_checksum_without_runtime_failure_policy() -> None:
    checksum = f"sha256:{hashlib.sha256(b'downloaded').hexdigest()}"
    payload = FilesPhase(
        downloader=DownloaderPlan(
            default="httpx",
            aria2=Aria2Plan(
                rpc_port=6800,
                split=16,
                max_connection_per_server=16,
                min_split_size="1M",
                resume_download=True,
            ),
            httpx=HttpxPlan(timeout=42, retries=4),
        ),
        default_download_mode="sync",
        download_max_attempts=3,
        download_failure_policy="continue",
        files=(
            FilePlan(
                url="https://example.test/model.bin",
                target="/workspace/ComfyUI/models/model.bin",
                overwrite=True,
                checksum=checksum,
                downloader="httpx",
                download_mode="sync",
                downloader_explicit=True,
                download_mode_explicit=True,
            ),
        ),
    )

    plan = file_download_plan(payload, "/workspace/ComfyUI")

    assert plan.items[0].checksum == checksum
    assert plan.download_max_attempts == 3
    assert not hasattr(plan, "download_failure_policy")


def test_file_download_plan_rejects_target_outside_admitted_root() -> None:
    payload = FilesPhase(
        downloader=DownloaderPlan(
            default="httpx",
            aria2=Aria2Plan(
                rpc_port=6800,
                split=16,
                max_connection_per_server=16,
                min_split_size="1M",
                resume_download=True,
            ),
            httpx=HttpxPlan(timeout=42, retries=0),
        ),
        default_download_mode="sync",
        download_max_attempts=1,
        download_failure_policy="fail",
        files=(
            FilePlan(
                url="https://example.test/model.bin",
                target="/outside/model.bin",
                overwrite=False,
                downloader="httpx",
                download_mode="sync",
                downloader_explicit=True,
                download_mode_explicit=True,
            ),
        ),
    )

    with pytest.raises(DownloadFilesError, match="escapes COMFYUI_PATH"):
        file_download_plan(payload, "/workspace/ComfyUI")


def test_build_orchestrator_preserves_order_and_places_via_shared_core(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    first = _item(root, "first.bin")
    second = _item(root, "second.bin", downloader="aria2")
    plan = FileDownloadPlan(root, _settings(), (first, second), 2)
    httpx = RecordingBackend()
    aria2 = RecordingBackend()

    results = process_file_downloads(
        plan,
        backends={"httpx": httpx, "aria2": aria2},
        log=lambda _: None,
    )

    assert [result.status for result in results] == [
        DownloadStatus.DOWNLOADED,
        DownloadStatus.DOWNLOADED,
    ]
    assert first.target.read_bytes() == b"downloaded"
    assert second.target.read_bytes() == b"downloaded"
    assert httpx.calls[0].sink.display_path.parent.name == ".cdh-staging"
    assert aria2.calls[0].sink.display_path.parent.name == ".cdh-staging"
    assert httpx.calls[0].sink.display_path != first.target


def test_build_retries_retryable_transfer_then_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    item = _item(root, "model.bin")
    backend = RecordingBackend(fail_times=2)

    results = process_file_downloads(
        FileDownloadPlan(root, _settings(), (item,), 3),
        backends={"httpx": backend},
        log=lambda _: None,
    )

    assert results[0].status is DownloadStatus.DOWNLOADED
    assert len(backend.calls) == 3


def test_build_exhaustion_is_always_fatal_and_preserves_later_items(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    first = _item(root, "first.bin")
    second = _item(root, "second.bin")
    backend = RecordingBackend(fail_times=3)

    with pytest.raises(TransferDownloadFilesError, match="network failed"):
        process_file_downloads(
            FileDownloadPlan(root, _settings(), (first, second), 2),
            backends={"httpx": backend},
            log=lambda _: None,
        )

    assert len(backend.calls) == 2
    assert not first.target.exists()
    assert not second.target.exists()


def test_build_existing_regular_skip_does_not_call_backend(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    item = _item(root, "model.bin")
    item.target.parent.mkdir(parents=True)
    item.target.write_bytes(b"keep")
    backend = RecordingBackend()

    result = process_file_downloads(
        FileDownloadPlan(root, _settings(), (item,), 1),
        backends={"httpx": backend},
        log=lambda _: None,
    )[0]

    assert result.status is DownloadStatus.SKIPPED
    assert result.outcome.observed_checksum is None
    assert backend.calls == []
    assert item.target.read_bytes() == b"keep"


def test_build_missing_backend_is_terminal_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    item = _item(root, "model.bin")

    with pytest.raises(DownloadFilesError, match="not configured"):
        process_file_downloads(
            FileDownloadPlan(root, _settings(), (item,), 1),
            backends={},
        )

    assert not item.target.parent.exists()


# Batch postconditions catch an earlier required final changed by later work.
def test_build_batch_rechecks_every_required_final(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    first = _item(root, "first.bin")
    second = _item(root, "second.bin")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")

    class MutatingBackend(RecordingBackend):
        def download(self, request, settings) -> TransportSuccess:
            result = super().download(request, settings)
            if len(self.calls) == 2:
                first.target.unlink()
                first.target.symlink_to(outside)
            return result

    with pytest.raises(DownloadFilesError, match=r"required.*regular"):
        process_file_downloads(
            FileDownloadPlan(root, _settings(), (first, second), 1),
            backends={"httpx": MutatingBackend()},
            log=lambda _: None,
        )

    assert first.target.is_symlink()
    assert second.target.read_bytes() == b"downloaded"
