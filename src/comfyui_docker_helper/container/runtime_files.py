"""Internal runtime file download planning for the container entrypoint."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import ValidationError, field_validator

from comfyui_docker_helper.config import Diagnostic
from comfyui_docker_helper.config.file_checksum import normalize_file_checksum
from comfyui_docker_helper.config.model_base import ConfigModel
from comfyui_docker_helper.config.runtime_file_validation import (
    normalize_runtime_file_path,
    validate_runtime_file_url,
)
from comfyui_docker_helper.config.runtime_models import RuntimeConfig
from comfyui_docker_helper.config.url_validation import DownloaderName
from comfyui_docker_helper.container.download_files import (
    Aria2Downloader,
    Aria2DownloaderFactory,
    DownloadBackendPreparer,
    HttpxDownloader,
)
from comfyui_docker_helper.container.runtime_diagnostics import (
    runtime_error_reason,
    runtime_source_host,
    short_runtime_identity,
)
from comfyui_docker_helper.container.runtime_state import (
    RuntimeDownloadDigestKey,
    RuntimeDownloadEntry,
    RuntimeDownloadsState,
    RuntimeState,
)
from comfyui_docker_helper.container.transfer_core import (
    Aria2DownloadSettings,
    DownloadBackend,
    DownloadCancelled,
    DownloaderSettings,
    DownloadStatus,
    FileTransferOutcome,
    FileTransferRequest,
    HttpxDownloadSettings,
    Logger,
    StagingDisposition,
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
    TransferIdentity,
    admitted_regular_final,
    project_transfer_identity,
    transfer_file,
)

type RuntimeFilePath = tuple[str | int, ...]
type RuntimeDownloadStartupObserver = Callable[[], None]
type RuntimeDownloadCancelRequested = Callable[[], bool]
type RuntimeDownloadBackendObserver = Callable[[DownloadBackend], None]
type RuntimeDownloadObservedStatus = Literal[
    "downloading",
    "failed",
    "exhausted",
    "completed",
]


class RuntimeDownloadStateObserver(Protocol):
    """Optional observer for persisting runtime download state transitions."""

    def __call__(
        self,
        item: RuntimeFilePlanItem,
        status: RuntimeDownloadObservedStatus,
        *,
        error: object | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeFilePlanItem:
    """One normalized runtime file target for download execution."""

    url: str
    directory: str
    filename: str
    relative_target: str
    target: Path
    overwrite: bool
    checksum: str | None
    download_mode: Literal["sync", "async"]
    downloader: DownloaderName | None


@dataclass(frozen=True, slots=True)
class RuntimeFilePlan:
    """Ordered normalized runtime file downloads."""

    items: tuple[RuntimeFilePlanItem, ...]


@dataclass(frozen=True, slots=True)
class RuntimeFileDownloadResult:
    """One runtime file backend transfer result."""

    item: RuntimeFilePlanItem
    backend: DownloaderName
    staging_target: Path
    status: DownloadStatus
    outcome: FileTransferOutcome


@dataclass(frozen=True, slots=True)
class RuntimeFileReconciliationItem:
    """One runtime file item reconciled against filesystem and state."""

    item: RuntimeFilePlanItem
    digest: RuntimeDownloadDigestKey
    status: Literal["pending", "completed", "skipped"]
    scheduled: bool
    staging_target: Path
    previous_entry: RuntimeDownloadEntry | None


@dataclass(frozen=True, slots=True)
class RuntimeFileReconciliation:
    """Pure runtime file reconciliation result for state and execution planning."""

    state: RuntimeState
    download_plan: RuntimeFilePlan
    items: tuple[RuntimeFileReconciliationItem, ...]
    stale_entry_digests: frozenset[str]
    stale_staging_candidates: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _MergedRuntimeFileItem:
    document: dict[str, Any]
    source_index: int


class RuntimeFilePlanError(ValueError):
    """Runtime file planning failure represented by stable diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("runtime file plan errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("runtime file plan is invalid")


class RuntimeFileDownloadError(ValueError):
    """Runtime file download failure represented by stable diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("runtime file download errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("runtime file download is invalid")


class RuntimeFileDownloadCancelled(Exception):
    """Runtime download work stopped after a cooperative cancellation request."""


class _RuntimeFilePatch(ConfigModel):
    dir: str
    filename: str
    url: str | None = None
    overwrite: bool | None = None
    checksum: str | None = None
    downloader: DownloaderName | None = None
    download_mode: Literal["sync", "async"] | None = None

    @field_validator("checksum")
    @classmethod
    def _normalize_checksum(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_file_checksum(value)


class _RuntimeFilesDocument(ConfigModel):
    files: list[_RuntimeFilePatch] | None = None


class _RuntimeFileConfig(ConfigModel):
    dir: str
    filename: str
    url: str
    overwrite: bool = False
    checksum: str | None = None
    downloader: DownloaderName | None = None
    download_mode: Literal["sync", "async"] | None = None

    @field_validator("checksum")
    @classmethod
    def _normalize_checksum(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_file_checksum(value)


def build_runtime_file_plan(
    documents: Iterable[Mapping[str, Any]],
    *,
    comfyui_path: str | Path,
    default_download_mode: Literal["sync", "async"] = "sync",
) -> RuntimeFilePlan:
    """Merge runtime file documents and derive safe target paths."""
    merged_items = _merge_runtime_file_items(documents)
    diagnostics: list[Diagnostic] = []
    items: list[RuntimeFilePlanItem] = []
    root = Path(comfyui_path)

    for item in merged_items:
        path: RuntimeFilePath = ("files", item.source_index)
        try:
            config = _RuntimeFileConfig.model_validate(item.document)
        except ValidationError as error:
            diagnostics.extend(_diagnostics_from_validation_error(error, path))
            continue

        if not validate_runtime_file_url(config.url, (*path, "url"), diagnostics):
            continue

        normalized = _normalize_runtime_file_path(config, path, diagnostics)
        if normalized is None:
            continue

        directory, relative_target = normalized
        target = root.joinpath(*PurePosixPath(relative_target).parts)
        items.append(
            RuntimeFilePlanItem(
                url=config.url,
                directory=directory.as_posix(),
                filename=config.filename,
                relative_target=relative_target,
                target=target,
                overwrite=config.overwrite,
                checksum=config.checksum,
                download_mode=config.download_mode or default_download_mode,
                downloader=config.downloader,
            )
        )

    if diagnostics:
        raise RuntimeFilePlanError(tuple(diagnostics))
    return RuntimeFilePlan(items=tuple(items))


def process_runtime_file_downloads(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    backends: Mapping[str, DownloadBackend],
    log: Logger = print,
    state_observer: RuntimeDownloadStateObserver | None = None,
    cancel_requested: RuntimeDownloadCancelRequested | None = None,
    backend_observer: RuntimeDownloadBackendObserver | None = None,
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
            log(
                f"Processing runtime file {index}/{len(plan.items)} with {backend_name}"
            )
            _observe_cancellable_runtime_backend(backend, backend_observer)
            attempts_used, outcome = _download_runtime_file_with_policy(
                item,
                backend_name,
                backend,
                settings,
                ("files", index - 1),
                config=config,
                log=log,
                state_observer=state_observer,
                cancel_requested=is_cancelled,
            )
            _raise_if_runtime_download_cancelled(is_cancelled)
            _notify_runtime_download_state(state_observer, item, "completed")
            log(
                "Runtime download completed: "
                f"mode={item.download_mode} target={item.relative_target} "
                f"backend={backend_name} attempts={attempts_used} "
                f"status={outcome.status} verification={outcome.verification}"
            )
        except _RuntimeDownloadContinued:
            continue
        except RuntimeFileDownloadCancelled:
            break
        except _RuntimeDownloadExhaustedFailure:
            _log_async_queue_stopping_after_exhausted_failure(
                log,
                item,
                config=config,
                pending=len(plan.items) - index,
            )
            raise
        except Exception as error:
            _notify_runtime_download_state(
                state_observer,
                item,
                "exhausted",
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


def canonical_runtime_file_identity_bytes(item: RuntimeFilePlanItem) -> bytes:
    """Return canonical runtime file identity bytes for digesting."""
    return _runtime_transfer_identity(item).canonical_bytes


def runtime_file_identity_digest(
    item: RuntimeFilePlanItem,
) -> RuntimeDownloadDigestKey:
    """Return the stable source-target identity digest for a runtime file item."""
    return _runtime_transfer_identity(item).digest


def runtime_file_staging_target(item: RuntimeFilePlanItem) -> Path:
    """Return shared desired-identity staging for reconciliation."""
    return _runtime_transfer_identity(item).staging_target


def _runtime_transfer_identity(item: RuntimeFilePlanItem) -> TransferIdentity:
    return project_transfer_identity(
        root=_runtime_item_root(item),
        url=item.url,
        target=item.target,
        expected_checksum=item.checksum,
    )


def reconcile_runtime_file_plan(
    plan: RuntimeFilePlan,
    state: RuntimeState,
    *,
    now: datetime,
    comfyui_path: str | Path,
) -> RuntimeFileReconciliation:
    """Reconcile desired runtime files against final files and persisted state."""
    root = Path(comfyui_path)
    current_digests = {runtime_file_identity_digest(item): item for item in plan.items}
    stale_entry_digests = frozenset(
        digest for digest in state.downloads.entries if digest not in current_digests
    )

    items: list[RuntimeFileReconciliationItem] = []
    scheduled_items: list[RuntimeFilePlanItem] = []
    entries: dict[RuntimeDownloadDigestKey, RuntimeDownloadEntry] = {}

    for item in plan.items:
        digest = runtime_file_identity_digest(item)
        previous_entry = state.downloads.entries.get(digest)
        final_exists = admitted_regular_final(root, item.target)

        status: Literal["pending", "completed", "skipped"] = (
            "completed"
            if previous_entry is not None
            and previous_entry.status == "completed"
            and final_exists
            and item.checksum is None
            else "pending"
        )
        scheduled = status == "pending"

        if scheduled:
            scheduled_items.append(item)

        entry = _runtime_download_entry_for_reconciliation(
            item,
            previous_entry,
            status=status,
            state=state,
            now=now,
        )
        entries[digest] = entry
        items.append(
            RuntimeFileReconciliationItem(
                item=item,
                digest=digest,
                status=status,
                scheduled=scheduled,
                staging_target=runtime_file_staging_target(item),
                previous_entry=previous_entry,
            )
        )

    reconciled_state = RuntimeState(
        schema_version=state.schema_version,
        updated_at=now,
        run_id=state.run_id,
        downloads=RuntimeDownloadsState(entries=entries),
    )
    return RuntimeFileReconciliation(
        state=reconciled_state,
        download_plan=RuntimeFilePlan(items=tuple(scheduled_items)),
        items=tuple(items),
        stale_entry_digests=stale_entry_digests,
        stale_staging_candidates=tuple(
            _stale_runtime_file_staging_candidates(
                state,
                stale_entry_digests=stale_entry_digests,
                root=root,
            )
        ),
    )


def _runtime_download_entry_for_reconciliation(
    item: RuntimeFilePlanItem,
    previous_entry: RuntimeDownloadEntry | None,
    *,
    status: Literal["pending", "completed", "skipped"],
    state: RuntimeState,
    now: datetime,
) -> RuntimeDownloadEntry:
    attempts = previous_entry.attempts if previous_entry is not None else 0
    attempt_run_id = (
        previous_entry.attempt_run_id if previous_entry is not None else state.run_id
    )
    return RuntimeDownloadEntry(
        target=item.relative_target,
        download_mode=item.download_mode,
        status=status,
        attempts=attempts,
        attempt_run_id=attempt_run_id,
        last_error=None,
        updated_at=now,
    )


def _stale_runtime_file_staging_candidates(
    state: RuntimeState,
    *,
    stale_entry_digests: frozenset[str],
    root: Path,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for digest in sorted(stale_entry_digests):
        entry = state.downloads.entries[digest]
        target = root.joinpath(*PurePosixPath(entry.target).parts)
        candidates.append(
            target.parent
            / ".cdh-staging"
            / f"cdh-{digest.removeprefix('sha256:')}.part"
        )
    return tuple(candidates)


def download_runtime_files(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    httpx_downloader: DownloadBackend | None = None,
    aria2_downloader_factory: Aria2DownloaderFactory = Aria2Downloader,
    log: Logger = print,
    state_observer: RuntimeDownloadStateObserver | None = None,
    startup_observer: RuntimeDownloadStartupObserver | None = None,
    cancel_requested: RuntimeDownloadCancelRequested | None = None,
    backend_observer: RuntimeDownloadBackendObserver | None = None,
) -> tuple[RuntimeFileDownloadResult, ...]:
    """Download runtime file plan items through existing backend adapters."""
    is_cancelled = cancel_requested or _runtime_download_not_cancelled
    observed_backend_ids: set[int] = set()
    observe_backend = _runtime_backend_observer_once(
        backend_observer,
        observed_backend_ids,
    )
    httpx_backend = httpx_downloader or HttpxDownloader(log=log)
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
            log=log,
            state_observer=state_observer,
            cancel_requested=is_cancelled,
            backend_observer=observe_backend,
        )

    with aria2_downloader_factory(log=log) as aria2_backend:
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
            log=log,
            state_observer=state_observer,
            cancel_requested=is_cancelled,
            backend_observer=observe_backend,
        )


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
        prepare = getattr(backend, "prepare", None)
        if prepare is not None:
            preparer: DownloadBackendPreparer = backend
            preparer.prepare(settings)
        prepared.add(backend_name)


def _notify_runtime_download_startup(
    startup_observer: RuntimeDownloadStartupObserver | None,
) -> None:
    if startup_observer is None:
        return
    startup_observer()


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
            retries=downloader.httpx.retries,
        ),
    )


class _RuntimeDownloadContinued(Exception):
    """Internal marker for exhausted transfer failures handled by continue."""


class _RuntimeDownloadExhaustedFailure(RuntimeFileDownloadError):
    """Internal marker for exhausted transfer failures handled by fail policy."""


def _download_runtime_file_with_policy(
    item: RuntimeFilePlanItem,
    backend_name: DownloaderName,
    backend: DownloadBackend,
    settings: DownloaderSettings,
    path: RuntimeFilePath,
    *,
    config: RuntimeConfig,
    log: Logger,
    state_observer: RuntimeDownloadStateObserver | None,
    cancel_requested: RuntimeDownloadCancelRequested,
) -> tuple[int, FileTransferOutcome]:
    attempts = config.cdh.download_max_attempts
    source_host = runtime_source_host(item.url)
    identity = short_runtime_identity(runtime_file_identity_digest(item))
    transfer_request = FileTransferRequest(
        root=_runtime_item_root(item),
        url=item.url,
        target=item.target,
        overwrite=item.overwrite,
        expected_checksum=item.checksum,
        staging_disposition=StagingDisposition.CLEAN,
    )
    for attempt in range(1, attempts + 1):
        _raise_if_runtime_download_cancelled(cancel_requested)
        try:
            log(
                "Runtime download attempt: "
                f"mode={item.download_mode} target={item.relative_target} "
                f"backend={backend_name} attempt={attempt}/{attempts} "
                f"status=downloading source_host={source_host} identity={identity}"
            )
            _notify_runtime_download_state(state_observer, item, "downloading")
            _raise_if_runtime_download_cancelled(cancel_requested)
            outcome = transfer_file(
                transfer_request,
                backend=backend,
                settings=settings,
            )
            _raise_if_runtime_download_cancelled(cancel_requested)
            return attempt, outcome
        except RuntimeFileDownloadCancelled:
            raise
        except DownloadCancelled as error:
            raise RuntimeFileDownloadCancelled from error
        except TerminalTransferDownloadFilesError as error:
            _notify_runtime_download_state(
                state_observer,
                item,
                "exhausted",
                error=error,
            )
            _apply_runtime_item_failure_policy(
                item,
                error,
                attempts=attempt,
                config=config,
                path=path,
                log=log,
            )
        except TransferDownloadFilesError as error:
            _raise_if_runtime_download_cancelled(cancel_requested)
            if attempt < attempts:
                _notify_runtime_download_state(
                    state_observer,
                    item,
                    "failed",
                    error=error,
                )
                log(
                    "Runtime download attempt failed: "
                    f"mode={item.download_mode} target={item.relative_target} "
                    f"backend={backend_name} attempt={attempt}/{attempts} "
                    f"status=failed reason={runtime_error_reason(error)}"
                )
                log(
                    "Retrying runtime file download after attempt "
                    f"{attempt}/{attempts} failed: target={item.relative_target} "
                    f"reason={runtime_error_reason(error)}"
                )
                continue
            _notify_runtime_download_state(
                state_observer,
                item,
                "exhausted",
                error=error,
            )
            _apply_runtime_item_failure_policy(
                item,
                error,
                attempts=attempts,
                config=config,
                path=path,
                log=log,
            )


def _apply_runtime_item_failure_policy(
    item: RuntimeFilePlanItem,
    error: Exception,
    *,
    attempts: int,
    config: RuntimeConfig,
    path: RuntimeFilePath,
    log: Logger,
) -> None:
    log(
        "WARNING: Runtime download exhausted: "
        f"mode={item.download_mode} target={item.relative_target} "
        f"attempts={attempts}/{config.cdh.download_max_attempts} "
        f"policy={config.cdh.download_failure_policy} status=exhausted "
        f"reason={runtime_error_reason(error)}"
    )
    if config.cdh.download_failure_policy == "continue":
        log(
            "WARNING: runtime file download failed after "
            f"{attempts} attempt(s), continuing: target={item.relative_target} "
            f"reason={runtime_error_reason(error)}"
        )
        raise _RuntimeDownloadContinued from error
    raise _RuntimeDownloadExhaustedFailure(
        (
            Diagnostic(
                path=(*path, "target"),
                code="runtime_file.download_failed",
                message=(
                    f"runtime file download failed after {attempts} attempt(s): {error}"
                ),
            ),
        )
    ) from error


def _log_async_queue_stopping_after_exhausted_failure(
    log: Logger,
    item: RuntimeFilePlanItem,
    *,
    config: RuntimeConfig,
    pending: int,
) -> None:
    if item.download_mode != "async" or config.cdh.download_failure_policy != "fail":
        return
    log(
        "WARNING: Async runtime download queue stopping: "
        "reason=download_exhausted "
        f"policy={config.cdh.download_failure_policy} "
        f"target={item.relative_target} pending={pending}"
    )


def _runtime_download_not_cancelled() -> bool:
    return False


def _raise_if_runtime_download_cancelled(
    cancel_requested: RuntimeDownloadCancelRequested,
) -> None:
    if cancel_requested():
        raise RuntimeFileDownloadCancelled


def _observe_cancellable_runtime_backend(
    backend: DownloadBackend,
    backend_observer: RuntimeDownloadBackendObserver | None,
) -> None:
    if backend_observer is None or not callable(getattr(backend, "cancel", None)):
        return
    backend_observer(backend)


def _runtime_backend_observer_once(
    backend_observer: RuntimeDownloadBackendObserver | None,
    observed_backend_ids: set[int],
) -> RuntimeDownloadBackendObserver | None:
    if backend_observer is None:
        return None

    def observe(backend: DownloadBackend) -> None:
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
) -> None:
    if state_observer is None:
        return
    state_observer(item, status, error=error)


def merge_runtime_file_items(
    documents: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Merge runtime file arrays by normalized relative target path."""
    return tuple(item.document for item in _merge_runtime_file_items(documents))


def _merge_runtime_file_items(
    documents: Iterable[Mapping[str, Any]],
) -> tuple[_MergedRuntimeFileItem, ...]:
    merged: list[_MergedRuntimeFileItem] = []
    indexes: dict[str, int] = {}
    diagnostics: list[Diagnostic] = []

    for document in documents:
        try:
            parsed = _RuntimeFilesDocument.model_validate(
                _files_only_document(document)
            )
        except ValidationError as error:
            diagnostics.extend(_diagnostics_from_validation_error(error, ()))
            continue

        if parsed.files is None:
            continue
        if not parsed.files:
            merged.clear()
            indexes.clear()
            continue

        for source_index, item in enumerate(parsed.files):
            path: RuntimeFilePath = ("files", source_index)
            item_document = item.model_dump(mode="json", exclude_none=True)
            has_valid_url = validate_runtime_file_url(
                item.url,
                (*path, "url"),
                diagnostics,
            )
            key = _merge_key(item, path, diagnostics)
            if key is None or not has_valid_url:
                continue
            if key in indexes:
                previous = merged[indexes[key]]
                merged[indexes[key]] = _MergedRuntimeFileItem(
                    document={**previous.document, **item_document},
                    source_index=source_index,
                )
            else:
                indexes[key] = len(merged)
                merged.append(
                    _MergedRuntimeFileItem(
                        document=item_document,
                        source_index=source_index,
                    )
                )

    if diagnostics:
        raise RuntimeFilePlanError(tuple(diagnostics))
    return tuple(merged)


def _files_only_document(document: Mapping[str, Any]) -> dict[str, Any]:
    if "files" not in document:
        return {}
    return {"files": document["files"]}


def _effective_downloader(
    item: RuntimeFilePlanItem,
    config: RuntimeConfig,
) -> DownloaderName:
    return item.downloader or config.cdh.default_downloader


def _requires_aria2_backend(plan: RuntimeFilePlan, config: RuntimeConfig) -> bool:
    return any(_effective_downloader(item, config) == "aria2" for item in plan.items)


def _runtime_item_root(item: RuntimeFilePlanItem) -> Path:
    relative_parts = PurePosixPath(item.relative_target).parts
    return item.target.parents[len(relative_parts) - 1]


def _merge_key(
    item: _RuntimeFilePatch,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> str | None:
    normalized = normalize_runtime_file_path(item.dir, item.filename, path, diagnostics)
    if normalized is None:
        return None
    return normalized[1]


def _normalize_runtime_file_path(
    item: _RuntimeFilePatch | _RuntimeFileConfig,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> tuple[PurePosixPath, str] | None:
    return normalize_runtime_file_path(item.dir, item.filename, path, diagnostics)


def _diagnostics_from_validation_error(
    error: ValidationError,
    prefix: RuntimeFilePath,
) -> tuple[Diagnostic, ...]:
    return tuple(
        Diagnostic(
            path=(*prefix, *_normalize_pydantic_location(item["loc"])),
            code=f"schema.{item['type']}",
            message=item["msg"],
        )
        for item in error.errors(include_url=False, include_context=False)
    )


def _normalize_pydantic_location(location: tuple[Any, ...]) -> RuntimeFilePath:
    return tuple(
        part if isinstance(part, (str, int)) else str(part) for part in location
    )
