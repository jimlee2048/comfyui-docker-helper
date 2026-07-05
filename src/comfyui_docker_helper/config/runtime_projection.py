"""Runtime-supported config projection for baked image defaults."""

from dataclasses import dataclass
from typing import Any, Literal

import tomli_w
from pydantic import Field

from comfyui_docker_helper.config.models import (
    CdhDownloaderConfig,
    Config,
    ConfigModel,
)
from comfyui_docker_helper.config.url_validation import (
    DownloaderName,
    require_downloader_name,
)

type ConfigPath = tuple[str | int, ...]


class RuntimeComfyUIConfig(ConfigModel):
    """ComfyUI startup fields supported by the runtime config."""

    listen: str = "0.0.0.0"
    port: int = Field(default=8188, ge=1, le=65535)
    extra_args: list[str] = Field(default_factory=list)


class RuntimeCdhConfig(ConfigModel):
    """cdh-owned runtime downloader defaults and backend settings."""

    default_downloader: DownloaderName = "aria2"
    default_download_mode: Literal["sync", "async"] = "sync"
    download_max_attempts: int = Field(default=3, ge=1)
    download_failure_policy: Literal["continue", "fail"] = "continue"
    downloader: CdhDownloaderConfig = Field(default_factory=CdhDownloaderConfig)


class RuntimeConfig(ConfigModel):
    """Strict private schema for fields baked into runtime/config.toml."""

    comfyui: RuntimeComfyUIConfig = Field(default_factory=RuntimeComfyUIConfig)
    cdh: RuntimeCdhConfig = Field(default_factory=RuntimeCdhConfig)


class RuntimeFileConfig(ConfigModel):
    """Runtime-supported file download defaults."""

    url: str
    dir: str
    filename: str
    overwrite: bool = False
    downloader: DownloaderName | None = None
    download_mode: Literal["sync", "async"] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfigProjection:
    """Validated runtime config bytes plus raw-document field provenance."""

    config: RuntimeConfig
    files: tuple[RuntimeFileConfig, ...]
    explicit_paths: frozenset[ConfigPath]

    def is_explicit(self, path: ConfigPath) -> bool:
        """Return whether a runtime-supported field was user-authored."""
        return path in self.explicit_paths

    def to_toml_bytes(self) -> bytes:
        """Serialize the effective runtime defaults deterministically."""
        return serialize_runtime_config_toml(
            self.config,
            files=self.files,
            explicit_paths=self.explicit_paths,
        )


def project_runtime_config(
    config: Config,
    raw_document: dict[str, Any],
) -> RuntimeConfigProjection:
    """Project the effective host config onto the runtime-supported schema."""
    document = {
        "comfyui": {
            "listen": config.comfyui.listen,
            "port": config.comfyui.port,
            "extra_args": list(config.comfyui.extra_args),
        },
        "cdh": {
            "default_downloader": require_downloader_name(
                config.cdh.default_downloader
            ),
            "default_download_mode": config.cdh.default_download_mode,
            "download_max_attempts": config.cdh.download_max_attempts,
            "downloader": config.cdh.downloader.model_dump(mode="json"),
        },
        "files": [
            {
                "url": file.url,
                "dir": file.dir,
                "filename": file.filename,
                "overwrite": file.overwrite,
                **(
                    {"downloader": require_downloader_name(file.downloader)}
                    if file.downloader
                    else {}
                ),
                **({"download_mode": file.download_mode} if file.download_mode else {}),
            }
            for file in config.files
        ],
    }
    files = tuple(RuntimeFileConfig.model_validate(item) for item in document["files"])
    explicit_paths = _runtime_explicit_paths(raw_document)
    runtime_document = {key: value for key, value in document.items() if key != "files"}
    if ("cdh", "download_failure_policy") in explicit_paths:
        runtime_document["cdh"]["download_failure_policy"] = (
            config.cdh.download_failure_policy
        )
    runtime_config = RuntimeConfig.model_validate(runtime_document)
    return RuntimeConfigProjection(
        config=runtime_config,
        files=files,
        explicit_paths=explicit_paths,
    )


def serialize_runtime_config_toml(
    config: RuntimeConfig,
    *,
    files: tuple[RuntimeFileConfig, ...] = (),
    explicit_paths: frozenset[ConfigPath] = frozenset(),
) -> bytes:
    """Serialize a runtime-supported config as deterministic TOML bytes."""
    document = config.model_dump(mode="json")
    if ("cdh", "download_failure_policy") not in explicit_paths:
        document["cdh"].pop("download_failure_policy", None)
    if files:
        document["files"] = [
            file.model_dump(mode="json", exclude_none=True) for file in files
        ]
    return tomli_w.dumps(document).encode("utf-8")


def _runtime_explicit_paths(raw_document: dict[str, Any]) -> frozenset[ConfigPath]:
    paths: set[ConfigPath] = set()
    _collect_explicit_runtime_paths(raw_document, (), paths)
    return frozenset(paths)


def _collect_explicit_runtime_paths(
    value: Any,
    path: ConfigPath,
    paths: set[ConfigPath],
) -> None:
    if not _is_runtime_supported_path(path):
        return
    if path:
        paths.add(path)
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                _collect_explicit_runtime_paths(item, (*path, key), paths)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _collect_explicit_runtime_paths(item, (*path, index), paths)


def _is_runtime_supported_path(path: ConfigPath) -> bool:
    if not path:
        return True
    if path[0] == "comfyui":
        return len(path) <= 2 and (len(path) == 1 or path[1] in _COMFYUI_FIELDS)
    if path[0] == "files":
        if len(path) == 1:
            return True
        if len(path) == 2:
            return isinstance(path[1], int)
        return len(path) == 3 and isinstance(path[1], int) and path[2] in _FILE_FIELDS
    if path[0] == "cdh":
        if len(path) == 1:
            return True
        if path[1] in {
            "default_downloader",
            "default_download_mode",
            "download_max_attempts",
            "download_failure_policy",
        }:
            return len(path) == 2
        return path[1] == "downloader"
    return False


_COMFYUI_FIELDS = frozenset({"listen", "port", "extra_args"})
_FILE_FIELDS = frozenset(
    {"url", "dir", "filename", "overwrite", "downloader", "download_mode"}
)
