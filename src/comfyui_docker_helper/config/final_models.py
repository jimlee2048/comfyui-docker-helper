"""Strict public configuration models for the active Planning Authority."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from comfyui_docker_helper.exact_ledger import (
    DEFAULT_CUDA_IMAGE_DISTRO,
    DEFAULT_CUDA_IMAGE_FLAVOR,
    DEFAULT_MANAGED_PYTHON_VERSION,
    UV_VERSION,
)

CudaImageFlavor = Literal["base", "runtime", "devel", "cudnn-runtime", "cudnn-devel"]
CudaImageDistro = Literal["ubuntu22.04", "ubuntu24.04"]


class FinalConfigModel(BaseModel):
    """Apply strict structural validation to every final config block."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class FinalCudaConfig(FinalConfigModel):
    """CUDA version and independent NVIDIA image-packaging selectors."""

    version: str
    image_flavor: CudaImageFlavor = DEFAULT_CUDA_IMAGE_FLAVOR
    image_distro: CudaImageDistro = DEFAULT_CUDA_IMAGE_DISTRO


class FinalComputePlatformConfig(FinalConfigModel):
    """Typed compute backend selection."""

    type: Literal["cuda"]
    cuda: FinalCudaConfig


class FinalSystemSshConfig(FinalConfigModel):
    """SSH runtime defaults accepted from image configuration."""

    enable: bool = False
    port: int = Field(default=22, ge=1, le=65535)
    password: str = ""
    pub_keys: list[str] = Field(default_factory=list)


class FinalSystemConfig(FinalConfigModel):
    """Container paths, OS packages, environment, and SSH defaults."""

    workspace: str = "/workspace"
    comfyui_path: str | None = None
    extra_packages: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    ssh: FinalSystemSshConfig = Field(default_factory=FinalSystemSshConfig)


class FinalPythonConfig(FinalConfigModel):
    """Managed Python, application packages, and isolated CLI-tool requests."""

    version: str = DEFAULT_MANAGED_PYTHON_VERSION
    uv_version: str = UV_VERSION
    index_url: str = "https://pypi.org/simple"
    extra_packages: list[str] = Field(default_factory=list)
    uv_tools: list[str] = Field(default_factory=list)


class FinalPyTorchConfig(FinalConfigModel):
    """PyTorch group requests independent of backend-specific channel syntax."""

    version: str
    index_base_url: str = "https://download.pytorch.org/whl"
    extra_packages: list[str] = Field(default_factory=list)


class FinalAria2Config(FinalConfigModel):
    """Active aria2 downloader settings."""

    rpc_port: int = Field(default=6800, ge=1, le=65535)
    split: int = Field(default=16, ge=1)
    max_connection_per_server: int = Field(default=16, ge=1)
    min_split_size: str = "1M"
    resume_download: bool = True


class FinalHttpxConfig(FinalConfigModel):
    """HTTPX settings with the currently public retry count."""

    timeout: int | float = Field(default=60, gt=0)
    retries: int = Field(default=3, ge=0)


class FinalDownloaderConfig(FinalConfigModel):
    """Downloader-specific settings."""

    aria2: FinalAria2Config = Field(default_factory=FinalAria2Config)
    httpx: FinalHttpxConfig = Field(default_factory=FinalHttpxConfig)


class FinalCdhConfig(FinalConfigModel):
    """Active cdh-owned transfer settings."""

    default_downloader: Literal["aria2", "httpx"] = "aria2"
    default_download_mode: Literal["sync", "async"] = "sync"
    download_max_attempts: int = Field(default=3, ge=1)
    download_failure_policy: Literal["continue", "fail"] = "fail"
    downloader: FinalDownloaderConfig = Field(default_factory=FinalDownloaderConfig)


class FinalBuildConfig(FinalConfigModel):
    """Build output settings and the canonical ordered target platform list."""

    tags: list[str] = Field(default_factory=list)
    output: Literal["load", "push"] = "load"
    platforms: list[Literal["linux/amd64"]] = Field(
        default_factory=lambda: ["linux/amd64"], min_length=1
    )


class _FinalCustomNodeConfig(FinalConfigModel):
    """Ordered executable hooks shared by custom-node variants."""

    pre_install_scripts: list[str] = Field(default_factory=list)
    post_install_scripts: list[str] = Field(default_factory=list)


class FinalRegistryCustomNodeConfig(_FinalCustomNodeConfig):
    """A custom node selected by Registry identity."""

    type: Literal["registry"]
    id: str
    version: str | None = None


class FinalGitCustomNodeConfig(_FinalCustomNodeConfig):
    """A custom node installed directly from Git."""

    type: Literal["git"]
    url: str
    ref: str | None = None
    target_dir: str | None = None


FinalCustomNodeConfig = Annotated[
    FinalRegistryCustomNodeConfig | FinalGitCustomNodeConfig,
    Field(discriminator="type"),
]


class FinalComfyUIConfig(FinalConfigModel):
    """ComfyUI, Manager, launch, and custom-node requests."""

    version: str
    install_cli: bool = True
    install_manager: bool = True
    listen: str = "0.0.0.0"
    port: int = Field(default=8188, ge=1, le=65535)
    extra_args: list[str] = Field(default_factory=list)
    custom_nodes: list[FinalCustomNodeConfig] = Field(default_factory=list)


class FinalFileConfig(FinalConfigModel):
    """A required file transfer request without the deferred checksum field."""

    url: str
    dir: str
    filename: str
    overwrite: bool = False
    downloader: Literal["aria2", "httpx"] | None = None
    download_mode: Literal["sync", "async"] | None = None


class FinalConfig(FinalConfigModel):
    """Strict active public configuration schema."""

    cdh: FinalCdhConfig = Field(default_factory=FinalCdhConfig)
    compute_platform: FinalComputePlatformConfig
    system: FinalSystemConfig = Field(default_factory=FinalSystemConfig)
    python: FinalPythonConfig = Field(default_factory=FinalPythonConfig)
    pytorch: FinalPyTorchConfig
    build: FinalBuildConfig = Field(default_factory=FinalBuildConfig)
    comfyui: FinalComfyUIConfig
    files: list[FinalFileConfig] = Field(default_factory=list)
