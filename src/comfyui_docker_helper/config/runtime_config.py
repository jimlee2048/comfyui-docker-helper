"""Runtime configuration loading and merge for container startup."""

import os
import posixpath
import re
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticComparison,
    DiagnosticComparisonSite,
    DiagnosticPath,
    DiagnosticSeverity,
    DiagnosticSourceContext,
    SourceLocation,
    SourceReference,
)
from comfyui_docker_helper.config.downloader_credentials import (
    DownloaderCredentialContext,
    DownloaderCredentialContextError,
    canonicalize_downloader_credential_context,
    parse_downloader_credential_context,
    parse_downloader_request_url,
    select_downloader_credential_context,
)
from comfyui_docker_helper.config.file_checksum import normalize_file_checksum
from comfyui_docker_helper.config.final_models import (
    FinalDownloaderCredentialConfig,
    FinalSecretSourceConfig,
)
from comfyui_docker_helper.config.merge import (
    ANY_PATH_PART,
    AtomicPolicy,
    KeyedItemMerge,
    KeyedSequencePolicy,
    MergePolicyRegistry,
    OriginNode,
    PolicyRule,
    SourceDocument,
    merge_toml_documents,
)
from comfyui_docker_helper.config.model_base import ConfigModel
from comfyui_docker_helper.config.runtime_file_validation import (
    normalize_runtime_file_path,
    runtime_file_item_merge,
    runtime_file_target_identity,
    validate_runtime_file_url,
)
from comfyui_docker_helper.config.runtime_models import RuntimeConfig
from comfyui_docker_helper.config.shutdown_timeout import ShutdownTimeout
from comfyui_docker_helper.config.ssh_keys import (
    normalize_ssh_public_key,
    normalize_ssh_public_keys,
)
from comfyui_docker_helper.config.url_validation import DownloaderName
from comfyui_docker_helper.config.value_validation import is_argv_value


def _runtime_downloader_credential_key(item: Any) -> tuple[str, ...] | None:
    if not isinstance(item, Mapping):
        return None
    match = item.get("match")
    if not isinstance(match, str):
        return None
    try:
        canonical = canonicalize_downloader_credential_context(match)
    except DownloaderCredentialContextError:
        return None
    return ("downloader-credential", canonical)


BAKED_RUNTIME_CONFIG_PATH = Path("/opt/cdh/runtime/config.toml")
MOUNTED_RUNTIME_CONFIG_PATH = Path("/etc/cdh/runtime/config.toml")
_RUNTIME_CONFIG_MERGE_POLICIES = MergePolicyRegistry(
    (
        PolicyRule(("secrets", ANY_PATH_PART), AtomicPolicy()),
        PolicyRule(
            ("cdh", "downloader", "credentials"),
            KeyedSequencePolicy(
                _runtime_downloader_credential_key,
                KeyedItemMerge.ATOMIC,
            ),
        ),
        PolicyRule(
            ("files",),
            KeyedSequencePolicy(
                runtime_file_target_identity,
                runtime_file_item_merge,
            ),
        ),
    )
)

type RuntimeConfigPath = str | Path
type RuntimePath = tuple[str, ...]
type RuntimeFilePath = tuple[str | int, ...]

_HOST_ONLY_ROOT_SECTIONS = frozenset({"compute_platform", "python", "pytorch", "build"})
_HOST_ONLY_CDH_FIELDS = frozenset({"local_file_mode"})
_HOST_ONLY_SYSTEM_FIELDS = frozenset(
    {"workspace", "comfyui_path", "extra_packages", "env"}
)
_HOST_ONLY_COMFYUI_FIELDS = frozenset(
    {"version", "install_cli", "install_manager", "custom_nodes"}
)
_COMFYUI_CONTROLLED_STARTUP_FLAGS = frozenset(
    {"--listen", "--port", "--auto-launch", "--disable-auto-launch"}
)
_SECRET_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class RuntimeConfigurationResult:
    """Merged runtime config plus non-fatal cross-context diagnostics."""

    config: RuntimeConfig
    files: tuple[dict[str, Any], ...] = ()
    warnings: tuple[Diagnostic, ...] = ()


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


class _RuntimeDownloaderConfigPatch(ConfigModel):
    aria2: _RuntimeAria2ConfigPatch | None = None
    httpx: _RuntimeHttpxConfigPatch | None = None
    credentials: list[FinalDownloaderCredentialConfig] | None = None


class _RuntimeCdhConfigPatch(ConfigModel):
    default_downloader: DownloaderName | None = None
    default_download_mode: Literal["sync", "async"] | None = None
    download_max_attempts: int | None = Field(default=None, ge=1)
    download_failure_policy: Literal["continue", "fail"] | None = None
    shutdown_timeout: ShutdownTimeout | None = None
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
    type: Literal["http"]
    url: str | None = None
    target_dir: str
    filename: str
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


class _RuntimeConfigPatch(ConfigModel):
    comfyui: _RuntimeComfyUIConfigPatch | None = None
    cdh: _RuntimeCdhConfigPatch | None = None
    system: _RuntimeSystemConfigPatch | None = None
    files: list[_RuntimeFilePatch] | None = None
    secrets: dict[str, FinalSecretSourceConfig] | None = None


def load_runtime_config(
    *,
    baked_config_path: RuntimeConfigPath = BAKED_RUNTIME_CONFIG_PATH,
    mounted_config_path: RuntimeConfigPath = MOUNTED_RUNTIME_CONFIG_PATH,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfigurationResult:
    """Load and merge code defaults, baked runtime config, and mounted config."""
    warnings: list[Diagnostic] = []
    documents: list[SourceDocument] = [
        SourceDocument(SourceReference(0, "defaults"), _runtime_defaults_document())
    ]

    for config_path in (
        Path(baked_config_path),
        Path(mounted_config_path),
    ):
        if not config_path.exists():
            continue

        source = SourceReference(len(documents), str(config_path))
        raw_document = _read_runtime_toml(config_path, source)
        document, document_warnings = _prepare_runtime_document(
            raw_document,
            source,
        )
        warnings.extend(document_warnings)
        documents.append(SourceDocument(source, document))

    environment_source = SourceReference(len(documents), "environment")
    try:
        env_document, env_pub_key = _runtime_env_document(
            os.environ if environ is None else environ
        )
    except RuntimeConfigurationError as error:
        raise RuntimeConfigurationError(
            _attach_explicit_source(error.diagnostics, environment_source)
        ) from error
    if env_document:
        documents.append(SourceDocument(environment_source, env_document))

    merged = merge_toml_documents(
        documents,
        policies=_RUNTIME_CONFIG_MERGE_POLICIES,
    )
    effective = _validate_effective_runtime_document(merged.document, merged.origins)
    config = _runtime_config_from_effective(effective)
    _validate_runtime_ssh(config, merged.origins)
    if env_pub_key is not None:
        config.system.ssh.pub_keys.append(env_pub_key)
        env_normalization = normalize_ssh_public_keys(
            config.system.ssh.pub_keys,
            path=("system", "ssh", "pub_keys"),
            code="ssh.invalid_public_key",
        )
        config.system.ssh.pub_keys[:] = list(env_normalization.values)
    _validate_runtime_downloader(config, merged.origins)
    _validate_runtime_extra_args(config, merged.origins)
    files = _validate_effective_runtime_files(effective.files, merged.origins)
    credential_warnings = _validate_runtime_downloader_credentials(
        config,
        files,
        merged.origins,
    )
    warnings.extend(credential_warnings)
    return RuntimeConfigurationResult(
        config=config,
        files=files,
        warnings=tuple(warnings),
    )


def _runtime_defaults_document() -> dict[str, Any]:
    return RuntimeConfig().model_dump(mode="json")


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
    if "CDH_SHUTDOWN_TIMEOUT" in environ:
        document.setdefault("cdh", {})["shutdown_timeout"] = (
            _parse_env_shutdown_timeout(environ["CDH_SHUTDOWN_TIMEOUT"])
        )
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


def _parse_env_shutdown_timeout(value: str) -> int | float:
    normalized = value.strip()
    try:
        timeout = float(normalized)
    except ValueError as error:
        raise _invalid_env_shutdown_timeout() from error
    if not normalized or not isfinite(timeout) or (timeout != -1 and timeout <= 0):
        raise _invalid_env_shutdown_timeout()
    if timeout.is_integer():
        return int(timeout)
    return timeout


def _invalid_env_shutdown_timeout() -> RuntimeConfigurationError:
    return RuntimeConfigurationError(
        (
            Diagnostic(
                path=("env", "CDH_SHUTDOWN_TIMEOUT"),
                code="env.invalid_shutdown_timeout",
                message="must be a finite positive number or -1",
            ),
        )
    )


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


def _read_runtime_toml(
    config_path: Path,
    source: SourceReference,
) -> dict[str, Any]:
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
                    source_context=SourceLocation(source, ()),
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
                    source_context=SourceLocation(source, ()),
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
            (
                Diagnostic(
                    path=(),
                    code=code,
                    message=message,
                    source_context=SourceLocation(source, ()),
                ),
            )
        ) from error


def _prepare_runtime_document(
    document: Mapping[str, Any],
    source: SourceReference,
) -> tuple[dict[str, Any], tuple[Diagnostic, ...]]:
    diagnostics: list[Diagnostic] = []
    prepared: dict[str, Any] = {}

    for key, value in document.items():
        if key in _HOST_ONLY_ROOT_SECTIONS:
            diagnostics.append(_host_only_warning((key,), source))
            continue
        if key == "system" and isinstance(value, Mapping):
            prepared[key] = _prepare_system_document(value, diagnostics, source)
            continue
        if key == "cdh" and isinstance(value, Mapping):
            prepared[key] = _prepare_cdh_document(value, diagnostics, source)
            continue
        if key == "comfyui" and isinstance(value, Mapping):
            prepared[key] = _prepare_comfyui_document(value, diagnostics, source)
            continue
        prepared[key] = value

    return prepared, tuple(diagnostics)


def _prepare_cdh_document(
    document: Mapping[str, Any],
    diagnostics: list[Diagnostic],
    source: SourceReference,
) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for key, value in document.items():
        if key in _HOST_ONLY_CDH_FIELDS:
            diagnostics.append(_host_only_warning(("cdh", key), source))
            continue
        prepared[key] = value
    return prepared


def _prepare_system_document(
    document: Mapping[str, Any],
    diagnostics: list[Diagnostic],
    source: SourceReference,
) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for key, value in document.items():
        if key in _HOST_ONLY_SYSTEM_FIELDS:
            diagnostics.append(_host_only_warning(("system", key), source))
            continue
        prepared[key] = value
    return prepared


def _prepare_comfyui_document(
    document: Mapping[str, Any],
    diagnostics: list[Diagnostic],
    source: SourceReference,
) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for key, value in document.items():
        if key in _HOST_ONLY_COMFYUI_FIELDS:
            diagnostics.append(_host_only_warning(("comfyui", key), source))
            continue
        prepared[key] = value
    return prepared


def _host_only_warning(path: RuntimePath, source: SourceReference) -> Diagnostic:
    return Diagnostic(
        path=path,
        code="runtime.host_only_ignored",
        message="host-only configuration is ignored by the container runtime",
        severity=DiagnosticSeverity.WARNING,
        source_context=SourceLocation(source, path),
    )


def _validate_effective_runtime_document(
    document: Mapping[str, Any],
    origins: OriginNode,
) -> _RuntimeConfigPatch:
    try:
        return _RuntimeConfigPatch.model_validate(document)
    except ValidationError as error:
        diagnostics = _enrich_runtime_diagnostics(
            _diagnostics_from_validation_error(error),
            origins,
        )
        raise RuntimeConfigurationError(diagnostics) from error


def _runtime_config_from_effective(document: _RuntimeConfigPatch) -> RuntimeConfig:
    try:
        return RuntimeConfig.model_validate(
            document.model_dump(
                mode="json",
                exclude={"files"},
                exclude_none=True,
            )
        )
    except ValidationError as error:
        diagnostics = _diagnostics_from_validation_error(error)
        raise RuntimeConfigurationError(diagnostics) from error


def _validate_runtime_ssh(config: RuntimeConfig, origins: OriginNode) -> None:
    normalization = normalize_ssh_public_keys(
        config.system.ssh.pub_keys,
        path=("system", "ssh", "pub_keys"),
        code="ssh.invalid_public_key",
    )
    if normalization.diagnostics:
        raise RuntimeConfigurationError(
            _enrich_runtime_diagnostics(normalization.diagnostics, origins)
        )
    config.system.ssh.pub_keys[:] = list(normalization.values)


def _validate_effective_runtime_files(
    items: list[_RuntimeFilePatch] | None,
    origins: OriginNode,
) -> tuple[dict[str, Any], ...]:
    diagnostics: list[Diagnostic] = []
    documents: list[dict[str, Any]] = []
    established: dict[str, RuntimeFilePath] = {}
    for index, item in enumerate(items or ()):
        path: RuntimeFilePath = ("files", index)
        if item.url is None:
            diagnostics.append(
                Diagnostic(
                    (*path, "url"),
                    "schema.missing",
                    "Field required",
                )
            )
        else:
            validate_runtime_file_url(item.url, (*path, "url"), diagnostics)
        normalized = normalize_runtime_file_path(
            item.target_dir,
            item.filename,
            path,
            diagnostics,
        )
        if normalized is not None:
            target = normalized[1]
            earlier_path = established.get(target)
            if earlier_path is None:
                established[target] = path
            else:
                diagnostics.append(
                    Diagnostic(
                        (*path, "filename"),
                        "runtime_file.duplicate_target",
                        "runtime file targets must be unique",
                        source_context=_runtime_comparison(
                            (*earlier_path, "filename"),
                            (*path, "filename"),
                            origins,
                        ),
                    )
                )
        document = item.model_dump(mode="json", exclude_none=True)
        if normalized is not None:
            document["target_dir"] = normalized[0].as_posix()
        documents.append(document)

    if diagnostics:
        raise RuntimeConfigurationError(
            _enrich_runtime_diagnostics(tuple(diagnostics), origins)
        )
    return tuple(documents)


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


def _attach_explicit_source(
    diagnostics: tuple[Diagnostic, ...],
    source: SourceReference,
) -> tuple[Diagnostic, ...]:
    return tuple(
        diagnostic
        if diagnostic.source_context is not None
        else replace(
            diagnostic,
            source_context=SourceLocation(source, diagnostic.path),
        )
        for diagnostic in diagnostics
    )


def _enrich_runtime_diagnostics(
    diagnostics: tuple[Diagnostic, ...],
    origins: OriginNode,
) -> tuple[Diagnostic, ...]:
    enriched: list[Diagnostic] = []
    for diagnostic in diagnostics:
        if diagnostic.source_context is not None:
            enriched.append(diagnostic)
            continue
        location = origins.exact_location(diagnostic.path)
        if location is None and diagnostic.code == "schema.missing":
            location = origins.missing_field_location(diagnostic.path)
        enriched.append(
            diagnostic
            if location is None
            else replace(diagnostic, source_context=location)
        )
    return tuple(enriched)


def _runtime_comparison(
    earlier_path: RuntimeFilePath,
    later_path: RuntimeFilePath,
    origins: OriginNode,
) -> DiagnosticSourceContext | None:
    earlier = origins.exact_location(earlier_path)
    later = origins.exact_location(later_path)
    if earlier is None or later is None:
        return later or earlier
    return DiagnosticComparison(
        earlier=DiagnosticComparisonSite(earlier),
        later=DiagnosticComparisonSite(later),
    )


def _validate_runtime_downloader(
    config: RuntimeConfig,
    origins: OriginNode,
) -> None:
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
        raise RuntimeConfigurationError(
            _enrich_runtime_diagnostics(tuple(diagnostics), origins)
        )


def _validate_runtime_downloader_credentials(
    config: RuntimeConfig,
    files: tuple[dict[str, Any], ...],
    origins: OriginNode,
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    warnings: list[Diagnostic] = []
    contexts: list[DownloaderCredentialContext] = []
    canonical_matches: dict[str, RuntimePath] = {}

    for name, source in config.secrets.items():
        path: RuntimePath = ("secrets", name)
        if _SECRET_NAME_PATTERN.fullmatch(name) is None:
            diagnostics.append(
                Diagnostic(
                    path,
                    "secret.invalid_name",
                    "names must match [a-z][a-z0-9_-]{0,63}",
                )
            )
        if (source.env is None) == (source.file is None):
            diagnostics.append(
                Diagnostic(
                    path,
                    "secret.invalid_source",
                    "must define exactly one of env or file",
                )
            )
        if (
            source.env is not None
            and _ENVIRONMENT_NAME_PATTERN.fullmatch(source.env) is None
        ):
            diagnostics.append(
                Diagnostic(
                    (*path, "env"),
                    "secret.invalid_env",
                    "environment variable names must match [A-Za-z_][A-Za-z0-9_]*",
                )
            )
        if source.file is not None and not _is_valid_runtime_secret_file(source.file):
            diagnostics.append(
                Diagnostic(
                    (*path, "file"),
                    "secret.invalid_file",
                    "runtime Secret file locators must be absolute POSIX paths",
                )
            )

    for index, route in enumerate(config.cdh.downloader.credentials):
        path: RuntimePath = ("cdh", "downloader", "credentials", index)
        try:
            context = parse_downloader_credential_context(route.match)
        except DownloaderCredentialContextError as error:
            diagnostics.append(
                Diagnostic(
                    (*path, "match"), "downloader_credential.invalid_match", str(error)
                )
            )
        else:
            contexts.append(context)
            earlier = canonical_matches.get(context.canonical_url)
            if earlier is not None:
                diagnostics.append(
                    Diagnostic(
                        (*path, "match"),
                        "downloader_credential.duplicate_match",
                        "credential route matches must be unique after normalization",
                    )
                )
            else:
                canonical_matches[context.canonical_url] = path
            if context.scheme == "http":
                warnings.append(
                    Diagnostic(
                        (*path, "match"),
                        "downloader_credential.insecure_http",
                        "Bearer credentials sent over HTTP lack TLS transport "
                        "confidentiality",
                        DiagnosticSeverity.WARNING,
                    )
                )
        secret_name = route.token.secret
        if _SECRET_NAME_PATTERN.fullmatch(secret_name) is None:
            diagnostics.append(
                Diagnostic(
                    (*path, "token", "secret"),
                    "secret.invalid_reference",
                    "Secret references must match [a-z][a-z0-9_-]{0,63}",
                )
            )
        elif secret_name not in config.secrets:
            diagnostics.append(
                Diagnostic(
                    (*path, "token", "secret"),
                    "secret.unknown_reference",
                    "must reference a defined Secret",
                )
            )

    if len(contexts) == len(config.cdh.downloader.credentials):
        for index, item in enumerate(files):
            try:
                request = parse_downloader_request_url(item["url"])
            except (KeyError, DownloaderCredentialContextError):
                continue
            if select_downloader_credential_context(contexts, request) is None:
                continue
            effective = item.get("downloader") or config.cdh.default_downloader
            if effective == "aria2":
                diagnostics.append(
                    Diagnostic(
                        ("files", index, "downloader"),
                        "downloader_credential.httpx_required",
                        "For security, authenticated downloads must use HTTPX",
                        hint=(
                            "aria2 cannot enforce per-redirect credential scope; set "
                            'downloader = "httpx" for this file or change the '
                            "default downloader"
                        ),
                    )
                )

    if diagnostics:
        raise RuntimeConfigurationError(
            _enrich_runtime_diagnostics(tuple(diagnostics), origins)
        )
    return _enrich_runtime_diagnostics(tuple(warnings), origins)


def _is_valid_runtime_secret_file(value: str) -> bool:
    parsed = PurePosixPath(value)
    return (
        bool(value)
        and parsed.is_absolute()
        and not value.startswith("//")
        and not value.endswith("/")
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and posixpath.normpath(value).startswith("/")
    )


def _validate_runtime_extra_args(
    config: RuntimeConfig,
    origins: OriginNode,
) -> None:
    diagnostics: list[Diagnostic] = []
    if not is_argv_value(config.comfyui.listen):
        diagnostics.append(
            Diagnostic(
                path=("comfyui", "listen"),
                code="comfyui.invalid_listen",
                message="must be non-empty and must not contain control characters",
            )
        )
    for index, argument in enumerate(config.comfyui.extra_args):
        if not is_argv_value(argument):
            diagnostics.append(
                Diagnostic(
                    path=("comfyui", "extra_args", index),
                    code="comfyui.invalid_extra_arg",
                    message="must be non-empty and must not contain control characters",
                )
            )
            continue
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
        raise RuntimeConfigurationError(
            _enrich_runtime_diagnostics(tuple(diagnostics), origins)
        )


def _normalize_pydantic_location(location: tuple[Any, ...]) -> DiagnosticPath:
    return tuple(
        part if isinstance(part, (str, int)) else str(part) for part in location
    )
