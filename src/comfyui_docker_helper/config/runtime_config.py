"""Runtime configuration loading and merge for container startup."""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticPath,
    DiagnosticSeverity,
)
from comfyui_docker_helper.config.merge import merge_toml_documents
from comfyui_docker_helper.config.models import ConfigModel
from comfyui_docker_helper.config.runtime_projection import RuntimeConfig

BAKED_RUNTIME_CONFIG_PATH = Path("/opt/cdh/runtime/config.toml")
MOUNTED_RUNTIME_CONFIG_PATH = Path("/etc/cdh/runtime/config.toml")

type RuntimeConfigPath = str | Path
type RuntimePath = tuple[str, ...]

_HOST_ONLY_ROOT_SECTIONS = frozenset(
    {"compute_platform", "system", "python", "pytorch", "build"}
)
_HOST_ONLY_COMFYUI_FIELDS = frozenset(
    {"version", "cli_version", "install_manager", "custom_nodes"}
)
_COMFYUI_CONTROLLED_STARTUP_FLAGS = frozenset(
    {"--listen", "--port", "--auto-launch", "--disable-auto-launch"}
)


@dataclass(frozen=True, slots=True)
class RuntimeConfigurationResult:
    """Merged runtime config plus non-fatal cross-context diagnostics."""

    config: RuntimeConfig
    warnings: tuple[Diagnostic, ...] = ()
    explicit_paths: frozenset[RuntimePath] = frozenset()

    def is_explicit(self, path: RuntimePath) -> bool:
        """Return whether a mounted runtime config authored the path."""
        return path in self.explicit_paths


class RuntimeConfigurationError(ValueError):
    """Runtime configuration failure represented by stable diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("runtime configuration errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("runtime configuration is invalid")


class _RuntimeAria2ConfigPatch(ConfigModel):
    rpc_port: int | None = None
    split: int | None = None
    max_connection_per_server: int | None = None
    min_split_size: str | None = None
    resume_download: bool | None = None


class _RuntimeHttpxConfigPatch(ConfigModel):
    timeout: int | float | None = None
    retries: int | None = None


class _RuntimeDownloaderConfigPatch(ConfigModel):
    aria2: _RuntimeAria2ConfigPatch | None = None
    httpx: _RuntimeHttpxConfigPatch | None = None


class _RuntimeCdhConfigPatch(ConfigModel):
    default_downloader: Literal["aria2", "httpx"] | None = None
    default_download_mode: Literal["sync"] | None = None
    downloader: _RuntimeDownloaderConfigPatch | None = None


class _RuntimeComfyUIConfigPatch(ConfigModel):
    listen: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    extra_args: list[str] | None = None


class _RuntimeConfigPatch(ConfigModel):
    comfyui: _RuntimeComfyUIConfigPatch | None = None
    cdh: _RuntimeCdhConfigPatch | None = None


def load_runtime_config(
    *,
    baked_config_path: RuntimeConfigPath = BAKED_RUNTIME_CONFIG_PATH,
    mounted_config_path: RuntimeConfigPath = MOUNTED_RUNTIME_CONFIG_PATH,
) -> RuntimeConfigurationResult:
    """Load and merge code defaults, baked runtime config, and mounted config."""
    warnings: list[Diagnostic] = []
    documents: list[dict[str, Any]] = [_runtime_defaults_document()]
    explicit_paths: set[RuntimePath] = set()

    for source, config_path in (
        ("baked", Path(baked_config_path)),
        ("mounted", Path(mounted_config_path)),
    ):
        if not config_path.exists():
            continue

        raw_document = _read_runtime_toml(config_path)
        document, document_warnings = _prepare_runtime_document(raw_document)
        warnings.extend(document_warnings)
        _validate_runtime_patch(document)
        documents.append(document)
        if source == "mounted":
            explicit_paths.update(_runtime_explicit_paths(document))

    merged = merge_toml_documents(documents)
    config = _validate_effective_runtime_config(merged)
    _validate_runtime_downloader(config)
    _validate_runtime_extra_args(config)
    return RuntimeConfigurationResult(
        config=config,
        warnings=tuple(warnings),
        explicit_paths=frozenset(explicit_paths),
    )


def _runtime_defaults_document() -> dict[str, Any]:
    return RuntimeConfig().model_dump(mode="json")


def _read_runtime_toml(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("rb") as config_file:
            return tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise RuntimeConfigurationError(
            (
                Diagnostic(
                    path=(),
                    code="toml.invalid_document",
                    message=str(error),
                ),
            )
        ) from error
    except UnicodeDecodeError as error:
        raise RuntimeConfigurationError(
            (
                Diagnostic(
                    path=(),
                    code="toml.invalid_encoding",
                    message="runtime configuration file must be valid UTF-8",
                ),
            )
        ) from error
    except OSError as error:
        code = "runtime_config.read_failed"
        message = "runtime configuration file could not be read"
        if isinstance(error, IsADirectoryError):
            code = "runtime_config.not_a_file"
            message = "runtime configuration path must be a file"
        elif isinstance(error, PermissionError):
            code = "runtime_config.permission_denied"
            message = "runtime configuration file cannot be read: permission denied"
        raise RuntimeConfigurationError(
            (Diagnostic(path=(), code=code, message=message),)
        ) from error


def _prepare_runtime_document(
    document: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[Diagnostic, ...]]:
    diagnostics: list[Diagnostic] = []
    prepared: dict[str, Any] = {}

    for key, value in document.items():
        if key in _HOST_ONLY_ROOT_SECTIONS:
            diagnostics.append(_host_only_warning((key,)))
            continue
        if key == "files":
            raise RuntimeConfigurationError(
                (
                    Diagnostic(
                        path=("files",),
                        code="runtime.files_unsupported",
                        message=(
                            "runtime [[files]] entries are not supported until v0.3-M3"
                        ),
                    ),
                )
            )
        if key == "comfyui" and isinstance(value, Mapping):
            prepared[key] = _prepare_comfyui_document(value, diagnostics)
            continue
        prepared[key] = value

    return prepared, tuple(diagnostics)


def _prepare_comfyui_document(
    document: Mapping[str, Any],
    diagnostics: list[Diagnostic],
) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for key, value in document.items():
        if key in _HOST_ONLY_COMFYUI_FIELDS:
            diagnostics.append(_host_only_warning(("comfyui", key)))
            continue
        prepared[key] = value
    return prepared


def _host_only_warning(path: RuntimePath) -> Diagnostic:
    return Diagnostic(
        path=path,
        code="runtime.host_only_ignored",
        message="host-only configuration is ignored by the container runtime",
        severity=DiagnosticSeverity.WARNING,
    )


def _validate_runtime_patch(document: Mapping[str, Any]) -> None:
    try:
        _RuntimeConfigPatch.model_validate(document)
    except ValidationError as error:
        diagnostics = _diagnostics_from_validation_error(error)
        raise RuntimeConfigurationError(diagnostics) from error


def _validate_effective_runtime_config(document: Mapping[str, Any]) -> RuntimeConfig:
    try:
        return RuntimeConfig.model_validate(document)
    except ValidationError as error:
        diagnostics = _diagnostics_from_validation_error(error)
        raise RuntimeConfigurationError(diagnostics) from error


def _diagnostics_from_validation_error(
    error: ValidationError,
) -> tuple[Diagnostic, ...]:
    return tuple(
        Diagnostic(
            path=_normalize_pydantic_location(item["loc"]),
            code=f"schema.{item['type']}",
            message=item["msg"],
        )
        for item in error.errors(include_url=False, include_context=False)
    )


def _validate_runtime_downloader(config: RuntimeConfig) -> None:
    diagnostics: list[Diagnostic] = []
    aria2 = config.cdh.downloader.aria2
    httpx = config.cdh.downloader.httpx

    if httpx.timeout <= 0:
        diagnostics.append(
            Diagnostic(
                path=("cdh", "downloader", "httpx", "timeout"),
                code="cdh.downloader.httpx_timeout_not_positive",
                message="must be greater than 0",
            )
        )
    if httpx.retries < 0:
        diagnostics.append(
            Diagnostic(
                path=("cdh", "downloader", "httpx", "retries"),
                code="cdh.downloader.httpx_retries_negative",
                message="must be greater than or equal to 0",
            )
        )
    if not 1 <= aria2.rpc_port <= 65535:
        diagnostics.append(
            Diagnostic(
                path=("cdh", "downloader", "aria2", "rpc_port"),
                code="cdh.downloader.aria2_rpc_port_out_of_range",
                message="must be in range 1..65535",
            )
        )
    if aria2.split <= 0:
        diagnostics.append(
            Diagnostic(
                path=("cdh", "downloader", "aria2", "split"),
                code="cdh.downloader.aria2_split_not_positive",
                message="must be greater than 0",
            )
        )
    if aria2.max_connection_per_server <= 0:
        diagnostics.append(
            Diagnostic(
                path=("cdh", "downloader", "aria2", "max_connection_per_server"),
                code="cdh.downloader.aria2_max_connection_per_server_not_positive",
                message="must be greater than 0",
            )
        )

    if diagnostics:
        raise RuntimeConfigurationError(tuple(diagnostics))


def _validate_runtime_extra_args(config: RuntimeConfig) -> None:
    diagnostics: list[Diagnostic] = []
    for index, argument in enumerate(config.comfyui.extra_args):
        flag = argument.split("=", maxsplit=1)[0]
        if flag in _COMFYUI_CONTROLLED_STARTUP_FLAGS:
            diagnostics.append(
                Diagnostic(
                    path=("comfyui", "extra_args", index),
                    code="comfyui.controlled_extra_arg",
                    message=(
                        "must not include --listen, --port, --auto-launch, "
                        "or --disable-auto-launch because cdh controls these "
                        "startup flags"
                    ),
                )
            )
    if diagnostics:
        raise RuntimeConfigurationError(tuple(diagnostics))


def _runtime_explicit_paths(document: Mapping[str, Any]) -> frozenset[RuntimePath]:
    paths: set[RuntimePath] = set()
    _collect_runtime_paths(document, (), paths)
    return frozenset(paths)


def _collect_runtime_paths(
    value: Any,
    path: RuntimePath,
    paths: set[RuntimePath],
) -> None:
    if path:
        paths.add(path)
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                _collect_runtime_paths(item, (*path, key), paths)


def _normalize_pydantic_location(location: tuple[Any, ...]) -> DiagnosticPath:
    return tuple(
        part if isinstance(part, (str, int)) else str(part) for part in location
    )
