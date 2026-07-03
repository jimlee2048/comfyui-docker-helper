"""Internal runtime file download planning for future entrypoint phases."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import ValidationError

from comfyui_docker_helper.config import Diagnostic
from comfyui_docker_helper.config.models import ConfigModel

type RuntimeFilePath = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class RuntimeFilePlanItem:
    """One normalized runtime file target for later download execution."""

    url: str
    directory: str
    filename: str
    relative_target: str
    target: Path
    overwrite: bool
    download_mode: Literal["sync"]
    downloader: Literal["aria2", "httpx"] | None
    action: Literal["download", "skip_existing", "overwrite_existing"]


@dataclass(frozen=True, slots=True)
class RuntimeFilePlan:
    """Ordered normalized runtime file downloads."""

    items: tuple[RuntimeFilePlanItem, ...]


class RuntimeFilePlanError(ValueError):
    """Runtime file planning failure represented by stable diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("runtime file plan errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("runtime file plan is invalid")


class _RuntimeFilePatch(ConfigModel):
    dir: str
    filename: str
    url: str | None = None
    overwrite: bool | None = None
    downloader: Literal["aria2", "httpx"] | None = None
    download_mode: Literal["sync"] | None = None


class _RuntimeFilesDocument(ConfigModel):
    files: list[_RuntimeFilePatch] | None = None


class _RuntimeFileConfig(ConfigModel):
    dir: str
    filename: str
    url: str
    overwrite: bool = False
    downloader: Literal["aria2", "httpx"] | None = None
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
            item_document = item.model_dump(mode="json", exclude_none=True)
            key = _merge_key(item, ("files", len(merged)), diagnostics)
            if key is None:
                continue
            if key in indexes:
                merged[indexes[key]] = {**merged[indexes[key]], **item_document}
            else:
                indexes[key] = len(merged)
                merged.append(item_document)

    if diagnostics:
        raise RuntimeFilePlanError(tuple(diagnostics))
    return tuple(merged)


def _files_only_document(document: Mapping[str, Any]) -> dict[str, Any]:
    if "files" not in document:
        return {}
    return {"files": document["files"]}


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
    if value.startswith("/"):
        diagnostics.append(
            Diagnostic(path, "runtime_file.absolute_directory", "must be relative")
        )
        return None
    if value.endswith("/"):
        diagnostics.append(
            Diagnostic(
                path,
                "runtime_file.trailing_slash",
                "must not end with a slash",
            )
        )
        return None

    parts = value.split("/")
    if not value or any(part == "" for part in parts):
        diagnostics.append(
            Diagnostic(
                path,
                "runtime_file.empty_directory_segment",
                "must not contain empty path segments",
            )
        )
        return None
    if any(part == "." for part in parts):
        diagnostics.append(
            Diagnostic(
                path,
                "runtime_file.current_directory_segment",
                "must not contain '.'",
            )
        )
        return None
    if any(part == ".." for part in parts):
        diagnostics.append(
            Diagnostic(
                path,
                "runtime_file.parent_directory_segment",
                "must not contain '..'",
            )
        )
        return None

    normalized = PurePosixPath(os.path.normpath(value))
    if normalized == PurePosixPath("."):
        diagnostics.append(
            Diagnostic(path, "runtime_file.empty_directory", "must not be empty")
        )
        return None
    return normalized


def _normalize_filename(
    value: str,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> str | None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        diagnostics.append(
            Diagnostic(
                path,
                "runtime_file.invalid_filename",
                "must be one nonempty filename component",
            )
        )
        return None
    return value


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
