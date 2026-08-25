"""Runtime file download policy around the shared transfer core."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import httpx

from comfyui_docker_helper.cli_output import EventSink
from comfyui_docker_helper.config import Diagnostic
from comfyui_docker_helper.config.runtime.models import RuntimeConfig
from comfyui_docker_helper.config.validation.urls import DownloaderName
from comfyui_docker_helper.container.runtime.event_delivery import (
    RuntimeBackgroundEventSink,
)
from comfyui_docker_helper.container.runtime.events import (
    RuntimeDownloadAttemptStarted,
    RuntimeDownloadFailed,
    RuntimeDownloadItemCompleted,
    RuntimeDownloadItemProgress,
    RuntimeDownloadItemRetryScheduled,
    RuntimeDownloadItemVerificationStarted,
)
from comfyui_docker_helper.container.runtime.files.models import (
    RuntimeDownloadBackendObserver,
    RuntimeDownloadCancelRequested,
    RuntimeDownloadObservedStatus,
    RuntimeDownloadStartupObserver,
    RuntimeDownloadStateObserver,
    RuntimeFileDownloadCancelled,
    RuntimeFileDownloadError,
    RuntimeFileDownloadResult,
    RuntimeFilePath,
    RuntimeFilePlan,
    RuntimeFilePlanItem,
)
from comfyui_docker_helper.container.runtime.files.planning import (
    _runtime_item_root,
    runtime_file_staging_target,
)
from comfyui_docker_helper.container.runtime.state import RuntimeStateError
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
    CancellableDownloadBackend,
    DownloadBackend,
    DownloaderSettings,
    FileTransferOutcome,
    FileTransferRequest,
    HttpxDownloadSettings,
    PreservedTransferCleanupError,
    ResumeAuthority,
    StagingDisposition,
    TransferDownloadFilesError,
    TransportRequest,
)
from comfyui_docker_helper.container.transfer.credentials import (
    DownloaderCredentialPolicy,
)
from comfyui_docker_helper.container.transfer.events import (
    DownloadAttemptStarted,
    DownloadBackendName,
    DownloadEvent,
    DownloadRetryReason,
    DownloadRetryScheduled,
    DownloadTransferProgress,
    DownloadVerificationStarted,
)
from comfyui_docker_helper.container.transfer.httpx import HttpxDownloader
from comfyui_docker_helper.container.transfer.models import DownloadBackendPreparer


def process_runtime_file_downloads(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    backends: Mapping[str, DownloadBackend],
    state_observer: RuntimeDownloadStateObserver | None = None,
    cancel_requested: RuntimeDownloadCancelRequested | None = None,
    backend_observer: RuntimeDownloadBackendObserver | None = None,
    credential_policy: DownloaderCredentialPolicy | None = None,
    event_sink: RuntimeBackgroundEventSink,
) -> tuple[RuntimeFileDownloadResult, ...]:
    """Run runtime policy around the shared transfer core."""
    settings = runtime_downloader_settings(config)
    is_cancelled = cancel_requested or _runtime_download_not_cancelled
    results: list[RuntimeFileDownloadResult] = []

    for index, item in enumerate(plan.items, 1):
        if is_cancelled():
            break
        backend_name = _effective_downloader(item, config)
        staging_target = runtime_file_staging_target(item)
        try:
            backend = backends[backend_name]
        except KeyError as error:
            raise RuntimeFileDownloadError(
                (
                    Diagnostic(
                        path=("files", index - 1, "downloader"),
                        code="runtime_file.downloader_unavailable",
                        message=f"download backend is not configured: {backend_name}",
                    ),
                )
            ) from error

        try:
            _observe_cancellable_runtime_backend(backend, backend_observer)
            attempts_used, outcome = _download_runtime_file_with_policy(
                item,
                backend_name,
                backend,
                settings,
                ("files", index - 1),
                config=config,
                state_observer=state_observer,
                cancel_requested=is_cancelled,
                credential_policy=credential_policy,
                event_sink=event_sink,
                index=index,
                total=len(plan.items),
            )
            _notify_runtime_download_state(state_observer, item, "completed")
            event_sink.emit(
                RuntimeDownloadItemCompleted(
                    index,
                    len(plan.items),
                    item.relative_target,
                    item.download_mode,
                    attempts_used,
                    config.cdh.download_max_attempts,
                )
            )
        except _RuntimeDownloadContinued:
            continue
        except RuntimeFileDownloadCancelled:
            break
        except _RuntimeDownloadPolicyFailure:
            raise
        except RuntimeStateError:
            raise
        except PreservedTransferCleanupError:
            # No backend call or state transition occurred, so keep the persisted
            # exact authority available for a later safe cleanup attempt.
            raise
        except Exception as error:
            _notify_runtime_download_state(
                state_observer,
                item,
                "failed",
                error=error,
            )
            raise

        results.append(
            RuntimeFileDownloadResult(
                item=item,
                backend=backend_name,
                staging_target=staging_target,
                status=outcome.status,
                outcome=outcome,
            )
        )

    return tuple(results)


def download_runtime_files(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    httpx_downloader: DownloadBackend | None = None,
    aria2_downloader_factory: Aria2DownloaderFactory = Aria2Downloader,
    state_observer: RuntimeDownloadStateObserver | None = None,
    startup_observer: RuntimeDownloadStartupObserver | None = None,
    cancel_requested: RuntimeDownloadCancelRequested | None = None,
    backend_observer: RuntimeDownloadBackendObserver | None = None,
    credential_policy: DownloaderCredentialPolicy | None = None,
    event_sink: RuntimeBackgroundEventSink,
) -> tuple[RuntimeFileDownloadResult, ...]:
    """Download runtime file plan items through existing backend adapters."""
    is_cancelled = cancel_requested or _runtime_download_not_cancelled
    observed_backend_ids: set[int] = set()
    observe_backend = _runtime_backend_observer_once(
        backend_observer,
        observed_backend_ids,
    )
    if httpx_downloader is not None:
        httpx_backend = httpx_downloader
    elif credential_policy is None:
        httpx_backend = HttpxDownloader()
    else:
        httpx_backend = HttpxDownloader(credential_policy=credential_policy)
    backends: dict[str, DownloadBackend] = {"httpx": httpx_backend}

    if not _requires_aria2_backend(plan, config):
        _observe_cancellable_runtime_backend(httpx_backend, observe_backend)
        if startup_observer is not None:
            _prepare_runtime_download_backends(plan, config=config, backends=backends)
        _notify_runtime_download_startup(startup_observer)
        return process_runtime_file_downloads(
            plan,
            config=config,
            backends=backends,
            state_observer=state_observer,
            cancel_requested=is_cancelled,
            backend_observer=observe_backend,
            credential_policy=credential_policy,
            event_sink=event_sink,
        )

    with aria2_downloader_factory() as aria2_backend:
        backends["aria2"] = aria2_backend
        _observe_cancellable_runtime_backend(httpx_backend, observe_backend)
        _observe_cancellable_runtime_backend(aria2_backend, observe_backend)
        if startup_observer is not None:
            _prepare_runtime_download_backends(plan, config=config, backends=backends)
        _notify_runtime_download_startup(startup_observer)
        return process_runtime_file_downloads(
            plan,
            config=config,
            backends=backends,
            state_observer=state_observer,
            cancel_requested=is_cancelled,
            backend_observer=observe_backend,
            credential_policy=credential_policy,
            event_sink=event_sink,
        )


def runtime_downloader_settings(config: RuntimeConfig) -> DownloaderSettings:
    """Build backend settings from effective runtime config."""
    downloader = config.cdh.downloader
    return DownloaderSettings(
        default=config.cdh.default_downloader,
        aria2=Aria2DownloadSettings(
            rpc_port=downloader.aria2.rpc_port,
            split=downloader.aria2.split,
            max_connection_per_server=downloader.aria2.max_connection_per_server,
            min_split_size=downloader.aria2.min_split_size,
            resume_download=downloader.aria2.resume_download,
        ),
        httpx=HttpxDownloadSettings(
            timeout=downloader.httpx.timeout,
        ),
    )


class _RuntimeDownloadContinued(Exception):
    """Internal marker for policy-eligible failures handled by continue."""


class _RuntimeDownloadPolicyFailure(RuntimeFileDownloadError):
    """Policy-handled terminal or exhausted failure with its truthful state."""

    def __init__(
        self,
        diagnostics: tuple[Diagnostic, ...],
        *,
        status: Literal["failed", "exhausted"],
    ) -> None:
        self.status = status
        super().__init__(diagnostics)


def _download_runtime_file_with_policy(
    item: RuntimeFilePlanItem,
    backend_name: DownloaderName,
    backend: DownloadBackend,
    settings: DownloaderSettings,
    path: RuntimeFilePath,
    *,
    config: RuntimeConfig,
    state_observer: RuntimeDownloadStateObserver | None,
    cancel_requested: RuntimeDownloadCancelRequested,
    credential_policy: DownloaderCredentialPolicy | None,
    event_sink: RuntimeBackgroundEventSink,
    index: int,
    total: int,
) -> tuple[int, FileTransferOutcome]:
    attempts = config.cdh.download_max_attempts
    transfer_request = FileTransferRequest(
        root=_runtime_item_root(item),
        url=item.url,
        target=item.target,
        overwrite=item.overwrite,
        expected_checksum=item.checksum,
        staging_disposition=(
            StagingDisposition.PRESERVE
            if item.resume_authority is not None
            else StagingDisposition.CLEAN
        ),
        resume_authority=item.resume_authority,
    )

    def observe_retry(
        _attempt: int,
        error: TransferDownloadFilesError,
    ) -> None:
        _notify_runtime_download_state(
            state_observer,
            item,
            "failed",
            error=error,
            resume_authority=error.resume_authority,
        )

    def admit_backend_call(request: TransportRequest) -> None:
        if backend_name == "httpx" and credential_policy is not None:
            credential_policy.authorization_for(httpx.URL(request.url))

    presentation = _RuntimeDownloadPresentation(
        event_sink,
        index=index,
        total=total,
        target=item.relative_target,
        mode=item.download_mode,
        backend=DownloadBackendName(backend_name),
        max_attempts=attempts,
    )
    result = coordinate_transfer_attempts(
        transfer_request,
        backend_name=backend_name,
        backend=backend,
        settings=settings,
        max_attempts=attempts,
        cancel_requested=cancel_requested,
        backend_call_admission=admit_backend_call,
        retry_observer=observe_retry,
        continuation_owner=True,
        event_sink=presentation,
    )
    if isinstance(result, AttemptSucceeded):
        presentation.close()
        return result.attempts, result.outcome
    if isinstance(result, AttemptCancelled):
        presentation.close()
        _notify_runtime_download_state(
            state_observer,
            item,
            "failed",
            error="download cancelled",
            resume_authority=result.resume_authority,
        )
        raise RuntimeFileDownloadCancelled
    error = result.error
    status: Literal["failed", "exhausted"] = (
        "failed" if isinstance(result, AttemptOrdinaryTerminal) else "exhausted"
    )
    if isinstance(result, AttemptLocalFailure):
        status = "failed"
    presentation.close()
    _notify_runtime_download_state(
        state_observer,
        item,
        status,
        error=error,
        resume_authority=(
            result.resume_authority if isinstance(result, AttemptExhausted) else None
        ),
    )
    if isinstance(
        result,
        (AttemptOrdinaryTerminal, AttemptExhausted, AttemptLocalFailure),
    ):
        event_sink.emit(
            RuntimeDownloadFailed(
                item.relative_target,
                item.download_mode,
                config.cdh.download_failure_policy,
                _controlled_download_failure_reason(error),
                result.attempts,
                attempts,
            )
        )
        _apply_runtime_item_failure_policy(
            item,
            error,
            attempts=result.attempts,
            config=config,
            path=path,
            status=status,
        )
    raise AssertionError("attempt coordinator returned an unknown result")


class _RuntimeDownloadPresentation(EventSink[DownloadEvent]):
    """Translate shared transfer facts into one private Runtime item scope."""

    def __init__(
        self,
        sink: RuntimeBackgroundEventSink,
        *,
        index: int,
        total: int,
        target: str,
        mode: Literal["sync", "async"],
        backend: DownloadBackendName,
        max_attempts: int,
    ) -> None:
        self._sink = sink
        self._index = index
        self._total = total
        self._target = target
        self._mode = mode
        self._backend = backend
        self._max_attempts = max_attempts
        self._attempt: int | None = None
        self._scope: object | None = None

    def emit(self, event: DownloadEvent, /) -> None:
        if isinstance(event, DownloadAttemptStarted):
            self.close()
            self._attempt = event.attempt
            self._scope = object()
            self._sink.emit(
                RuntimeDownloadAttemptStarted(
                    self._index,
                    self._total,
                    self._target,
                    self._mode,
                    self._backend,
                    event.attempt,
                    self._max_attempts,
                )
            )
            return
        if isinstance(event, DownloadTransferProgress):
            if self._attempt is None or self._scope is None:
                return
            self._sink.emit_progress(
                self._scope,
                RuntimeDownloadItemProgress(
                    self._index,
                    self._total,
                    self._target,
                    self._mode,
                    self._attempt,
                    self._max_attempts,
                    event,
                ),
            )
            return
        if isinstance(event, DownloadVerificationStarted):
            if self._attempt is None or self._scope is None:
                return
            self.close()
            self._sink.emit(
                RuntimeDownloadItemVerificationStarted(
                    self._index,
                    self._total,
                    self._target,
                )
            )
            return
        if isinstance(event, DownloadRetryScheduled):
            self.close()
            self._sink.emit(
                RuntimeDownloadItemRetryScheduled(
                    self._index,
                    self._total,
                    self._target,
                    self._mode,
                    self._max_attempts,
                    event,
                )
            )

    def close(self) -> None:
        scope = self._scope
        self._scope = None
        self._attempt = None
        if scope is not None:
            self._sink.close_progress(scope)


def _controlled_download_failure_reason(error: Exception) -> DownloadRetryReason:
    if isinstance(error, TransferDownloadFilesError):
        return error.reason
    return DownloadRetryReason.UNKNOWN


def _apply_runtime_item_failure_policy(
    item: RuntimeFilePlanItem,
    error: Exception,
    *,
    attempts: int,
    config: RuntimeConfig,
    path: RuntimeFilePath,
    status: Literal["failed", "exhausted"],
) -> None:
    reason = _controlled_download_failure_reason(error)
    if config.cdh.download_failure_policy == "continue":
        raise _RuntimeDownloadContinued from error
    raise _RuntimeDownloadPolicyFailure(
        (
            Diagnostic(
                path=(*path, "target"),
                code="runtime_file.download_failed",
                message=(
                    f"runtime file download failed after {attempts} attempt(s); "
                    f"reason={reason.value}"
                ),
            ),
        ),
        status=status,
    ) from error


def _runtime_download_not_cancelled() -> bool:
    return False


def _observe_cancellable_runtime_backend(
    backend: DownloadBackend,
    backend_observer: RuntimeDownloadBackendObserver | None,
) -> None:
    if backend_observer is None or not isinstance(backend, CancellableDownloadBackend):
        return
    backend_observer(backend)


def _runtime_backend_observer_once(
    backend_observer: RuntimeDownloadBackendObserver | None,
    observed_backend_ids: set[int],
) -> RuntimeDownloadBackendObserver | None:
    if backend_observer is None:
        return None

    def observe(backend: CancellableDownloadBackend) -> None:
        identity = id(backend)
        if identity in observed_backend_ids:
            return
        observed_backend_ids.add(identity)
        backend_observer(backend)

    return observe


def _notify_runtime_download_state(
    state_observer: RuntimeDownloadStateObserver | None,
    item: RuntimeFilePlanItem,
    status: RuntimeDownloadObservedStatus,
    *,
    error: object | None = None,
    resume_authority: ResumeAuthority | None = None,
) -> None:
    if state_observer is None:
        return
    state_observer(
        item,
        status,
        error=error,
        resume_authority=resume_authority,
    )


def _effective_downloader(
    item: RuntimeFilePlanItem,
    config: RuntimeConfig,
) -> DownloaderName:
    return item.downloader or config.cdh.default_downloader


def _requires_aria2_backend(plan: RuntimeFilePlan, config: RuntimeConfig) -> bool:
    return any(_effective_downloader(item, config) == "aria2" for item in plan.items)


def _prepare_runtime_download_backends(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    backends: Mapping[str, DownloadBackend],
) -> None:
    settings = runtime_downloader_settings(config)
    prepared: set[DownloaderName] = set()
    for item in plan.items:
        backend_name = _effective_downloader(item, config)
        if backend_name in prepared:
            continue
        backend = backends[backend_name]
        if isinstance(backend, DownloadBackendPreparer):
            backend.prepare(settings)
        prepared.add(backend_name)


def _notify_runtime_download_startup(
    startup_observer: RuntimeDownloadStartupObserver | None,
) -> None:
    if startup_observer is None:
        return
    startup_observer()
