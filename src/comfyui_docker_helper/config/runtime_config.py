"""Runtime configuration loading and merge for container startup."""

import os
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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
from comfyui_docker_helper.config.ssh_keys import (
    normalize_ssh_public_key,
    normalize_ssh_public_keys,
)
from comfyui_docker_helper.config.url_validation import DownloaderName, is_http_url

BAKED_RUNTIME_CONFIG_PATH = Path("/opt/cdh/runtime/config.toml")
MOUNTED_RUNTIME_CONFIG_PATH = Path("/etc/cdh/runtime/config.toml")

type RuntimeConfigPath = str | Path
type RuntimePath = tuple[str, ...]
type RuntimeFilePath = tuple[str | int, ...]

_HOST_ONLY_ROOT_SECTIONS = frozenset({"compute_platform", "python", "pytorch", "build"})
_HOST_ONLY_SYSTEM_FIELDS = frozenset(
    {"workspace", "comfyui_path", "extra_packages", "env"}
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
    files: tuple[dict[str, Any], ...] = ()
    file_documents: tuple[dict[str, Any], ...] = ()
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
    default_downloader: DownloaderName | None = None
    default_download_mode: Literal["sync", "async"] | None = None
    download_max_attempts: int | None = Field(default=None, ge=1)
    download_failure_policy: Literal["continue", "fail"] | None = None
    downloader: _RuntimeDownloaderConfigPatch | None = None


class _RuntimeComfyUIConfigPatch(ConfigModel):
    listen: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    extra_args: list[str] | None = None


class _RuntimeSystemSshConfigPatch(ConfigModel):
    enable: bool | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    password: str | None = None
    pub_keys: list[str] | None = None


class _RuntimeSystemConfigPatch(ConfigModel):
    ssh: _RuntimeSystemSshConfigPatch | None = None


class _RuntimeFilePatch(ConfigModel):
    url: str | None = None
    dir: str
    filename: str
    overwrite: bool | None = None
    downloader: DownloaderName | None = None
    download_mode: Literal["sync", "async"] | None = None


class _RuntimeConfigPatch(ConfigModel):
    comfyui: _RuntimeComfyUIConfigPatch | None = None
    cdh: _RuntimeCdhConfigPatch | None = None
    system: _RuntimeSystemConfigPatch | None = None
    files: list[_RuntimeFilePatch] | None = None


def load_runtime_config(
    *,
    baked_config_path: RuntimeConfigPath = BAKED_RUNTIME_CONFIG_PATH,
    mounted_config_path: RuntimeConfigPath = MOUNTED_RUNTIME_CONFIG_PATH,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfigurationResult:
    """Load and merge code defaults, baked runtime config, and mounted config."""
    warnings: list[Diagnostic] = []
    documents: list[dict[str, Any]] = [_runtime_defaults_document()]
    file_documents: list[dict[str, Any]] = []
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
        documents.append(_runtime_config_document(document))
        if "files" in document:
            file_documents.append({"files": document["files"]})
        if source == "mounted":
            explicit_paths.update(_runtime_explicit_paths(document))

    env_document, env_pub_key = _runtime_env_document(
        os.environ if environ is None else environ
    )
    if env_document:
        _validate_runtime_patch(env_document)
        documents.append(env_document)

    files = _merge_runtime_file_items(file_documents)
    merged = merge_toml_documents(documents)
    _normalize_merged_ssh_public_keys(merged)
    if env_pub_key is not None:
        ssh = merged.setdefault("system", {}).setdefault("ssh", {})
        pub_keys = ssh.setdefault("pub_keys", [])
        if env_pub_key not in pub_keys:
            pub_keys.append(env_pub_key)
    config = _validate_effective_runtime_config(merged)
    _validate_runtime_ssh(config)
    _validate_runtime_downloader(config)
    _validate_runtime_extra_args(config)
    return RuntimeConfigurationResult(
        config=config,
        files=files,
        file_documents=tuple(file_documents),
        warnings=tuple(warnings),
        explicit_paths=frozenset(explicit_paths),
    )


def _runtime_defaults_document() -> dict[str, Any]:
    return RuntimeConfig().model_dump(mode="json")


def _runtime_config_document(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "files"}


def _runtime_env_document(
    environ: Mapping[str, str],
) -> tuple[dict[str, Any], str | None]:
    document: dict[str, Any] = {}
    ssh_pub_key: str | None = None

    if "CDH_COMFYUI_LISTEN" in environ:
        document.setdefault("comfyui", {})["listen"] = environ["CDH_COMFYUI_LISTEN"]
    if "CDH_COMFYUI_PORT" in environ:
        document.setdefault("comfyui", {})["port"] = _parse_env_port(
            environ["CDH_COMFYUI_PORT"]
        )
    if "CDH_COMFYUI_EXTRA_ARGS" in environ:
        document.setdefault("comfyui", {})["extra_args"] = _parse_env_extra_args(
            environ["CDH_COMFYUI_EXTRA_ARGS"]
        )
    if "CDH_DEFAULT_DOWNLOADER" in environ:
        document.setdefault("cdh", {})["default_downloader"] = environ[
            "CDH_DEFAULT_DOWNLOADER"
        ]
    if "CDH_DEFAULT_DOWNLOAD_MODE" in environ:
        document.setdefault("cdh", {})["default_download_mode"] = environ[
            "CDH_DEFAULT_DOWNLOAD_MODE"
        ]
    if "CDH_DOWNLOAD_MAX_ATTEMPTS" in environ:
        document.setdefault("cdh", {})["download_max_attempts"] = (
            _parse_env_download_max_attempts(environ["CDH_DOWNLOAD_MAX_ATTEMPTS"])
        )
    if "CDH_DOWNLOAD_FAILURE_POLICY" in environ:
        document.setdefault("cdh", {})["download_failure_policy"] = environ[
            "CDH_DOWNLOAD_FAILURE_POLICY"
        ]
    if "SSH_ENABLE" in environ:
        document.setdefault("system", {}).setdefault("ssh", {})["enable"] = (
            _parse_env_ssh_enable(environ["SSH_ENABLE"])
        )
    if "SSH_PORT" in environ:
        document.setdefault("system", {}).setdefault("ssh", {})["port"] = (
            _parse_env_ssh_port(environ["SSH_PORT"])
        )
    if "SSH_PASSWORD" in environ:
        document.setdefault("system", {}).setdefault("ssh", {})["password"] = environ[
            "SSH_PASSWORD"
        ]
    if "SSH_PUB_KEY" in environ:
        ssh_pub_key, diagnostic = normalize_ssh_public_key(
            environ["SSH_PUB_KEY"],
            path=("env", "SSH_PUB_KEY"),
            code="env.invalid_ssh_pub_key",
        )
        if diagnostic is not None:
            raise RuntimeConfigurationError((diagnostic,))

    return document, ssh_pub_key


def _parse_env_ssh_enable(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise RuntimeConfigurationError(
        (
            Diagnostic(
                path=("env", "SSH_ENABLE"),
                code="env.invalid_ssh_enable",
                message=(
                    "must be true, false, 1, 0, yes, no, on, or off "
                    "after trimming whitespace"
                ),
            ),
        )
    )


def _parse_env_ssh_port(value: str) -> int:
    try:
        port = int(value.strip(), 10)
    except ValueError as error:
        raise RuntimeConfigurationError(
            (
                Diagnostic(
                    path=("env", "SSH_PORT"),
                    code="env.invalid_ssh_port",
                    message="must be an integer TCP port in range 1..65535",
                ),
            )
        ) from error
    if not 1 <= port <= 65535:
        raise RuntimeConfigurationError(
            (
                Diagnostic(
                    path=("env", "SSH_PORT"),
                    code="env.invalid_ssh_port",
                    message="must be an integer TCP port in range 1..65535",
                ),
            )
        )
    return port


def _parse_env_download_max_attempts(value: str) -> int:
    try:
        max_attempts = int(value, 10)
    except ValueError as error:
        raise RuntimeConfigurationError(
            (
                Diagnostic(
                    path=("env", "CDH_DOWNLOAD_MAX_ATTEMPTS"),
                    code="env.invalid_download_max_attempts",
                    message="must be a positive integer",
                ),
            )
        ) from error
    if max_attempts < 1:
        raise RuntimeConfigurationError(
            (
                Diagnostic(
                    path=("env", "CDH_DOWNLOAD_MAX_ATTEMPTS"),
                    code="env.invalid_download_max_attempts",
                    message="must be a positive integer",
                ),
            )
        )
    return max_attempts


def _parse_env_port(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError as error:
        raise RuntimeConfigurationError(
            (
                Diagnostic(
                    path=("env", "CDH_COMFYUI_PORT"),
                    code="env.invalid_port",
                    message="must be an integer TCP port in range 1..65535",
                ),
            )
        ) from error
    if not 1 <= port <= 65535:
        raise RuntimeConfigurationError(
            (
                Diagnostic(
                    path=("env", "CDH_COMFYUI_PORT"),
                    code="env.invalid_port",
                    message="must be an integer TCP port in range 1..65535",
                ),
            )
        )
    return port


def _parse_env_extra_args(value: str) -> list[str]:
    try:
        return shlex.split(value, posix=True)
    except ValueError as error:
        raise RuntimeConfigurationError(
            (
                Diagnostic(
                    path=("env", "CDH_COMFYUI_EXTRA_ARGS"),
                    code="env.invalid_extra_args",
                    message="must be valid POSIX shell-like arguments",
                ),
            )
        ) from error


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
        if key == "system" and isinstance(value, Mapping):
            prepared[key] = _prepare_system_document(value, diagnostics)
            continue
        if key == "comfyui" and isinstance(value, Mapping):
            prepared[key] = _prepare_comfyui_document(value, diagnostics)
            continue
        prepared[key] = value

    return prepared, tuple(diagnostics)


def _prepare_system_document(
    document: Mapping[str, Any],
    diagnostics: list[Diagnostic],
) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for key, value in document.items():
        if key in _HOST_ONLY_SYSTEM_FIELDS:
            diagnostics.append(_host_only_warning(("system", key)))
            continue
        prepared[key] = value
    return prepared


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


def _normalize_merged_ssh_public_keys(document: dict[str, Any]) -> None:
    system = document.get("system")
    if not isinstance(system, dict):
        return
    ssh = system.get("ssh")
    if not isinstance(ssh, dict) or "pub_keys" not in ssh:
        return
    pub_keys = ssh["pub_keys"]
    if not isinstance(pub_keys, list) or not all(
        isinstance(item, str) for item in pub_keys
    ):
        return
    normalized, diagnostics = normalize_ssh_public_keys(
        pub_keys,
        path=("system", "ssh", "pub_keys"),
        code="ssh.invalid_public_key",
    )
    if diagnostics:
        raise RuntimeConfigurationError(diagnostics)
    ssh["pub_keys"] = list(normalized)


def _validate_runtime_ssh(config: RuntimeConfig) -> None:
    normalized, diagnostics = normalize_ssh_public_keys(
        config.system.ssh.pub_keys,
        path=("system", "ssh", "pub_keys"),
        code="ssh.invalid_public_key",
    )
    if diagnostics:
        raise RuntimeConfigurationError(diagnostics)
    config.system.ssh.pub_keys[:] = list(normalized)


def _merge_runtime_file_items(
    documents: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    merged: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    diagnostics: list[Diagnostic] = []

    for document in documents:
        try:
            parsed = _RuntimeConfigPatch.model_validate(document)
        except ValidationError as error:
            diagnostics.extend(_diagnostics_from_validation_error(error))
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
            key = _runtime_file_merge_key(item, path, diagnostics)
            if key is None or not has_valid_url:
                continue
            if key in indexes:
                merged[indexes[key]] = {**merged[indexes[key]], **item_document}
            else:
                indexes[key] = len(merged)
                merged.append(item_document)

    if diagnostics:
        raise RuntimeConfigurationError(tuple(diagnostics))
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


def _runtime_file_merge_key(
    item: _RuntimeFilePatch,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> str | None:
    directory = _normalize_runtime_file_directory(item.dir, (*path, "dir"), diagnostics)
    filename = _normalize_runtime_file_filename(
        item.filename,
        (*path, "filename"),
        diagnostics,
    )
    if directory is None or filename is None:
        return None
    return (directory / filename).as_posix()


def _normalize_runtime_file_directory(
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


def _normalize_runtime_file_filename(
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
