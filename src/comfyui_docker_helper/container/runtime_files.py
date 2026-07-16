"""Internal runtime file download planning for the container entrypoint."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from comfyui_docker_helper.config import Diagnostic
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
    Aria2DownloadSettings,
    DownloadBackend,
    DownloadBackendPreparer,
    DownloadCancelled,
    DownloaderSettings,
    DownloadStatus,
    FileDownloadItem,
    HttpxDownloader,
    HttpxDownloadSettings,
    Logger,
    TransferDownloadFilesError,
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

RUNTIME_FILE_IDENTITY_SCHEMA_VERSION = 1
RUNTIME_STAGING_STALE_SECONDS = 24 * 60 * 60
_CDH_STAGING_ARTIFACT_RE = re.compile(r"^cdh-[0-9a-f]{64}\.part(?:\..+)?$")

type RuntimeFilePath = tuple[str | int, ...]
type RuntimeStagingClock = Callable[[], float]
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
    download_mode: Literal["sync", "async"]
    downloader: DownloaderName | None
    action: Literal["download", "skip_existing", "overwrite_existing"]


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
    downloader: DownloaderName | None = None
    download_mode: Literal["sync", "async"] | None = None


class _RuntimeFilesDocument(ConfigModel):
    files: list[_RuntimeFilePatch] | None = None


class _RuntimeFileConfig(ConfigModel):
    dir: str
    filename: str
    url: str
    overwrite: bool = False
    downloader: DownloaderName | None = None
    download_mode: Literal["sync", "async"] | None = None


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
        action = _target_action(root, target, directory, config.overwrite, path)
        if isinstance(action, Diagnostic):
            diagnostics.append(action)
            continue

        items.append(
            RuntimeFilePlanItem(
                url=config.url,
                directory=directory.as_posix(),
                filename=config.filename,
                relative_target=relative_target,
                target=target,
                overwrite=config.overwrite,
                download_mode=config.download_mode or default_download_mode,
                downloader=config.downloader,
                action=action,
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
    staging_cleanup_clock: RuntimeStagingClock = time.time,
    state_observer: RuntimeDownloadStateObserver | None = None,
    cancel_requested: RuntimeDownloadCancelRequested | None = None,
    backend_observer: RuntimeDownloadBackendObserver | None = None,
    extra_protected_staging_targets: Iterable[Path] = (),
) -> tuple[RuntimeFileDownloadResult, ...]:
    """Transfer runtime files to cdh-owned staging targets."""
    settings = runtime_downloader_settings(config)
    is_cancelled = cancel_requested or _runtime_download_not_cancelled
    results: list[RuntimeFileDownloadResult] = []
    current_staging_targets = tuple(
        _runtime_staging_target(item) for item in plan.items
    ) + tuple(extra_protected_staging_targets)

    for index, item in enumerate(plan.items, 1):
        if is_cancelled():
            break
        backend_name = _effective_downloader(item, config)
        staging_target = _runtime_staging_target(item)
        if item.action == "skip_existing":
            log(f"Skipping existing runtime file: {item.target}")
            results.append(
                RuntimeFileDownloadResult(
                    item=item,
                    backend=backend_name,
                    staging_target=staging_target,
                    status=DownloadStatus.SKIPPED,
                )
            )
            continue

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

        staging_item = _runtime_staging_download_item(
            item,
            backend_name,
            settings=settings,
        )

        transfer_completed = False
        try:
            _prepare_staging_parent(
                item,
                staging_item.target.parent,
                ("files", index - 1),
                current_staging_targets=current_staging_targets,
                clock=staging_cleanup_clock,
            )
            log(
                f"Downloading runtime file {index}/{len(plan.items)} "
                f"with {backend_name}"
            )
            _observe_cancellable_runtime_backend(backend, backend_observer)
            attempts_used = _download_runtime_file_with_policy(
                item,
                staging_item,
                backend,
                settings,
                ("files", index - 1),
                config=config,
                log=log,
                state_observer=state_observer,
                cancel_requested=is_cancelled,
            )
            transfer_completed = True
            _raise_if_runtime_download_cancelled(is_cancelled)
            _place_staged_runtime_file(item, staging_item.target, ("files", index - 1))
            if not item.target.is_file() or item.target.is_symlink():
                raise RuntimeFileDownloadError(
                    (
                        Diagnostic(
                            path=("files", index - 1, "target"),
                            code="runtime_file.final_target_not_regular",
                            message=(
                                "final target is not a regular file after placement"
                            ),
                        ),
                    )
                )
            _notify_runtime_download_state(state_observer, item, "completed")
            log(
                "Runtime download completed: "
                f"mode={item.download_mode} target={item.relative_target} "
                f"backend={backend_name} attempts={attempts_used} "
                "status=completed"
            )
            _remove_empty_staging_parent(staging_item.target.parent)
        except _RuntimeDownloadContinued:
            _cleanup_current_staging(staging_item.target)
            _remove_empty_staging_parent(staging_item.target.parent)
            continue
        except RuntimeFileDownloadCancelled:
            _cleanup_current_staging(staging_item.target)
            _remove_empty_staging_parent(staging_item.target.parent)
            break
        except _RuntimeDownloadExhaustedFailure:
            _log_async_queue_stopping_after_exhausted_failure(
                log,
                item,
                config=config,
                pending=len(plan.items) - index,
            )
            _cleanup_current_staging(staging_item.target)
            _remove_empty_staging_parent(staging_item.target.parent)
            raise
        except Exception as error:
            if transfer_completed:
                _notify_runtime_download_state(
                    state_observer,
                    item,
                    "exhausted",
                    error=error,
                )
            _cleanup_current_staging(staging_item.target)
            _remove_empty_staging_parent(staging_item.target.parent)
            raise

        results.append(
            RuntimeFileDownloadResult(
                item=item,
                backend=backend_name,
                staging_target=staging_item.target,
                status=DownloadStatus.DOWNLOADED,
            )
        )

    return tuple(results)


def canonical_runtime_file_identity_bytes(item: RuntimeFilePlanItem) -> bytes:
    """Return canonical runtime file identity bytes for digesting."""
    payload = {
        "schema_version": RUNTIME_FILE_IDENTITY_SCHEMA_VERSION,
        "source": item.url,
        "source_type": "url",
        "target": item.relative_target,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def runtime_file_identity_digest(
    item: RuntimeFilePlanItem,
) -> RuntimeDownloadDigestKey:
    """Return the stable source-target identity digest for a runtime file item."""
    digest = hashlib.sha256(canonical_runtime_file_identity_bytes(item)).hexdigest()
    return f"sha256:{digest}"


def runtime_file_staging_target(item: RuntimeFilePlanItem, digest: str) -> Path:
    """Return the digest-named target-local staging path for reconciliation."""
    digest_suffix = digest.removeprefix("sha256:")
    return item.target.parent / ".cdh-staging" / f"cdh-{digest_suffix}.part"


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
    current_targets = {item.relative_target: item for item in plan.items}
    stale_entry_digests = frozenset(
        digest for digest in state.downloads.entries if digest not in current_digests
    )

    items: list[RuntimeFileReconciliationItem] = []
    scheduled_items: list[RuntimeFilePlanItem] = []
    entries: dict[RuntimeDownloadDigestKey, RuntimeDownloadEntry] = {}

    for item in plan.items:
        digest = runtime_file_identity_digest(item)
        previous_entry = state.downloads.entries.get(digest)
        final_exists = item.target.is_file() and not item.target.is_symlink()

        if not item.overwrite:
            scheduled = not final_exists
            status: Literal["pending", "completed", "skipped"] = (
                "pending" if scheduled else "skipped"
            )
        else:
            status = (
                "completed"
                if previous_entry is not None
                and previous_entry.status == "completed"
                and final_exists
                else "pending"
            )
            scheduled = status == "pending"

        if scheduled:
            scheduled_items.append(_runtime_file_scheduled_item(item))

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
                staging_target=runtime_file_staging_target(item, digest),
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
                current_targets=current_targets,
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


def _runtime_file_scheduled_item(item: RuntimeFilePlanItem) -> RuntimeFilePlanItem:
    return replace(
        item,
        action="overwrite_existing" if item.overwrite else "download",
    )


def _stale_runtime_file_staging_candidates(
    state: RuntimeState,
    *,
    stale_entry_digests: frozenset[str],
    current_targets: Mapping[str, RuntimeFilePlanItem],
    root: Path,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for digest in sorted(stale_entry_digests):
        entry = state.downloads.entries[digest]
        target_item = current_targets.get(entry.target)
        if target_item is not None:
            candidates.append(runtime_file_staging_target(target_item, digest))
            continue

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
    staging_cleanup_clock: RuntimeStagingClock = time.time,
    state_observer: RuntimeDownloadStateObserver | None = None,
    startup_observer: RuntimeDownloadStartupObserver | None = None,
    cancel_requested: RuntimeDownloadCancelRequested | None = None,
    backend_observer: RuntimeDownloadBackendObserver | None = None,
    extra_protected_staging_targets: Iterable[Path] = (),
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
            staging_cleanup_clock=staging_cleanup_clock,
            state_observer=state_observer,
            cancel_requested=is_cancelled,
            backend_observer=observe_backend,
            extra_protected_staging_targets=extra_protected_staging_targets,
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
            staging_cleanup_clock=staging_cleanup_clock,
            state_observer=state_observer,
            cancel_requested=is_cancelled,
            backend_observer=observe_backend,
            extra_protected_staging_targets=extra_protected_staging_targets,
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
        if item.action == "skip_existing":
            continue
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
    staging_item: FileDownloadItem,
    backend: DownloadBackend,
    settings: DownloaderSettings,
    path: RuntimeFilePath,
    *,
    config: RuntimeConfig,
    log: Logger,
    state_observer: RuntimeDownloadStateObserver | None,
    cancel_requested: RuntimeDownloadCancelRequested,
) -> int:
    attempt_settings = _single_runtime_attempt_settings(settings)
    attempts = config.cdh.download_max_attempts
    backend_name = staging_item.downloader
    source_host = runtime_source_host(item.url)
    identity = short_runtime_identity(runtime_file_identity_digest(item))
    for attempt in range(1, attempts + 1):
        _raise_if_runtime_download_cancelled(cancel_requested)
        if _runtime_attempt_requires_clean_staging(staging_item, settings):
            _cleanup_current_staging(staging_item.target)
        if _runtime_resume_state_blocks_backend(staging_item, settings):
            raise RuntimeFileDownloadError(
                (
                    Diagnostic(
                        path=(*path, "target"),
                        code="runtime_file.invalid_resume_staging",
                        message="invalid staged resume state could not be cleaned",
                    ),
                )
            )
        try:
            log(
                "Runtime download attempt: "
                f"mode={item.download_mode} target={item.relative_target} "
                f"backend={backend_name} attempt={attempt}/{attempts} "
                f"status=downloading source_host={source_host} identity={identity}"
            )
            _notify_runtime_download_state(state_observer, item, "downloading")
            _raise_if_runtime_download_cancelled(cancel_requested)
            backend.download(staging_item, attempt_settings)
            _raise_if_runtime_download_cancelled(cancel_requested)
            return attempt
        except RuntimeFileDownloadCancelled:
            raise
        except DownloadCancelled as error:
            raise RuntimeFileDownloadCancelled from error
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
            log(
                "WARNING: Runtime download exhausted: "
                f"mode={item.download_mode} target={item.relative_target} "
                f"backend={backend_name} attempts={attempts}/{attempts} "
                f"policy={config.cdh.download_failure_policy} status=exhausted "
                f"reason={runtime_error_reason(error)}"
            )
            if config.cdh.download_failure_policy == "continue":
                log(
                    "WARNING: runtime file download failed after "
                    f"{attempts} attempt(s), continuing: "
                    f"target={item.relative_target} "
                    f"reason={runtime_error_reason(error)}"
                )
                raise _RuntimeDownloadContinued from error
            raise _RuntimeDownloadExhaustedFailure(
                (
                    Diagnostic(
                        path=(*path, "target"),
                        code="runtime_file.download_failed",
                        message=(
                            "runtime file download failed after "
                            f"{attempts} attempt(s): {error}"
                        ),
                    ),
                )
            ) from error
        except Exception as error:
            _raise_if_runtime_download_cancelled(cancel_requested)
            _notify_runtime_download_state(
                state_observer,
                item,
                "exhausted",
                error=error,
            )
            log(
                "WARNING: Runtime download exhausted: "
                f"mode={item.download_mode} target={item.relative_target} "
                f"backend={backend_name} attempts={attempt}/{attempts} "
                f"policy={config.cdh.download_failure_policy} status=exhausted "
                f"reason={runtime_error_reason(error)}"
            )
            raise


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


def _single_runtime_attempt_settings(
    settings: DownloaderSettings,
) -> DownloaderSettings:
    """Keep runtime policy attempts from multiplying backend HTTPX retries."""
    return DownloaderSettings(
        default=settings.default,
        aria2=settings.aria2,
        httpx=HttpxDownloadSettings(
            timeout=settings.httpx.timeout,
            retries=0,
        ),
    )


def _runtime_attempt_requires_clean_staging(
    item: FileDownloadItem,
    settings: DownloaderSettings,
) -> bool:
    if item.downloader == "httpx":
        return True
    if item.downloader != "aria2":
        return False
    if not settings.aria2.resume_download:
        return True
    return not _runtime_resume_state_is_valid_or_absent(item.target)


def _runtime_resume_state_is_valid_or_absent(staging_target: Path) -> bool:
    try:
        mode = staging_target.lstat().st_mode
    except FileNotFoundError:
        return not _current_staging_sidecars_exist(staging_target)
    except OSError:
        return False
    if not stat.S_ISREG(mode):
        return False
    try:
        with staging_target.open("rb") as staged_file:
            staged_file.read(0)
    except OSError:
        return False
    return True


def _current_staging_sidecars_exist(staging_target: Path) -> bool:
    if not _is_real_cdh_staging_directory(staging_target.parent):
        return False

    staging_prefix = _cdh_staging_prefix(staging_target)
    if staging_prefix is None:
        return False

    try:
        entries = tuple(staging_target.parent.iterdir())
    except OSError:
        return True

    return any(
        path != staging_target and _cdh_staging_prefix(path) == staging_prefix
        for path in entries
    )


def _runtime_resume_state_blocks_backend(
    item: FileDownloadItem,
    settings: DownloaderSettings,
) -> bool:
    return (
        item.downloader == "aria2"
        and settings.aria2.resume_download
        and not _runtime_resume_state_is_valid_or_absent(item.target)
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
    return any(
        item.action != "skip_existing"
        and _effective_downloader(item, config) == "aria2"
        for item in plan.items
    )


def _runtime_staging_download_item(
    item: RuntimeFilePlanItem,
    downloader: DownloaderName,
    *,
    settings: DownloaderSettings,
) -> FileDownloadItem:
    return FileDownloadItem(
        url=item.url,
        filename=item.filename,
        target=_runtime_staging_target(item),
        overwrite=_runtime_staging_overwrite(downloader, settings),
        downloader=downloader,
    )


def _runtime_staging_overwrite(
    downloader: DownloaderName,
    settings: DownloaderSettings,
) -> bool:
    return downloader == "aria2" and not settings.aria2.resume_download


def _runtime_staging_target(item: RuntimeFilePlanItem) -> Path:
    return runtime_file_staging_target(item, runtime_file_identity_digest(item))


def _prepare_staging_parent(
    item: RuntimeFilePlanItem,
    staging_parent: Path,
    path: RuntimeFilePath,
    *,
    current_staging_targets: Iterable[Path],
    clock: RuntimeStagingClock,
) -> None:
    target_parent_error = _validate_existing_parent_contained(
        _runtime_item_root(item),
        PurePosixPath(item.directory),
        path,
    )
    if target_parent_error is not None:
        raise RuntimeFileDownloadError((target_parent_error,))

    try:
        staging_parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeFileDownloadError(
            (
                Diagnostic(
                    path=(*path, "target"),
                    code="runtime_file.staging_parent_failed",
                    message=f"staging parent could not be created: {error}",
                ),
            )
        ) from error

    _validate_staging_parent(staging_parent, path)
    target_parent_error = _validate_resolved_target_parent_contained(item, path)
    if target_parent_error is not None:
        raise RuntimeFileDownloadError((target_parent_error,))
    _cleanup_stale_staging_files(
        staging_parent,
        current_staging_targets=current_staging_targets,
        clock=clock,
    )


def _validate_staging_parent(
    staging_parent: Path,
    path: RuntimeFilePath,
) -> None:
    try:
        mode = staging_parent.lstat().st_mode
    except OSError as error:
        raise RuntimeFileDownloadError(
            (
                Diagnostic(
                    path=(*path, "target"),
                    code="runtime_file.staging_parent_inspect_failed",
                    message=f"staging parent could not be inspected: {error}",
                ),
            )
        ) from error

    if not stat.S_ISDIR(mode):
        raise RuntimeFileDownloadError(
            (
                Diagnostic(
                    path=(*path, "target"),
                    code="runtime_file.staging_parent_invalid",
                    message="staging parent must be a real directory",
                ),
            )
        )


def _place_staged_runtime_file(
    item: RuntimeFilePlanItem,
    staging_target: Path,
    path: RuntimeFilePath,
) -> None:
    staging_error = _validate_staging_target(staging_target, path)
    if staging_error is not None:
        raise RuntimeFileDownloadError((staging_error,))

    existing_error = _validate_final_target_before_replace(item, path)
    if existing_error is not None:
        raise RuntimeFileDownloadError((existing_error,))

    target_parent_error = _validate_resolved_target_parent_contained(item, path)
    if target_parent_error is not None:
        raise RuntimeFileDownloadError((target_parent_error,))

    try:
        staging_target.replace(item.target)
    except OSError as error:
        raise RuntimeFileDownloadError(
            (
                Diagnostic(
                    path=(*path, "target"),
                    code="runtime_file.final_replace_failed",
                    message=f"staged file could not be placed at final target: {error}",
                ),
            )
        ) from error


def _validate_staging_target(
    staging_target: Path,
    path: RuntimeFilePath,
) -> Diagnostic | None:
    try:
        mode = staging_target.lstat().st_mode
    except FileNotFoundError:
        return Diagnostic(
            path=(*path, "target"),
            code="runtime_file.staging_missing",
            message="download backend did not produce a regular staging file",
        )
    except OSError as error:
        return Diagnostic(
            path=(*path, "target"),
            code="runtime_file.staging_inspect_failed",
            message=f"staging file could not be inspected: {error}",
        )

    if not stat.S_ISREG(mode):
        return Diagnostic(
            path=(*path, "target"),
            code="runtime_file.non_regular_staging",
            message="download backend did not produce a regular staging file",
        )
    return None


def _validate_resolved_target_parent_contained(
    item: RuntimeFilePlanItem,
    path: RuntimeFilePath,
) -> Diagnostic | None:
    root = _runtime_item_root(item)
    try:
        resolved_root = root.resolve(strict=False)
    except OSError as error:
        return Diagnostic(
            (*path, "target"),
            "runtime_file.root_resolution_failed",
            f"COMFYUI_PATH cannot be resolved: {error}",
        )

    try:
        item.target.parent.resolve(strict=True).relative_to(resolved_root)
    except ValueError:
        return Diagnostic(
            (*path, "target"),
            "runtime_file.symlink_escape",
            "target parent must not escape COMFYUI_PATH",
        )
    except OSError as error:
        return Diagnostic(
            (*path, "target"),
            "runtime_file.parent_resolution_failed",
            f"target parent cannot be resolved: {error}",
        )
    return None


def _runtime_item_root(item: RuntimeFilePlanItem) -> Path:
    relative_parts = PurePosixPath(item.relative_target).parts
    return item.target.parents[len(relative_parts) - 1]


def _validate_final_target_before_replace(
    item: RuntimeFilePlanItem,
    path: RuntimeFilePath,
) -> Diagnostic | None:
    try:
        mode = item.target.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as error:
        return Diagnostic(
            path=(*path, "target"),
            code="runtime_file.final_target_inspect_failed",
            message=f"final target could not be inspected before placement: {error}",
        )

    if not stat.S_ISREG(mode):
        return Diagnostic(
            path=(*path, "target"),
            code="runtime_file.non_regular_target",
            message="final target exists but is not a regular file",
        )
    if not item.overwrite:
        return Diagnostic(
            path=(*path, "target"),
            code="runtime_file.final_target_exists",
            message="final target appeared before placement and overwrite is false",
        )
    return None


def _cleanup_stale_staging_files(
    staging_parent: Path,
    *,
    current_staging_targets: Iterable[Path],
    clock: RuntimeStagingClock,
) -> None:
    if not _is_real_cdh_staging_directory(staging_parent):
        return

    try:
        entries = tuple(staging_parent.iterdir())
    except FileNotFoundError:
        return
    except OSError:
        return

    current_prefixes = {
        _cdh_staging_prefix(path) for path in current_staging_targets
    } - {None}
    stale_before = clock() - RUNTIME_STAGING_STALE_SECONDS

    for entry in entries:
        if not _is_cdh_staging_artifact(entry):
            continue
        if _cdh_staging_prefix(entry) in current_prefixes:
            continue
        try:
            stat_result = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(stat_result.st_mode):
            continue
        if stat_result.st_mtime >= stale_before:
            continue
        with suppress(OSError):
            entry.unlink()


def _cleanup_current_staging(staging_target: Path) -> None:
    if not _is_real_cdh_staging_directory(staging_target.parent):
        return

    staging_prefix = _cdh_staging_prefix(staging_target)
    if staging_prefix is None:
        return

    try:
        entries = tuple(staging_target.parent.iterdir())
    except OSError:
        return

    for path in entries:
        if _cdh_staging_prefix(path) != staging_prefix:
            continue
        try:
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                path.unlink()
            elif stat.S_ISDIR(mode):
                path.rmdir()
        except OSError:
            pass


def _is_cdh_staging_artifact(path: Path) -> bool:
    if path.parent.name != ".cdh-staging":
        return False
    return _CDH_STAGING_ARTIFACT_RE.fullmatch(path.name) is not None


def _cdh_staging_prefix(path: Path) -> str | None:
    if not _is_cdh_staging_artifact(path):
        return None
    return path.name[: len("cdh-") + 64 + len(".")]


def _remove_empty_staging_parent(staging_parent: Path) -> None:
    if not _is_real_cdh_staging_directory(staging_parent):
        return
    with suppress(OSError):
        staging_parent.rmdir()


def _is_real_cdh_staging_directory(path: Path) -> bool:
    return path.name == ".cdh-staging" and _is_real_directory(path)


def _is_real_directory(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISDIR(mode)


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


def _target_action(
    root: Path,
    target: Path,
    directory: PurePosixPath,
    overwrite: bool,
    path: RuntimeFilePath,
) -> Literal["download", "skip_existing", "overwrite_existing"] | Diagnostic:
    parent_error = _validate_existing_parent_contained(root, directory, path)
    if parent_error is not None:
        return parent_error

    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        return "download"
    except OSError as error:
        return Diagnostic(
            (*path, "target"),
            "runtime_file.target_inspection_failed",
            f"target cannot be inspected: {error}",
        )

    if not target.is_file() or target.is_symlink():
        return Diagnostic(
            (*path, "target"),
            "runtime_file.non_regular_target",
            "existing target must be a regular file",
        )
    del mode
    return "overwrite_existing" if overwrite else "skip_existing"


def _validate_existing_parent_contained(
    root: Path,
    directory: PurePosixPath,
    path: RuntimeFilePath,
) -> Diagnostic | None:
    try:
        resolved_root = root.resolve(strict=False)
    except OSError as error:
        return Diagnostic(
            (*path, "target"),
            "runtime_file.root_resolution_failed",
            f"COMFYUI_PATH cannot be resolved: {error}",
        )

    current = root
    for part in directory.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            return None
        try:
            current.resolve(strict=True).relative_to(resolved_root)
        except ValueError:
            return Diagnostic(
                (*path, "target"),
                "runtime_file.symlink_escape",
                "target parent must not escape COMFYUI_PATH",
            )
        except OSError as error:
            return Diagnostic(
                (*path, "target"),
                "runtime_file.parent_resolution_failed",
                f"target parent cannot be resolved: {error}",
            )
    return None


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
