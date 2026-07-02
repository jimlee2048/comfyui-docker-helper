"""Reusable configuration loading, validation, and planning service."""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticPath,
    DiagnosticSeverity,
)
from comfyui_docker_helper.config.merge import merge_toml_documents
from comfyui_docker_helper.config.models import Config
from comfyui_docker_helper.config.plan import (
    RenderPlan,
    RenderPlanValidationError,
    build_render_plan,
)

_CUSTOM_NODE_BRANCHES = frozenset({"git", "registry"})
type ConfigPath = str | Path


@dataclass(frozen=True, slots=True)
class ConfigurationResult:
    """A validated render plan and non-fatal host-context diagnostics."""

    config: Config
    plan: RenderPlan
    warnings: tuple[Diagnostic, ...] = ()


class ConfigurationServiceError(ValueError):
    """A configuration failure represented by stable public diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("configuration service errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("configuration is invalid")


def load_validate_plan(
    config_path: ConfigPath | Sequence[ConfigPath],
    *,
    scripts_dir: str | Path = "./scripts",
) -> RenderPlan:
    """Read TOML file(s) and return the complete validated render plan."""
    return load_validate_plan_result(config_path, scripts_dir=scripts_dir).plan


def load_validate_plan_result(
    config_path: ConfigPath | Sequence[ConfigPath],
    *,
    scripts_dir: str | Path = "./scripts",
) -> ConfigurationResult:
    """Read TOML file(s), returning the render plan and non-fatal warnings."""
    paths = _coerce_config_paths(config_path)
    include_source = len(paths) > 1
    document = merge_toml_documents(
        _read_toml(path, include_source=include_source) for path in paths
    )
    config = _validate_structure(document)
    warnings = _validate_host_context(document)
    try:
        plan = build_render_plan(config, scripts_dir=scripts_dir)
    except RenderPlanValidationError as error:
        raise ConfigurationServiceError(error.diagnostics) from error
    return ConfigurationResult(config=config, plan=plan, warnings=warnings)


def _coerce_config_paths(
    config_path: ConfigPath | Sequence[ConfigPath],
) -> tuple[ConfigPath, ...]:
    if isinstance(config_path, (str, Path)):
        return (config_path,)

    paths = tuple(config_path)
    if not paths:
        raise ConfigurationServiceError(
            (
                Diagnostic(
                    path=(),
                    code="config.file_required",
                    message="at least one configuration file is required",
                ),
            )
        )
    return paths


def _read_toml(
    config_path: ConfigPath, *, include_source: bool = False
) -> dict[str, Any]:
    path = Path(config_path)
    try:
        with path.open("rb") as config_file:
            return tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationServiceError(
            (
                Diagnostic(
                    path=(),
                    code="toml.invalid_document",
                    message=_with_source(str(error), path, include_source),
                ),
            )
        ) from error
    except UnicodeDecodeError as error:
        raise ConfigurationServiceError(
            (
                Diagnostic(
                    path=(),
                    code="toml.invalid_encoding",
                    message=_with_source(
                        "configuration file must be valid UTF-8",
                        path,
                        include_source,
                    ),
                ),
            )
        ) from error
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            code = "config.file_not_found"
            message = "configuration file does not exist"
        elif isinstance(error, IsADirectoryError):
            code = "config.not_a_file"
            message = "configuration path must be a file"
        elif isinstance(error, PermissionError):
            code = "config.permission_denied"
            message = "configuration file cannot be read: permission denied"
        else:
            code = "config.read_failed"
            message = "configuration file could not be read"
        raise ConfigurationServiceError(
            (
                Diagnostic(
                    path=(),
                    code=code,
                    message=_with_source(message, path, include_source),
                ),
            )
        ) from error


def _validate_structure(document: Mapping[str, Any]) -> Config:
    try:
        return Config.model_validate(document)
    except ValidationError as error:
        diagnostics = tuple(
            Diagnostic(
                path=_normalize_pydantic_location(item["loc"]),
                code=f"schema.{item['type']}",
                message=item["msg"],
            )
            for item in error.errors(include_url=False, include_context=False)
        )
        raise ConfigurationServiceError(diagnostics) from error


def _validate_host_context(document: Mapping[str, Any]) -> tuple[Diagnostic, ...]:
    """Collect v0.2 host workflow warnings for runtime-only config fields."""
    diagnostics: list[Diagnostic] = []

    cdh = document.get("cdh")
    if isinstance(cdh, Mapping) and cdh.get("default_download_mode") == "sync":
        diagnostics.append(
            Diagnostic(
                path=("cdh", "default_download_mode"),
                code="host.runtime_download_mode_ignored",
                message=(
                    "sync download mode is runtime-only in v0.2 host workflows; "
                    "remove this field"
                ),
                severity=DiagnosticSeverity.WARNING,
            )
        )

    files = document.get("files")
    if isinstance(files, Sequence) and not isinstance(files, (str, bytes)):
        for index, item in enumerate(files):
            if isinstance(item, Mapping) and item.get("download_mode") == "sync":
                diagnostics.append(
                    Diagnostic(
                        path=("files", index, "download_mode"),
                        code="host.runtime_download_mode_ignored",
                        message=(
                            "sync download mode is runtime-only in v0.2 host "
                            "workflows; remove this field"
                        ),
                        severity=DiagnosticSeverity.WARNING,
                    )
                )

    return tuple(diagnostics)


def _with_source(message: str, path: Path, include_source: bool) -> str:
    if not include_source:
        return message
    return f"{message}: {path}"


def _normalize_pydantic_location(location: tuple[Any, ...]) -> DiagnosticPath:
    normalized: list[str | int] = []
    for index, part in enumerate(location):
        if (
            isinstance(part, str)
            and part in _CUSTOM_NODE_BRANCHES
            and index >= 2
            and isinstance(location[index - 1], int)
            and location[index - 2] == "custom_nodes"
        ):
            continue
        normalized.append(part if isinstance(part, (str, int)) else str(part))
    return tuple(normalized)
