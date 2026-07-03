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

type ConfigPath = tuple[str, ...]


class RuntimeComfyUIConfig(ConfigModel):
    """ComfyUI startup fields supported by the v0.3 runtime config."""

    listen: str = "0.0.0.0"
    port: int = Field(default=8188, ge=1, le=65535)
    extra_args: list[str] = Field(default_factory=list)


class RuntimeCdhConfig(ConfigModel):
    """cdh-owned runtime downloader defaults and backend settings."""

    default_downloader: Literal["aria2", "httpx"] = "aria2"
    default_download_mode: Literal["sync"] = "sync"
    downloader: CdhDownloaderConfig = Field(default_factory=CdhDownloaderConfig)


class RuntimeConfig(ConfigModel):
    """Strict private schema for fields baked into runtime/config.toml."""

    comfyui: RuntimeComfyUIConfig = Field(default_factory=RuntimeComfyUIConfig)
    cdh: RuntimeCdhConfig = Field(default_factory=RuntimeCdhConfig)


@dataclass(frozen=True, slots=True)
class RuntimeConfigProjection:
    """Validated runtime config bytes plus raw-document field provenance."""

    config: RuntimeConfig
    explicit_paths: frozenset[ConfigPath]

    def is_explicit(self, path: ConfigPath) -> bool:
        """Return whether a runtime-supported field was user-authored."""
        return path in self.explicit_paths

    def to_toml_bytes(self) -> bytes:
        """Serialize the effective runtime defaults deterministically."""
        return serialize_runtime_config_toml(self.config)


def project_runtime_config(
    config: Config,
    raw_document: dict[str, Any],
) -> RuntimeConfigProjection:
    """Project the effective host config onto the v0.3 runtime-supported schema."""
    document = {
        "comfyui": {
            "listen": config.comfyui.listen,
            "port": config.comfyui.port,
            "extra_args": list(config.comfyui.extra_args),
        },
        "cdh": {
            "default_downloader": config.cdh.default_downloader,
            "default_download_mode": config.cdh.default_download_mode,
            "downloader": config.cdh.downloader.model_dump(mode="json"),
        },
    }
    runtime_config = RuntimeConfig.model_validate(document)
    return RuntimeConfigProjection(
        config=runtime_config,
        explicit_paths=_runtime_explicit_paths(raw_document),
    )


def serialize_runtime_config_toml(config: RuntimeConfig) -> bytes:
    """Serialize a runtime-supported config as deterministic TOML bytes."""
    return tomli_w.dumps(config.model_dump(mode="json")).encode("utf-8")


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


def _is_runtime_supported_path(path: ConfigPath) -> bool:
    if not path:
        return True
    if path[0] == "comfyui":
        return len(path) <= 2 and (len(path) == 1 or path[1] in _COMFYUI_FIELDS)
    if path[0] != "cdh":
        return False
    if len(path) == 1:
        return True
    if path[1] in {"default_downloader", "default_download_mode"}:
        return len(path) == 2
    return path[1] == "downloader"


_COMFYUI_FIELDS = frozenset({"listen", "port", "extra_args"})
