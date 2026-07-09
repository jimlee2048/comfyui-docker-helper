"""Public Pydantic models for the TOML configuration contract."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ConfigModel(BaseModel):
    """Apply strict structural validation to every public config block."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class CudaConfig(ConfigModel):
    """CUDA image settings."""

    version: str
    image_flavor: str = "cudnn-devel"
    image_distro: str = "ubuntu24.04"


class ComputePlatformConfig(ConfigModel):
    """Compute platform backend configuration."""

    type: str
    cuda: CudaConfig


class SystemSshConfig(ConfigModel):
    """SSH runtime defaults accepted from host build configuration."""

    enable: bool = False
    port: int = Field(default=22, ge=1, le=65535)
    password: str = ""
    pub_keys: list[str] = Field(default_factory=list)


class SystemConfig(ConfigModel):
    """Container workspace, OS packages, environment, and SSH runtime defaults."""

    workspace: str = "/workspace"
    comfyui_path: str | None = None
    extra_packages: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    ssh: SystemSshConfig = Field(default_factory=SystemSshConfig)


class PythonConfig(ConfigModel):
    """Python and uv installation settings."""

    version: str = "3.12"
    uv_version: str = "latest"
    index_url: str = "https://pypi.org/simple"
    extra_packages: list[str] = Field(default_factory=list)


class PyTorchConfig(ConfigModel):
    """PyTorch installation settings."""

    version: str
    index_base_url: str = "https://download.pytorch.org/whl"
    extra_packages: list[str] = Field(default_factory=list)


class Aria2Config(ConfigModel):
    """aria2 downloader settings."""

    rpc_port: int = 6800
    split: int = 16
    max_connection_per_server: int = 16
    min_split_size: str = "1M"
    resume_download: bool = True


class HttpxConfig(ConfigModel):
    """HTTPX downloader settings."""

    timeout: int | float = 60
    retries: int = 3


class CdhDownloaderConfig(ConfigModel):
    """cdh-owned downloader backend settings."""

    aria2: Aria2Config = Field(default_factory=Aria2Config)
    httpx: HttpxConfig = Field(default_factory=HttpxConfig)


class CdhConfig(ConfigModel):
    """cdh-owned helper settings."""

    default_downloader: str = "aria2"
    default_download_mode: Literal["sync", "async"] = "sync"
    download_max_attempts: int = Field(default=3, ge=1)
    download_failure_policy: Literal["continue", "fail"] = "fail"
    downloader: CdhDownloaderConfig = Field(default_factory=CdhDownloaderConfig)


class BuildConfig(ConfigModel):
    """Docker build ergonomics."""

    tags: list[str] = Field(default_factory=list)
    output: Literal["load", "push"] = "load"


class _CustomNodeConfig(ConfigModel):
    """Settings shared by all custom-node sources."""

    pre_install_scripts: list[str] = Field(default_factory=list)
    post_install_scripts: list[str] = Field(default_factory=list)


class RegistryCustomNodeConfig(_CustomNodeConfig):
    """A custom node installed by registry ID."""

    type: Literal["registry"]
    id: str
    version: str | None = None


class GitCustomNodeConfig(_CustomNodeConfig):
    """A custom node installed from Git."""

    type: Literal["git"]
    url: str
    ref: str | None = None
    target_dir: str | None = None


CustomNodeConfig = Annotated[
    RegistryCustomNodeConfig | GitCustomNodeConfig,
    Field(discriminator="type"),
]


class ComfyUIConfig(ConfigModel):
    """ComfyUI, Manager, launch, and custom-node settings."""

    version: str
    cli_version: str = "latest"
    install_manager: bool = True
    listen: str = "0.0.0.0"
    port: int = Field(default=8188, ge=1, le=65535)
    extra_args: list[str] = Field(default_factory=list)
    custom_nodes: list[CustomNodeConfig] = Field(default_factory=list)


class FileConfig(ConfigModel):
    """A file to download into the ComfyUI tree."""

    url: str
    dir: str
    filename: str
    overwrite: bool = False
    downloader: str | None = None
    download_mode: Literal["sync", "async"] | None = None


class Config(ConfigModel):
    """Root public configuration loaded from one TOML document."""

    cdh: CdhConfig = Field(default_factory=CdhConfig)
    compute_platform: ComputePlatformConfig
    system: SystemConfig = Field(default_factory=SystemConfig)
    python: PythonConfig = Field(default_factory=PythonConfig)
    pytorch: PyTorchConfig
    build: BuildConfig = Field(default_factory=BuildConfig)
    comfyui: ComfyUIConfig
    files: list[FileConfig] = Field(default_factory=list)
