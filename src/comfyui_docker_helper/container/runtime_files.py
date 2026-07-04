"""Internal runtime file download planning for the container entrypoint."""

from __future__ import annotations

import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import ValidationError

from comfyui_docker_helper.config import Diagnostic
from comfyui_docker_helper.config.models import ConfigModel
from comfyui_docker_helper.config.runtime_projection import RuntimeConfig
from comfyui_docker_helper.config.url_validation import (
    DownloaderName,
    is_http_url,
    validate_file_name,
    validate_relative_file_directory,
)
from comfyui_docker_helper.container.download_files import (
    Aria2Downloader,
    Aria2DownloaderFactory,
    Aria2DownloadSettings,
    DownloadBackend,
    DownloaderSettings,
    DownloadStatus,
    FileDownloadItem,
    HttpxDownloader,
    HttpxDownloadSettings,
    Logger,
    TransferDownloadFilesError,
)

type RuntimeFilePath = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class RuntimeFilePlanItem:
    """One normalized runtime file target for download execution."""

    url: str
    directory: str
    filename: str
    relative_target: str
    target: Path
    overwrite: bool
    download_mode: Literal["sync"]
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


class _RuntimeFilePatch(ConfigModel):
    dir: str
    filename: str
    url: str | None = None
    overwrite: bool | None = None
    downloader: DownloaderName | None = None
    download_mode: Literal["sync"] | None = None


class _RuntimeFilesDocument(ConfigModel):
    files: list[_RuntimeFilePatch] | None = None


class _RuntimeFileConfig(ConfigModel):
    dir: str
    filename: str
    url: str
    overwrite: bool = False
    downloader: DownloaderName | None = None
    download_mode: Literal["sync"] = "sync"


def build_runtime_file_plan(
    documents: Iterable[Mapping[str, Any]],
    *,
    comfyui_path: str | Path,
) -> RuntimeFilePlan:
    """Merge runtime file documents and derive safe target paths."""
    merged_items = merge_runtime_file_items(documents)
    diagnostics: list[Diagnostic] = []
    items: list[RuntimeFilePlanItem] = []
    root = Path(comfyui_path)

    for index, item in enumerate(merged_items):
        path: RuntimeFilePath = ("files", index)
        try:
            config = _RuntimeFileConfig.model_validate(item)
        except ValidationError as error:
            diagnostics.extend(_diagnostics_from_validation_error(error, path))
            continue

        if not _validate_runtime_file_url(config.url, (*path, "url"), diagnostics):
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
                download_mode=config.download_mode,
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
) -> tuple[RuntimeFileDownloadResult, ...]:
    """Transfer runtime files to cdh-owned staging targets."""
    settings = runtime_downloader_settings(config)
    results: list[RuntimeFileDownloadResult] = []

    for index, item in enumerate(plan.items, 1):
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

        staging_item = _runtime_staging_download_item(item, backend_name)
        _prepare_staging_parent(staging_item.target.parent, ("files", index - 1))

        log(f"Downloading runtime file {index}/{len(plan.items)} with {backend_name}")
        try:
            _download_runtime_file_with_policy(
                item,
                staging_item,
                backend,
                settings,
                ("files", index - 1),
                config=config,
                log=log,
            )
            _place_staged_runtime_file(item, staging_item.target, ("files", index - 1))
        except _RuntimeDownloadContinued:
            _cleanup_current_staging(staging_item.target)
            continue
        except Exception:
            _cleanup_current_staging(staging_item.target)
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


def download_runtime_files(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    httpx_downloader: DownloadBackend | None = None,
    aria2_downloader_factory: Aria2DownloaderFactory = Aria2Downloader,
    log: Logger = print,
) -> tuple[RuntimeFileDownloadResult, ...]:
    """Download runtime file plan items through existing backend adapters."""
    httpx_backend = httpx_downloader or HttpxDownloader(log=log)
    backends: dict[str, DownloadBackend] = {"httpx": httpx_backend}

    if not _requires_aria2_backend(plan, config):
        return process_runtime_file_downloads(
            plan,
            config=config,
            backends=backends,
            log=log,
        )

    with aria2_downloader_factory(log=log) as aria2_backend:
        backends["aria2"] = aria2_backend
        return process_runtime_file_downloads(
            plan,
            config=config,
            backends=backends,
            log=log,
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
            retries=downloader.httpx.retries,
        ),
    )


class _RuntimeDownloadContinued(Exception):
    """Internal marker for exhausted transfer failures handled by continue."""


def _download_runtime_file_with_policy(
    item: RuntimeFilePlanItem,
    staging_item: FileDownloadItem,
    backend: DownloadBackend,
    settings: DownloaderSettings,
    path: RuntimeFilePath,
    *,
    config: RuntimeConfig,
    log: Logger,
) -> None:
    attempt_settings = _single_runtime_attempt_settings(settings)
    attempts = config.cdh.download_max_attempts
    for attempt in range(1, attempts + 1):
        try:
            backend.download(staging_item, attempt_settings)
            return
        except TransferDownloadFilesError as error:
            if attempt < attempts:
                log(
                    "Retrying runtime file download after attempt "
                    f"{attempt}/{attempts} failed: {item.target}: {error}"
                )
                continue
            if config.cdh.download_failure_policy == "continue":
                log(
                    "WARNING: runtime file download failed after "
                    f"{attempts} attempt(s), continuing: {item.target}: {error}"
                )
                raise _RuntimeDownloadContinued from error
            raise RuntimeFileDownloadError(
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


def merge_runtime_file_items(
    documents: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Merge runtime file arrays by normalized relative target path."""
    merged: list[dict[str, Any]] = []
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

        for item in parsed.files:
            path: RuntimeFilePath = ("files", len(merged))
            item_document = item.model_dump(mode="json", exclude_none=True)
            has_valid_url = _validate_runtime_file_url(
                item.url,
                (*path, "url"),
                diagnostics,
            )
            key = _merge_key(item, path, diagnostics)
            if key is None or not has_valid_url:
                continue
            if key in indexes:
                merged[indexes[key]] = {**merged[indexes[key]], **item_document}
            else:
                indexes[key] = len(merged)
                merged.append(item_document)

    if diagnostics:
        raise RuntimeFilePlanError(tuple(diagnostics))
    return tuple(merged)


def _validate_runtime_file_url(
    value: str | None,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> bool:
    if value is None or is_http_url(value):
        return True
    diagnostics.append(
        Diagnostic(
            path,
            "runtime_file.invalid_url",
            "must be an HTTP(S) URL with a host",
        )
    )
    return False


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
) -> FileDownloadItem:
    return FileDownloadItem(
        url=item.url,
        directory=item.directory,
        filename=item.filename,
        target=_runtime_staging_target(item),
        overwrite=False,
        downloader=downloader,
    )


def _runtime_staging_target(item: RuntimeFilePlanItem) -> Path:
    return item.target.parent / ".cdh-staging" / f"{item.target.name}.cdh-download"


def _prepare_staging_parent(
    staging_parent: Path,
    path: RuntimeFilePath,
) -> None:
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
    _cleanup_stale_staging_files(staging_parent)


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


def _cleanup_stale_staging_files(staging_parent: Path) -> None:
    if not _is_real_directory(staging_parent):
        return

    try:
        entries = tuple(staging_parent.iterdir())
    except FileNotFoundError:
        return
    except OSError:
        return

    for entry in entries:
        if not _is_cdh_staging_artifact(entry):
            continue
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
        except OSError:
            pass


def _cleanup_current_staging(staging_target: Path) -> None:
    if not _is_real_directory(staging_target.parent):
        return

    for path in (
        staging_target,
        staging_target.with_name(f"{staging_target.name}.tmp"),
        Path(f"{staging_target}.aria2"),
    ):
        if not _is_cdh_staging_artifact(path):
            continue
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
        except OSError:
            pass


def _is_cdh_staging_artifact(path: Path) -> bool:
    if path.parent.name != ".cdh-staging":
        return False
    return (
        path.name.endswith(".cdh-download")
        or path.name.endswith(".cdh-download.tmp")
        or path.name.endswith(".cdh-download.aria2")
    )


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
    normalized = _normalize_runtime_file_path(item, path, diagnostics)
    if normalized is None:
        return None
    return normalized[1]


def _normalize_runtime_file_path(
    item: _RuntimeFilePatch | _RuntimeFileConfig,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> tuple[PurePosixPath, str] | None:
    directory = _normalize_directory(item.dir, (*path, "dir"), diagnostics)
    filename = _normalize_filename(item.filename, (*path, "filename"), diagnostics)
    if directory is None or filename is None:
        return None
    relative_target = (directory / filename).as_posix()
    return directory, relative_target


def _normalize_directory(
    value: str,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> PurePosixPath | None:
    result = validate_relative_file_directory(value)
    if result.path is not None:
        return result.path
    diagnostics.append(
        Diagnostic(
            path,
            f"runtime_file.{result.code}",
            result.message or "must be a valid relative directory",
        )
    )
    return None


def _normalize_filename(
    value: str,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> str | None:
    result = validate_file_name(value)
    if result.filename is None:
        diagnostics.append(
            Diagnostic(
                path,
                "runtime_file.invalid_filename",
                "must be one nonempty filename component",
            )
        )
        return None
    return result.filename


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
