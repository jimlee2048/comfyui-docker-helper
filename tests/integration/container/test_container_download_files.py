"""Required build-file orchestration through the shared transfer core."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_docker_helper.config.build_plan import (
    Aria2Plan,
    DownloaderPlan,
    FilesPhase,
    HttpFilePlan,
    HttpxPlan,
)
from comfyui_docker_helper.container import attempt_coordinator
from comfyui_docker_helper.container.download_files import (
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    DownloadStatus,
    FileDownloadItem,
    FileDownloadPlan,
    HttpxDownloadSettings,
    TransportCancelled,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportOutcome,
    TransportRequest,
    TransportRetryable,
    TransportSuccess,
    file_download_plan,
    process_file_downloads,
)
from comfyui_docker_helper.container.transfer_core import (
    DownloadCancelled,
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
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
        httpx=HttpxDownloadSettings(timeout=90.5),
    )


def _item(root: Path, name: str, *, downloader: str = "httpx") -> FileDownloadItem:
    return FileDownloadItem(
        url=f"https://example.test/{name}",
        filename=name,
        target=root / "models" / name,
        overwrite=False,
        downloader=downloader,
    )


def _disable_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep orchestration tests focused on policy rather than elapsed time."""
    monkeypatch.setattr(
        attempt_coordinator,
        "_default_cancellable_wait",
        lambda _timeout, _cancel_requested: False,
    )


def _owned_staging_leaves(root: Path) -> tuple[Path, ...]:
    staging = root / "models" / ".cdh-staging"
    return tuple(staging.iterdir()) if staging.exists() else ()


# Build-file planning preserves declared checksum, attempt budget, order, and
# target containment before transport begins.
def test_file_download_plan_projects_checksum_and_attempt_budget() -> None:
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
            httpx=HttpxPlan(timeout=42),
        ),
        credentials=(),
        default_download_mode="sync",
        download_max_attempts=3,
        files=(
            HttpFilePlan(
                type="http",
                url="https://example.test/model.bin",
                target="/workspace/ComfyUI/models/model.bin",
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
    assert plan.items[0].overwrite is True
    assert plan.download_max_attempts == 3


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
            httpx=HttpxPlan(timeout=42),
        ),
        credentials=(),
        default_download_mode="sync",
        download_max_attempts=1,
        files=(
            HttpFilePlan(
                type="http",
                url="https://example.test/model.bin",
                target="/outside/model.bin",
                downloader="httpx",
                download_mode="sync",
                downloader_explicit=True,
                download_mode_explicit=True,
            ),
        ),
    )

    with pytest.raises(DownloadFilesError, match="escapes COMFYUI_PATH"):
        file_download_plan(payload, "/workspace/ComfyUI")


# The build orchestrator delegates each item in declaration order with a fresh
# per-file attempt budget.
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


def test_build_grants_each_declared_file_a_fresh_attempt_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry used by one file must not consume the next file's budget."""
    _disable_retry_delay(monkeypatch)
    root = tmp_path / "ComfyUI"
    root.mkdir()
    first = _item(root, "first.bin")
    second = _item(root, "second.bin")

    class RetryEachFileOnce(RecordingBackend):
        def __init__(self) -> None:
            super().__init__()
            self.seen: set[str] = set()

        def download(self, request, settings):
            self.calls.append(request)
            with request.sink.open_for_write() as output:
                output.write(b"downloaded")
            if request.url not in self.seen:
                self.seen.add(request.url)
                return TransportRetryable(
                    TransportDiagnostic("httpx", "network failed")
                )
            return TransportSuccess(
                length=len(b"downloaded"), namespace="httpx", http_status=200
            )

    backend = RetryEachFileOnce()

    results = process_file_downloads(
        FileDownloadPlan(root, _settings(), (first, second), 2),
        backends={"httpx": backend},
        log=lambda _: None,
    )

    assert [call.url for call in backend.calls] == [
        first.url,
        first.url,
        second.url,
        second.url,
    ]
    assert [result.item for result in results] == [first, second]
    assert all(result.status is DownloadStatus.DOWNLOADED for result in results)


# Required build downloads expose stable success evidence and stop immediately
# on exhausted or terminal outcomes.
def test_build_exhaustion_is_always_fatal_and_preserves_later_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhaustion preserves an old final and stops later declarations."""
    _disable_retry_delay(monkeypatch)
    root = tmp_path / "ComfyUI"
    root.mkdir()
    first = replace(_item(root, "first.bin"), overwrite=True)
    second = _item(root, "second.bin")
    first.target.parent.mkdir()
    first.target.write_bytes(b"old")
    backend = RecordingBackend(fail_times=3)

    with pytest.raises(TransferDownloadFilesError, match="network failed"):
        process_file_downloads(
            FileDownloadPlan(root, _settings(), (first, second), 2),
            backends={"httpx": backend},
            log=lambda _: None,
        )

    assert len(backend.calls) == 2
    assert first.target.read_bytes() == b"old"
    assert not second.target.exists()
    assert _owned_staging_leaves(root) == ()


def test_successful_replacement_returns_stable_observation_and_cleans_staging(
    tmp_path: Path,
) -> None:
    """Build success exposes the shared core's final result without artifacts."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    item = replace(_item(root, "model.bin"), overwrite=True)
    item.target.parent.mkdir()
    item.target.write_bytes(b"old")

    (result,) = process_file_downloads(
        FileDownloadPlan(root, _settings(), (item,), 1),
        backends={"httpx": RecordingBackend()},
        log=lambda _: None,
    )

    assert result.item == item
    assert result.outcome.target == item.target
    assert result.outcome.observed_length == len(b"downloaded")
    assert item.target.read_bytes() == b"downloaded"
    assert _owned_staging_leaves(root) == ()


@pytest.mark.parametrize(
    ("terminal_outcome", "expected_error"),
    [
        (
            TransportOrdinaryTerminal(
                TransportDiagnostic("httpx", "not found"),
                http_status=404,
            ),
            TerminalTransferDownloadFilesError,
        ),
        (
            TransportCancelled(TransportDiagnostic("httpx", "cancelled")),
            DownloadCancelled,
        ),
    ],
)
def test_build_terminal_outcomes_are_fatal_and_stop_declaration_order(
    tmp_path: Path,
    terminal_outcome: TransportOutcome,
    expected_error: type[Exception],
) -> None:
    """Ordinary terminal and cancellation cannot become build success."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    first = _item(root, "first.bin")
    second = _item(root, "second.bin")

    class TerminalBackend(RecordingBackend):
        def download(self, request, settings):
            self.calls.append(request)
            with request.sink.open_for_write() as output:
                output.write(b"partial")
            return terminal_outcome

    backend = TerminalBackend()

    with pytest.raises(expected_error):
        process_file_downloads(
            FileDownloadPlan(root, _settings(), (first, second), 3),
            backends={"httpx": backend},
            log=lambda _: None,
        )

    assert [call.url for call in backend.calls] == [first.url]
    assert not first.target.exists()
    assert not second.target.exists()
    assert _owned_staging_leaves(root) == ()


# Preflight skips valid existing finals and rejects unavailable backends before
# mutating the destination.
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
