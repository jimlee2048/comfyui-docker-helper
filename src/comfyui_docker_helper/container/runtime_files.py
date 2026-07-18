"""Internal runtime file download planning for the container entrypoint."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
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
from comfyui_docker_helper.container.attempt_coordinator import (
    AttemptCancelled,
    AttemptExhausted,
    AttemptOrdinaryTerminal,
    AttemptSucceeded,
    coordinate_transfer_attempts,
)
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
    RuntimeResumeState,
    RuntimeState,
    RuntimeStateError,
    failed_runtime_download_entry,
    runtime_download_desired_identity_digest,
)
from comfyui_docker_helper.container.transfer_core import (
    Aria2DownloadSettings,
    CancellableDownloadBackend,
    DownloadBackend,
    DownloaderSettings,
    DownloadFilesError,
    DownloadStatus,
    FileTransferOutcome,
    FileTransferRequest,
    HttpxDownloadSettings,
    Logger,
    PreservedTransferCleanupError,
    ResumeAuthority,
    StagingDisposition,
    TransferDownloadFilesError,
    TransferIdentity,
    _admit_preserved_transfer,
    admitted_regular_final,
    confirm_indexed_transfer_artifacts_absent,
    discard_preserved_transfer,
    project_transfer_identity,
)

type RuntimeFilePath = tuple[str | int, ...]
type RuntimeDownloadStartupObserver = Callable[[], None]
type RuntimeDownloadCancelRequested = Callable[[], bool]
type RuntimeDownloadBackendObserver = Callable[[CancellableDownloadBackend], None]
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
        resume_authority: ResumeAuthority | None = None,
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
    resume_authority: ResumeAuthority | None = None


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
    status: Literal["pending", "completed"]
    scheduled: bool
    staging_target: Path
    previous_entry: RuntimeDownloadEntry | None


@dataclass(frozen=True, slots=True)
class RuntimeFileReconciliation:
    """Runtime state and execution plan after bounded indexed reconciliation."""

    state: RuntimeState
    download_plan: RuntimeFilePlan
    items: tuple[RuntimeFileReconciliationItem, ...]
    stale_entry_digests: frozenset[str]
    cleanup_pending_digests: frozenset[str]


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
        except _RuntimeDownloadPolicyFailure as error:
            _log_async_queue_stopping_after_failure(
                log,
                item,
                status=error.status,
                config=config,
                pending=len(plan.items) - index,
            )
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


def canonical_runtime_file_identity_bytes(item: RuntimeFilePlanItem) -> bytes:
    """Return canonical runtime file identity bytes for digesting."""
    return _runtime_transfer_identity(item).canonical_bytes


def runtime_file_identity_digest(
    item: RuntimeFilePlanItem,
) -> RuntimeDownloadDigestKey:
    """Return the stable source-target identity digest for a runtime file item."""
    return _runtime_transfer_identity(item).digest


def runtime_file_state_identity_digest(
    item: RuntimeFilePlanItem,
    *,
    default_downloader: DownloaderName | None = None,
) -> RuntimeDownloadDigestKey:
    """Return runtime desired identity without changing transfer staging identity."""
    downloader = item.downloader or default_downloader
    if downloader is None:
        raise RuntimeStateError("runtime desired identity requires a downloader")
    return runtime_download_desired_identity_digest(
        source=item.url,
        target=item.relative_target,
        checksum=item.checksum,
        overwrite=item.overwrite,
        downloader=downloader,
    )


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
    default_downloader: DownloaderName,
    resume_download: bool,
) -> RuntimeFileReconciliation:
    """Reconcile desired files and execute exact state-indexed stale cleanup."""
    root = Path(comfyui_path)
    admitted_items = tuple(
        replace(item, downloader=item.downloader or default_downloader)
        for item in plan.items
    )
    current_digests = {
        runtime_file_state_identity_digest(item): item for item in admitted_items
    }
    stale_entry_digests = frozenset(
        digest for digest in state.downloads.entries if digest not in current_digests
    )

    state_namespaces = _validate_runtime_state_entries(root, state)
    current_namespaces = {runtime_file_identity_digest(item) for item in admitted_items}

    items: list[RuntimeFileReconciliationItem] = []
    scheduled_items: list[RuntimeFilePlanItem] = []
    entries: dict[RuntimeDownloadDigestKey, RuntimeDownloadEntry] = {}
    cleanup_pending_digests: set[str] = set()

    for digest in sorted(stale_entry_digests):
        entry = state.downloads.entries[digest]
        pending = _reconcile_stale_runtime_entry(
            root,
            entry,
            now=now,
        )
        if pending is not None:
            if state_namespaces[digest] in current_namespaces:
                raise DownloadFilesError(
                    "current runtime transfer namespace has unresolved stale cleanup"
                )
            entries[digest] = pending
            cleanup_pending_digests.add(digest)

    for item in admitted_items:
        digest = runtime_file_state_identity_digest(item)
        previous_entry = state.downloads.entries.get(digest)
        final_exists = admitted_regular_final(root, item.target)

        status: Literal["pending", "completed"] = (
            "completed"
            if previous_entry is not None
            and previous_entry.status == "completed"
            and final_exists
            and item.checksum is None
            else "pending"
        )
        scheduled = status == "pending"

        resume_authority: ResumeAuthority | None = None
        if scheduled:
            resume_authority = _current_resume_authority(
                root,
                item,
                previous_entry,
                resume_download=resume_download,
            )
            scheduled_items.append(
                replace(
                    item,
                    resume_authority=resume_authority,
                )
            )

        entry = _runtime_download_entry_for_reconciliation(
            item,
            previous_entry,
            status=status,
            state=state,
            now=now,
            resume_authority=resume_authority,
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
        cleanup_pending_digests=frozenset(cleanup_pending_digests),
    )


def _runtime_download_entry_for_reconciliation(
    item: RuntimeFilePlanItem,
    previous_entry: RuntimeDownloadEntry | None,
    *,
    status: Literal["pending", "completed"],
    state: RuntimeState,
    now: datetime,
    resume_authority: ResumeAuthority | None,
) -> RuntimeDownloadEntry:
    attempts = previous_entry.attempts if previous_entry is not None else 0
    attempt_run_id = (
        previous_entry.attempt_run_id if previous_entry is not None else state.run_id
    )
    return RuntimeDownloadEntry(
        source=item.url,
        target=item.relative_target,
        checksum=item.checksum,
        overwrite=item.overwrite,
        downloader=item.downloader,
        download_mode=item.download_mode,
        status=status,
        attempts=attempts,
        attempt_run_id=attempt_run_id,
        resume=(
            RuntimeResumeState.from_authority(resume_authority)
            if resume_authority is not None
            else None
        ),
        last_error=None,
        updated_at=now,
    )


def _validate_runtime_state_entries(
    root: Path,
    state: RuntimeState,
) -> dict[str, str]:
    namespaces: dict[str, str] = {}
    namespace_owners: dict[str, str] = {}
    for digest, entry in state.downloads.entries.items():
        expected = runtime_download_desired_identity_digest(
            source=entry.source,
            target=entry.target,
            checksum=entry.checksum,
            overwrite=entry.overwrite,
            downloader=entry.downloader,
        )
        if expected != digest:
            raise RuntimeStateError(
                "runtime state is invalid; remove the state file and restart"
            )
        transfer_digest = _entry_transfer_identity(root, entry).digest
        owner = namespace_owners.get(transfer_digest)
        if owner is not None and owner != digest:
            raise RuntimeStateError(
                "runtime state is invalid; remove the state file and restart"
            )
        namespace_owners[transfer_digest] = digest
        namespaces[digest] = transfer_digest
    return namespaces


def validate_runtime_file_state_plan(
    plan: RuntimeFilePlan,
    state: RuntimeState,
    *,
    comfyui_path: str | Path,
    default_downloader: DownloaderName,
    expected_run_id: str,
) -> None:
    """Re-admit an execution plan against the complete canonical state identity."""
    root = Path(comfyui_path)
    _validate_runtime_state_entries(root, state)
    if state.run_id != expected_run_id:
        raise RuntimeStateError("runtime download state belongs to another start")
    for item in plan.items:
        admitted = replace(item, downloader=item.downloader or default_downloader)
        digest = runtime_file_state_identity_digest(admitted)
        try:
            entry = state.downloads.entries[digest]
        except KeyError as error:
            raise RuntimeStateError(
                f"runtime download state entry is missing for {item.relative_target}"
            ) from error
        expected_resume = _entry_resume_authority(root, entry)
        if (
            entry.source != admitted.url
            or entry.target != admitted.relative_target
            or entry.checksum != admitted.checksum
            or entry.overwrite != admitted.overwrite
            or entry.downloader != admitted.downloader
            or entry.download_mode != admitted.download_mode
            or expected_resume != admitted.resume_authority
            or entry.status != "pending"
            or entry.attempt_run_id != expected_run_id
            or entry.attempts != 0
        ):
            raise RuntimeStateError(
                f"runtime download state identity differs for {item.relative_target}"
            )


def _reconcile_stale_runtime_entry(
    root: Path,
    entry: RuntimeDownloadEntry,
    *,
    now: datetime,
) -> RuntimeDownloadEntry | None:
    target = root.joinpath(*PurePosixPath(entry.target).parts)
    transfer_digest = _entry_transfer_identity(root, entry).digest
    authority = _entry_resume_authority(root, entry)
    try:
        absent = confirm_indexed_transfer_artifacts_absent(
            root=root,
            target=target,
            identity_digest=transfer_digest,
        )
    except (DownloadFilesError, OSError) as error:
        return failed_runtime_download_entry(
            entry,
            status="cleanup_pending",
            last_error=error,
            updated_at=now,
            resume_authority=authority,
        )
    if absent:
        return None
    if authority is not None:
        request = FileTransferRequest(
            root=root,
            url=entry.source,
            target=target,
            overwrite=entry.overwrite,
            expected_checksum=entry.checksum,
            staging_disposition=StagingDisposition.PRESERVE,
            resume_authority=authority,
        )
        try:
            discard_preserved_transfer(request)
        except (DownloadFilesError, OSError) as error:
            return failed_runtime_download_entry(
                entry,
                status="cleanup_pending",
                last_error=error,
                updated_at=now,
                resume_authority=authority,
            )
        return None

    return failed_runtime_download_entry(
        entry,
        status="cleanup_pending",
        last_error="interrupted transfer lacks exact artifact authority",
        updated_at=now,
    )


def _current_resume_authority(
    root: Path,
    item: RuntimeFilePlanItem,
    entry: RuntimeDownloadEntry | None,
    *,
    resume_download: bool,
) -> ResumeAuthority | None:
    transfer_digest = runtime_file_identity_digest(item)
    authority = _entry_resume_authority(root, entry)
    request: FileTransferRequest | None = None
    artifact_admission: Literal["absent", "partial", "complete"] | None = None
    if authority is not None:
        request = FileTransferRequest(
            root=root,
            url=item.url,
            target=item.target,
            overwrite=item.overwrite,
            expected_checksum=item.checksum,
            staging_disposition=StagingDisposition.PRESERVE,
            resume_authority=authority,
        )
        try:
            artifact_admission = _admit_preserved_transfer(request)
        except (DownloadFilesError, OSError) as error:
            raise DownloadFilesError(
                "current runtime resume artifacts failed exact admission"
            ) from error
    may_resume = (
        authority is not None
        and artifact_admission == "complete"
        and item.downloader == "aria2"
        and resume_download
        and entry is not None
        and entry.status != "cleanup_pending"
    )
    if may_resume:
        return authority

    target = item.target
    try:
        absent = confirm_indexed_transfer_artifacts_absent(
            root=root,
            target=target,
            identity_digest=transfer_digest,
        )
    except (DownloadFilesError, OSError) as error:
        raise DownloadFilesError(
            "current runtime transfer cleanup could not be established"
        ) from error
    if absent:
        return None
    if authority is None:
        raise DownloadFilesError(
            "current runtime transfer namespace lacks exact cleanup authority"
        )
    assert request is not None
    try:
        discard_preserved_transfer(request)
    except (DownloadFilesError, OSError) as error:
        raise DownloadFilesError("current runtime transfer cleanup failed") from error
    return None


def _entry_transfer_identity(
    root: Path,
    entry: RuntimeDownloadEntry,
) -> TransferIdentity:
    target = root.joinpath(*PurePosixPath(entry.target).parts)
    return project_transfer_identity(
        root=root,
        url=entry.source,
        target=target,
        expected_checksum=entry.checksum,
    )


def _entry_resume_authority(
    root: Path,
    entry: RuntimeDownloadEntry | None,
) -> ResumeAuthority | None:
    if entry is None or entry.resume is None:
        return None
    return entry.resume.as_authority(_entry_transfer_identity(root, entry).digest)


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
        staging_disposition=(
            StagingDisposition.PRESERVE
            if item.resume_authority is not None
            else StagingDisposition.CLEAN
        ),
        resume_authority=item.resume_authority,
    )

    def observe_start(attempt: int) -> None:
        log(
            "Runtime download attempt: "
            f"mode={item.download_mode} target={item.relative_target} "
            f"backend={backend_name} attempt={attempt}/{attempts} "
            f"status=downloading source_host={source_host} identity={identity}"
        )
        _notify_runtime_download_state(state_observer, item, "downloading")

    def observe_retry(
        attempt: int,
        error: TransferDownloadFilesError,
    ) -> None:
        _notify_runtime_download_state(
            state_observer,
            item,
            "failed",
            error=error,
            resume_authority=error.resume_authority,
        )
        log(
            "Runtime download attempt failed: "
            f"mode={item.download_mode} target={item.relative_target} "
            f"backend={backend_name} attempt={attempt}/{attempts} "
            f"status=failed reason={runtime_error_reason(error)}"
        )

    result = coordinate_transfer_attempts(
        transfer_request,
        backend_name=backend_name,
        backend=backend,
        settings=settings,
        max_attempts=attempts,
        cancel_requested=cancel_requested,
        attempt_start_observer=observe_start,
        retry_observer=observe_retry,
        continuation_owner=True,
        log=log,
    )
    if isinstance(result, AttemptSucceeded):
        return result.attempts, result.outcome
    if isinstance(result, AttemptCancelled):
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
    _notify_runtime_download_state(
        state_observer,
        item,
        status,
        error=error,
        resume_authority=(
            result.resume_authority if isinstance(result, AttemptExhausted) else None
        ),
    )
    if isinstance(result, (AttemptOrdinaryTerminal, AttemptExhausted)):
        _apply_runtime_item_failure_policy(
            item,
            error,
            attempts=result.attempts,
            config=config,
            path=path,
            status=status,
            log=log,
        )
    raise AssertionError("attempt coordinator returned an unknown result")


def _apply_runtime_item_failure_policy(
    item: RuntimeFilePlanItem,
    error: Exception,
    *,
    attempts: int,
    config: RuntimeConfig,
    path: RuntimeFilePath,
    status: Literal["failed", "exhausted"],
    log: Logger,
) -> None:
    log(
        f"WARNING: Runtime download {status}: "
        f"mode={item.download_mode} target={item.relative_target} "
        f"attempts={attempts}/{config.cdh.download_max_attempts} "
        f"policy={config.cdh.download_failure_policy} status={status} "
        f"reason={runtime_error_reason(error)}"
    )
    if config.cdh.download_failure_policy == "continue":
        log(
            "WARNING: runtime file download failed after "
            f"{attempts} attempt(s), continuing: target={item.relative_target} "
            f"reason={runtime_error_reason(error)}"
        )
        raise _RuntimeDownloadContinued from error
    raise _RuntimeDownloadPolicyFailure(
        (
            Diagnostic(
                path=(*path, "target"),
                code="runtime_file.download_failed",
                message=(
                    f"runtime file download failed after {attempts} attempt(s): {error}"
                ),
            ),
        ),
        status=status,
    ) from error


def _log_async_queue_stopping_after_failure(
    log: Logger,
    item: RuntimeFilePlanItem,
    *,
    status: Literal["failed", "exhausted"],
    config: RuntimeConfig,
    pending: int,
) -> None:
    if item.download_mode != "async" or config.cdh.download_failure_policy != "fail":
        return
    log(
        "WARNING: Async runtime download queue stopping: "
        f"reason=download_{status} "
        f"policy={config.cdh.download_failure_policy} "
        f"target={item.relative_target} pending={pending}"
    )


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
