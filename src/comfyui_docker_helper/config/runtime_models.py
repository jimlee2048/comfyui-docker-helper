"""Strict runtime-only configuration models for container startup."""

from typing import Literal

from pydantic import Field

from comfyui_docker_helper.config.model_base import ConfigModel
from comfyui_docker_helper.config.shutdown_timeout import ShutdownTimeout
from comfyui_docker_helper.config.url_validation import DownloaderName


class RuntimeComfyUIConfig(ConfigModel):
    listen: str = "0.0.0.0"
    port: int = Field(default=8188, ge=1, le=65535)
    extra_args: list[str] = Field(default_factory=list)


class RuntimeAria2Config(ConfigModel):
    rpc_port: int = 6800
    split: int = 16
    max_connection_per_server: int = 16
    min_split_size: str = "1M"
    resume_download: bool = True


class RuntimeHttpxConfig(ConfigModel):
    timeout: int | float = 60


class RuntimeDownloaderConfig(ConfigModel):
    aria2: RuntimeAria2Config = Field(default_factory=RuntimeAria2Config)
    httpx: RuntimeHttpxConfig = Field(default_factory=RuntimeHttpxConfig)


class RuntimeCdhConfig(ConfigModel):
    default_downloader: DownloaderName = "aria2"
    default_download_mode: Literal["sync", "async"] = "sync"
    download_max_attempts: int = Field(default=3, ge=1)
    download_failure_policy: Literal["continue", "fail"] = "continue"
    shutdown_timeout: ShutdownTimeout = 8
    downloader: RuntimeDownloaderConfig = Field(default_factory=RuntimeDownloaderConfig)


class RuntimeSystemSshConfig(ConfigModel):
    enable: bool = False
    port: int = Field(default=22, ge=1, le=65535)
    password: str = ""
    pub_keys: list[str] = Field(default_factory=list)


class RuntimeSystemConfig(ConfigModel):
    ssh: RuntimeSystemSshConfig = Field(default_factory=RuntimeSystemSshConfig)


class RuntimeConfig(ConfigModel):
    """Runtime override schema independent of host planning configuration."""

    comfyui: RuntimeComfyUIConfig = Field(default_factory=RuntimeComfyUIConfig)
    cdh: RuntimeCdhConfig = Field(default_factory=RuntimeCdhConfig)
    system: RuntimeSystemConfig = Field(default_factory=RuntimeSystemConfig)
