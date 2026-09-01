"""Runtime file plan construction and transfer/state identity projection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import ValidationError, field_validator

from comfyui_docker_helper.config import Diagnostic
from comfyui_docker_helper.config.file_checksum import normalize_file_checksum
from comfyui_docker_helper.config.model_base import ConfigModel
from comfyui_docker_helper.config.validation.runtime_files import (
    normalize_runtime_file_target,
    relative_file_targets_overlap,
    validate_runtime_file_url,
)
from comfyui_docker_helper.config.validation.urls import (
    DownloaderName,
    is_reserved_file_target_component,
    reserved_file_target_component_message,
)
from comfyui_docker_helper.container.runtime.files.models import (
    RuntimeFilePath,
    RuntimeFilePlan,
    RuntimeFilePlanError,
    RuntimeFilePlanItem,
)
from comfyui_docker_helper.container.runtime.state import (
    RuntimeDownloadDigestKey,
    RuntimeStateError,
    runtime_download_desired_identity_digest,
)
from comfyui_docker_helper.container.transfer.core import (
    TransferIdentity,
    project_transfer_identity,
)


class _RuntimeFileConfig(ConfigModel):
    type: Literal["http"]
    url: str
    target: str
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
    files: Iterable[Mapping[str, Any]],
    *,
    comfyui_path: str | Path,
    default_download_mode: Literal["sync", "async"] = "sync",
) -> RuntimeFilePlan:
    """Validate final merged runtime file items and derive safe target paths."""
    diagnostics: list[Diagnostic] = []
    items: list[RuntimeFilePlanItem] = []
    established: list[tuple[str, RuntimeFilePath]] = []
    root = Path(comfyui_path)
    root_parts = PurePosixPath(root.as_posix()).parts

    for source_index, item in enumerate(files):
        path: RuntimeFilePath = ("files", source_index)
        try:
            config = _RuntimeFileConfig.model_validate(item)
        except ValidationError as error:
            diagnostics.extend(_diagnostics_from_validation_error(error, path))
            continue

        if not validate_runtime_file_url(config.url, (*path, "url"), diagnostics):
            continue

        normalized = _normalize_runtime_file_target(config, path, diagnostics)
        if normalized is None:
            continue

        relative_target = normalized.as_posix()
        reserved_component = next(
            (part for part in root_parts if is_reserved_file_target_component(part)),
            None,
        )
        if reserved_component is not None:
            diagnostics.append(
                Diagnostic(
                    (*path, "target"),
                    "runtime_file.reserved_target_component",
                    reserved_file_target_component_message(reserved_component),
                )
            )
            continue
        for earlier_target, _earlier_path in established:
            if not relative_file_targets_overlap(earlier_target, relative_target):
                continue
            diagnostics.append(
                Diagnostic(
                    (*path, "target"),
                    "runtime_file.overlapping_target",
                    "runtime file targets must not equal or overlap "
                    "another target region",
                )
            )
            break
        established.append((relative_target, path))
        target = root.joinpath(*normalized.parts)
        items.append(
            RuntimeFilePlanItem(
                url=config.url,
                directory=normalized.parent.as_posix(),
                filename=normalized.name,
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
        url=item.url,
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


def _runtime_item_root(item: RuntimeFilePlanItem) -> Path:
    relative_parts = PurePosixPath(item.relative_target).parts
    return item.target.parents[len(relative_parts) - 1]


def _normalize_runtime_file_target(
    item: _RuntimeFileConfig,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> PurePosixPath | None:
    return normalize_runtime_file_target(
        item.target,
        (*path, "target"),
        diagnostics,
    )


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
