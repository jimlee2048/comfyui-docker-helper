"""Public configuration loading and offline validation."""

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    DiagnosticSourceContext,
    SourceLocation,
    SourceReference,
)
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_validation import (
    FinalConfigDomainResult,
    FinalConfigError,
    validate_final_config_domains,
    validate_final_config_semantics,
    validate_final_config_structure,
)
from comfyui_docker_helper.config.host_merge_policies import (
    HOST_CONFIG_MERGE_POLICIES,
)
from comfyui_docker_helper.config.merge import (
    OriginNode,
    SourceDocument,
    merge_toml_documents,
)

type ConfigPath = str | Path


@dataclass(frozen=True, slots=True)
class ConfigurationResult:
    """One fully validated final config; no resolution or planning occurs here."""

    config: FinalConfig
    domains: FinalConfigDomainResult
    raw_document: dict[str, Any]
    origins: OriginNode
    secret_file_base: Path
    warnings: tuple[Diagnostic, ...] = ()


class ConfigurationServiceError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("configuration service errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("configuration is invalid")


def load_validate_config(
    config_path: ConfigPath | Sequence[ConfigPath],
    *,
    build_hooks_dir: str | Path | None = None,
) -> FinalConfig:
    return load_validate_config_result(
        config_path, build_hooks_dir=build_hooks_dir
    ).config


def load_validate_config_result(
    config_path: ConfigPath | Sequence[ConfigPath],
    *,
    build_hooks_dir: str | Path | None = None,
) -> ConfigurationResult:
    """Load and validate locally without providers, Docker, lock I/O, or planning."""
    paths = _coerce_config_paths(config_path)
    documents: list[SourceDocument] = []
    for layer_ordinal, path in enumerate(paths):
        source = SourceReference(layer_ordinal, str(path))
        documents.append(
            SourceDocument(
                source,
                _read_toml(path, source=source),
            )
        )
    merged = merge_toml_documents(
        documents,
        policies=HOST_CONFIG_MERGE_POLICIES,
    )
    document = merged.document
    secret_file_base = _resolve_config_parent(paths[0], source=documents[0].source)
    try:
        config = validate_final_config_structure(document)
    except FinalConfigError as error:
        raise ConfigurationServiceError(
            _enrich_diagnostics(error.diagnostics, merged.origins)
        ) from error
    domains = validate_final_config_domains(config, build_hooks_dir=build_hooks_dir)
    diagnostics = (
        *domains.diagnostics,
        *validate_final_config_semantics(config, domains, origins=merged.origins),
    )
    diagnostics = _enrich_diagnostics(diagnostics, merged.origins)
    errors = tuple(
        item for item in diagnostics if item.severity == DiagnosticSeverity.ERROR
    )
    warnings = tuple(
        item for item in diagnostics if item.severity == DiagnosticSeverity.WARNING
    )
    if errors:
        raise ConfigurationServiceError(errors)
    return ConfigurationResult(
        config=config,
        domains=domains,
        raw_document=document,
        origins=merged.origins,
        secret_file_base=secret_file_base,
        warnings=warnings,
    )


def _coerce_config_paths(
    config_path: ConfigPath | Sequence[ConfigPath],
) -> tuple[ConfigPath, ...]:
    if isinstance(config_path, (str, Path)):
        return (config_path,)
    paths = tuple(config_path)
    if not paths:
        raise ConfigurationServiceError(
            (Diagnostic((), "config.file_required", "at least one file is required"),)
        )
    return paths


def _enrich_diagnostics(
    diagnostics: tuple[Diagnostic, ...],
    origins: OriginNode,
) -> tuple[Diagnostic, ...]:
    return tuple(_enrich_diagnostic(item, origins) for item in diagnostics)


def _enrich_diagnostic(diagnostic: Diagnostic, origins: OriginNode) -> Diagnostic:
    if diagnostic.source_context is not None:
        return diagnostic
    source_context: DiagnosticSourceContext | None = origins.exact_location(
        diagnostic.path
    )
    if source_context is None and diagnostic.code == "schema.missing":
        source_context = origins.missing_field_location(diagnostic.path)
    if source_context is None:
        return diagnostic
    return replace(diagnostic, source_context=source_context)


def _read_toml(
    config_path: ConfigPath,
    *,
    source: SourceReference,
) -> dict[str, Any]:
    path = Path(config_path)
    try:
        with path.open("rb") as config_file:
            return tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise _read_error("toml.invalid_document", str(error), source) from error
    except UnicodeDecodeError as error:
        raise _read_error(
            "toml.invalid_encoding",
            "configuration file must be valid UTF-8",
            source,
        ) from error
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            code, message = "config.file_not_found", "configuration file does not exist"
        elif isinstance(error, IsADirectoryError):
            code, message = "config.not_a_file", "configuration path must be a file"
        elif isinstance(error, PermissionError):
            code, message = (
                "config.permission_denied",
                "configuration file cannot be read",
            )
        else:
            code, message = "config.read_failed", "configuration file could not be read"
        raise _read_error(code, message, source) from error


def _resolve_config_parent(
    config_path: ConfigPath,
    *,
    source: SourceReference,
) -> Path:
    path = Path(config_path)
    try:
        return path.resolve(strict=True).parent
    except OSError as error:
        raise _read_error(
            "config.read_failed",
            "configuration file parent could not be resolved",
            source,
        ) from error


def _read_error(
    code: str,
    message: str,
    source: SourceReference,
) -> ConfigurationServiceError:
    return ConfigurationServiceError(
        (
            Diagnostic(
                (),
                code,
                message,
                source_context=SourceLocation(source, ()),
            ),
        )
    )
