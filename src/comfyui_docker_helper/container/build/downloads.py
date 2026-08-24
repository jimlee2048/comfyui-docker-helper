"""Container build-file download planning and orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from comfyui_docker_helper.cli_output.events import EventSink
from comfyui_docker_helper.config.planning.build_plan import FilesPhase, HttpFilePlan
from comfyui_docker_helper.config.validation.urls import (
    DownloaderName,
    require_downloader_name,
)
from comfyui_docker_helper.container.transfer.aria2 import (
    Aria2Downloader,
    Aria2DownloaderFactory,
)
from comfyui_docker_helper.container.transfer.coordinator import (
    AttemptCancelled,
    AttemptExhausted,
    AttemptLocalFailure,
    AttemptOrdinaryTerminal,
    AttemptSucceeded,
    coordinate_transfer_attempts,
)
from comfyui_docker_helper.container.transfer.core import (
    Aria2DownloadSettings,
    DownloadBackend,
    DownloadCancelled,
    DownloaderSettings,
    DownloadFilesError,
    DownloadStatus,
    FileTransferOutcome,
    FileTransferRequest,
    HttpxDownloadSettings,
    StagingDisposition,
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
    VerificationStatus,
    verify_required_final,
)
from comfyui_docker_helper.container.transfer.credentials import (
    MountedDownloaderCredentialPolicy,
)
from comfyui_docker_helper.container.transfer.events import (
    DownloadBackendName,
    DownloadBatchCompleted,
    DownloadEvent,
    DownloadFinalVerificationCompleted,
    DownloadFinalVerificationStarted,
    DownloadItemCompleted,
    DownloadItemStarted,
    DownloadItemStatus,
    DownloadRetryReason,
)
from comfyui_docker_helper.container.transfer.httpx import HttpxDownloader


@dataclass(frozen=True, slots=True)
class FileDownloadItem:
    """One resolved build-time file request."""

    url: str
    filename: str
    target: Path
    overwrite: bool
    downloader: DownloaderName
    checksum: str | None = None


@dataclass(frozen=True, slots=True)
class FileDownloadPlan:
    """Ordered required build files and transport settings."""

    comfyui_root: Path
    downloader: DownloaderSettings
    items: tuple[FileDownloadItem, ...]
    download_max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """One required build-file outcome with expected/observed evidence."""

    item: FileDownloadItem
    outcome: FileTransferOutcome

    @property
    def status(self) -> DownloadStatus:
        return self.outcome.status


def file_download_plan(
    payload: FilesPhase,
    comfyui_root: str | Path,
) -> FileDownloadPlan:
    """Project admitted BuildPlan file inputs without runtime failure policy."""
    plan = FileDownloadPlan(
        comfyui_root=Path(comfyui_root),
        downloader=DownloaderSettings(
            default=require_downloader_name(payload.downloader.default),
            aria2=Aria2DownloadSettings(
                rpc_port=payload.downloader.aria2.rpc_port,
                split=payload.downloader.aria2.split,
                max_connection_per_server=(
                    payload.downloader.aria2.max_connection_per_server
                ),
                min_split_size=payload.downloader.aria2.min_split_size,
                resume_download=payload.downloader.aria2.resume_download,
            ),
            httpx=HttpxDownloadSettings(
                timeout=payload.downloader.httpx.timeout,
            ),
        ),
        items=tuple(
            FileDownloadItem(
                url=item.url,
                filename=Path(item.target).name,
                target=Path(item.target),
                overwrite=True,
                downloader=require_downloader_name(item.downloader),
                checksum=item.checksum,
            )
            for item in payload.files
            if isinstance(item, HttpFilePlan)
        ),
        download_max_attempts=payload.download_max_attempts,
    )
    _validate_download_plan(plan)
    return plan


def process_file_downloads(
    plan: FileDownloadPlan,
    *,
    backends: Mapping[str, DownloadBackend],
    event_sink: EventSink[DownloadEvent] | None = None,
) -> tuple[DownloadResult, ...]:
    """Process required build files serially; every failure remains fatal."""
    _validate_download_plan(plan)
    results: list[DownloadResult] = []
    for index, item in enumerate(plan.items, 1):
        target = _download_target(plan, item)
        if event_sink is not None:
            event_sink.emit(
                DownloadItemStarted(
                    index=index,
                    total=len(plan.items),
                    target=target,
                    backend=DownloadBackendName(item.downloader),
                    max_attempts=plan.download_max_attempts,
                    checksum_expected=item.checksum is not None,
                )
            )
        try:
            backend = backends[item.downloader]
        except KeyError as error:
            raise DownloadFilesError(
                f"download backend is not configured for {target}"
            ) from error
        outcome = _download_with_policy(
            item,
            backend,
            plan,
            event_sink=event_sink,
        )
        _verify_build_file_postcondition(plan, item)
        if event_sink is not None:
            status = (
                DownloadItemStatus.DOWNLOADED
                if outcome.status is DownloadStatus.DOWNLOADED
                else DownloadItemStatus.SKIPPED
            )
            event_sink.emit(
                DownloadItemCompleted(
                    status=status,
                    observed_bytes=outcome.observed_length,
                    checksum_verified=(
                        outcome.verification is VerificationStatus.VERIFIED
                    ),
                )
            )
        results.append(DownloadResult(item=item, outcome=outcome))

    checksum_count = sum(item.checksum is not None for item in plan.items)
    if event_sink is not None:
        event_sink.emit(
            DownloadFinalVerificationStarted(
                item_count=len(plan.items),
                checksum_count=checksum_count,
            )
        )
    for item in plan.items:
        _verify_build_file_postcondition(plan, item)
    if event_sink is not None:
        event_sink.emit(DownloadFinalVerificationCompleted())
        event_sink.emit(
            DownloadBatchCompleted(
                item_count=len(plan.items),
                checksum_verified_count=checksum_count,
            )
        )
    return tuple(results)


def _verify_build_file_postcondition(
    plan: FileDownloadPlan,
    item: FileDownloadItem,
) -> None:
    target = _download_target(plan, item)
    try:
        verify_required_final(
            root=plan.comfyui_root,
            target=item.target,
            expected_checksum=item.checksum,
        )
    except DownloadFilesError as error:
        raise DownloadFilesError(
            f"required download file verification failed for {target}"
        ) from error


def download_files(
    files: FilesPhase,
    comfyui_root: str | Path,
    *,
    httpx_downloader: DownloadBackend | None = None,
    aria2_downloader_factory: Aria2DownloaderFactory = Aria2Downloader,
    event_sink: EventSink[DownloadEvent],
) -> tuple[DownloadResult, ...]:
    """Download required build files from one admitted BuildPlan phase."""
    plan = file_download_plan(files, comfyui_root)
    httpx_backend = httpx_downloader or HttpxDownloader(
        credential_policy=MountedDownloaderCredentialPolicy.from_routes(
            files.credentials
        ),
    )
    backends: dict[str, DownloadBackend] = {"httpx": httpx_backend}
    if not any(item.downloader == "aria2" for item in plan.items):
        return process_file_downloads(
            plan,
            backends=backends,
            event_sink=event_sink,
        )
    with aria2_downloader_factory() as aria2_backend:
        backends["aria2"] = aria2_backend
        return process_file_downloads(
            plan,
            backends=backends,
            event_sink=event_sink,
        )


def _download_with_policy(
    item: FileDownloadItem,
    backend: DownloadBackend,
    plan: FileDownloadPlan,
    *,
    event_sink: EventSink[DownloadEvent] | None = None,
) -> FileTransferOutcome:
    settings = plan.downloader
    target = _download_target(plan, item)
    request = FileTransferRequest(
        root=plan.comfyui_root,
        url=item.url,
        target=item.target,
        overwrite=item.overwrite,
        expected_checksum=item.checksum,
        staging_disposition=StagingDisposition.CLEAN,
    )
    result = coordinate_transfer_attempts(
        request,
        backend_name=item.downloader,
        backend=backend,
        settings=settings,
        max_attempts=plan.download_max_attempts,
        event_sink=event_sink,
    )
    if isinstance(result, AttemptSucceeded):
        return result.outcome
    if isinstance(result, AttemptOrdinaryTerminal):
        status = _http_status_detail(result.error.http_status)
        raise TerminalTransferDownloadFilesError(
            f"download failed for {target}{status}",
            http_status=result.error.http_status,
        ) from result.error
    if isinstance(result, AttemptExhausted):
        status = _http_status_detail(result.error.http_status)
        reason = _download_retry_reason_detail(result.error.reason)
        attempt_noun = "attempt" if result.attempts == 1 else "attempts"
        raise TransferDownloadFilesError(
            f"download failed for {target} after {result.attempts} {attempt_noun}: "
            f"{reason}{status}",
            retry_after_seconds=result.error.retry_after_seconds,
            resume_authority=result.error.resume_authority,
            reason=result.error.reason,
            http_status=result.error.http_status,
        ) from result.error
    if isinstance(result, AttemptLocalFailure):
        raise DownloadFilesError(
            f"download credentials could not be used for {target}"
        ) from result.error
    if isinstance(result, AttemptCancelled):
        raise DownloadCancelled(
            f"download cancelled for {target}",
            resume_authority=result.resume_authority,
        )
    raise AssertionError("attempt coordinator returned an unknown result")


def _download_retry_reason_detail(reason: DownloadRetryReason) -> str:
    return {
        DownloadRetryReason.TIMEOUT: "transfer timed out",
        DownloadRetryReason.NETWORK: "network transfer failed",
        DownloadRetryReason.TEMPORARY_SERVER: "temporary remote service failure",
        DownloadRetryReason.RATE_LIMITED: "remote service rate limited the request",
        DownloadRetryReason.RESUME_REJECTED: "remote service rejected transfer resume",
        DownloadRetryReason.CHECKSUM_MISMATCH: "checksum verification failed",
        DownloadRetryReason.UNKNOWN: "temporary transfer failure",
    }[reason]


def _http_status_detail(status: int | None) -> str:
    return "" if status is None else f" (HTTP {status})"


def _download_target(plan: FileDownloadPlan, item: FileDownloadItem) -> str:
    return item.target.relative_to(plan.comfyui_root).as_posix()


def _validate_download_plan(plan: FileDownloadPlan) -> None:
    if not plan.comfyui_root.is_absolute():
        raise DownloadFilesError("download root must be an absolute path")
    for item in plan.items:
        if not item.target.is_absolute():
            raise DownloadFilesError("download target must be an absolute path")
        try:
            relative = item.target.relative_to(plan.comfyui_root)
        except ValueError as error:
            raise DownloadFilesError("download target escapes COMFYUI_PATH") from error
        if not relative.parts or ".." in relative.parts:
            raise DownloadFilesError(
                "download target must be a strict descendant of COMFYUI_PATH"
            )
