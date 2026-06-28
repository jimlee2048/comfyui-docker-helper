"""Immutable normalized render-plan models and deterministic construction."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.models import Config, GitCustomNodeConfig
from comfyui_docker_helper.config.validation import (
    normalize_comfy_cli_version,
    normalize_comfyui_version,
    validate_config,
)

_VENV_PATH = "/opt/venv"
_DEFAULT_OS_PACKAGES = (
    "bash",
    "ca-certificates",
    "curl",
    "git",
    "build-essential",
    "aria2",
)
_PYTORCH_INDEX_BASE_URL = "https://download.pytorch.org/whl"


class Layer(StrEnum):
    """A Dockerfile operation in required execution order."""

    BASE_AND_UV = "base-and-uv"
    OS_PACKAGES = "os-packages"
    WORKSPACE_DIRECTORIES = "workspace-directories"
    PYTHON_VENV = "python-venv"
    PYTORCH = "pytorch"
    PYTHON_EXTRAS = "python-extras"
    COMFY_CLI = "comfy-cli"
    COMFYUI = "comfyui"
    CDH = "cdh"
    CUSTOM_NODES = "custom-nodes"
    FILES = "files"
    FINAL = "final"


class ArtifactKind(StrEnum):
    """The materialization behavior of a build-context artifact."""

    FILE = "file"
    TREE = "tree"


class ArtifactCondition(StrEnum):
    """The feature that activates a conditional output artifact."""

    CUSTOM_NODES = "custom-nodes"
    FILES = "files"
    HOOKS = "hooks"


@dataclass(frozen=True, slots=True)
class PathsPlan:
    """Resolved container paths."""

    workspace: str
    comfyui: str
    venv: str
    preinstall_directories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EnvironmentVariable:
    """One ordered user-supplied environment assignment."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class PythonPlan:
    """Normalized Python and uv settings."""

    version: str
    uv_version: str
    extra_packages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PyTorchPlan:
    """Normalized PyTorch wheel source and ordered requirements."""

    version: str
    wheel_tag: str
    index_base_url: str
    requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComfyUIPlan:
    """Normalized comfy-cli and ComfyUI installation and launch settings."""

    cli_version: str
    cli_requirement: str
    version: str
    install_manager: bool
    install_arguments: tuple[str, ...]
    launch_arguments: tuple[str, ...]
    launch_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegistryCustomNodePlan:
    """A normalized registry custom node."""

    type: Literal["registry"]
    id: str
    version: str | None
    target: str
    pre_install_scripts: tuple[str, ...]
    post_install_scripts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitCustomNodePlan:
    """A normalized Git custom node."""

    type: Literal["git"]
    url: str
    ref: str | None
    target_dir: str | None
    target: str
    pre_install_scripts: tuple[str, ...]
    post_install_scripts: tuple[str, ...]


type CustomNodePlan = RegistryCustomNodePlan | GitCustomNodePlan


@dataclass(frozen=True, slots=True)
class CustomNodesPlan:
    """Ordered custom nodes and helper orchestration flags."""

    items: tuple[CustomNodePlan, ...]
    update_cache: bool
    has_hooks: bool
    scripts_source_dir: Path | None


@dataclass(frozen=True, slots=True)
class Aria2Plan:
    """Normalized aria2 helper settings."""

    rpc_port: int
    split: int
    max_connection_per_server: int
    min_split_size: str
    resume_download: bool


@dataclass(frozen=True, slots=True)
class HttpxPlan:
    """Normalized HTTPX helper settings."""

    timeout: int | float
    retries: int


@dataclass(frozen=True, slots=True)
class DownloaderPlan:
    """Normalized downloader selection and both backend settings."""

    default: Literal["aria2", "httpx"]
    aria2: Aria2Plan
    httpx: HttpxPlan


@dataclass(frozen=True, slots=True)
class FilePlan:
    """One fully resolved ordered file download."""

    url: str
    directory: str
    filename: str
    target: str
    overwrite: bool
    downloader: Literal["aria2", "httpx"]


@dataclass(frozen=True, slots=True)
class FilesPlan:
    """Downloader settings and ordered resolved file items."""

    downloader: DownloaderPlan
    items: tuple[FilePlan, ...]


@dataclass(frozen=True, slots=True)
class BuildArgument:
    """One Docker build argument in specification order."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class OutputArtifact:
    """One file or tree expected in the rendered build context."""

    path: str
    kind: ArtifactKind
    condition: ArtifactCondition | None = None


@dataclass(frozen=True, slots=True)
class OutputManifest:
    """Always-present and active conditional context artifacts."""

    always: tuple[OutputArtifact, ...]
    conditional: tuple[OutputArtifact, ...]

    @property
    def all(self) -> tuple[OutputArtifact, ...]:
        """Return every artifact in materialization order."""
        return (*self.always, *self.conditional)


@dataclass(frozen=True, slots=True)
class RenderPlan:
    """The sole normalized input required by context rendering."""

    base_image: str
    paths: PathsPlan
    os_packages: tuple[str, ...]
    environment: tuple[EnvironmentVariable, ...]
    python: PythonPlan
    pytorch: PyTorchPlan
    comfyui: ComfyUIPlan
    custom_nodes: CustomNodesPlan
    files: FilesPlan
    build_arguments: tuple[BuildArgument, ...]
    layers: tuple[Layer, ...]
    output_manifest: OutputManifest


class RenderPlanValidationError(ValueError):
    """Refuse to normalize a configuration with business validation errors."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("configuration is invalid for render-plan construction")


def build_render_plan(
    config: Config,
    *,
    scripts_dir: str | Path | None = None,
) -> RenderPlan:
    """Build a deterministic immutable plan from a valid public configuration."""
    diagnostics = validate_config(config, scripts_dir=scripts_dir)
    if diagnostics:
        raise RenderPlanValidationError(diagnostics)

    workspace = str(PurePosixPath(config.system.workspace))
    comfyui_path = (
        str(PurePosixPath(config.system.comfyui_path))
        if config.system.comfyui_path is not None
        else str(PurePosixPath(workspace) / "ComfyUI")
    )
    paths = PathsPlan(
        workspace=workspace,
        comfyui=comfyui_path,
        venv=_VENV_PATH,
        preinstall_directories=_build_preinstall_directories(workspace, comfyui_path),
    )

    cuda = config.compute_platform.cuda
    cuda_image_tag = f"{cuda.version}-{cuda.image_flavor}-{cuda.image_distro}"
    wheel_tag = _pytorch_wheel_tag(cuda.version)
    comfyui_version = normalize_comfyui_version(config.comfyui.version)
    comfy_cli_version = normalize_comfy_cli_version(config.comfyui.cli_version)

    python = PythonPlan(
        version=config.python.version,
        uv_version=config.python.uv_version,
        extra_packages=tuple(config.python.extra_packages),
    )
    pytorch = PyTorchPlan(
        version=config.pytorch.version,
        wheel_tag=wheel_tag,
        index_base_url=_PYTORCH_INDEX_BASE_URL,
        requirements=(
            f"torch=={config.pytorch.version}",
            *config.pytorch.extra_packages,
        ),
    )
    comfyui = _build_comfyui_plan(
        config,
        paths,
        comfy_cli_version,
        comfyui_version,
    )
    custom_nodes = _build_custom_nodes_plan(config, scripts_dir)
    files = _build_files_plan(config, comfyui_path)
    build_arguments = _build_arguments(
        config,
        cuda_image_tag,
        wheel_tag,
        comfy_cli_version,
        comfyui_version,
    )

    return RenderPlan(
        base_image=f"nvidia/cuda:{cuda_image_tag}",
        paths=paths,
        os_packages=(*_DEFAULT_OS_PACKAGES, *config.system.extra_packages),
        environment=tuple(
            EnvironmentVariable(name, value)
            for name, value in config.system.env.items()
        ),
        python=python,
        pytorch=pytorch,
        comfyui=comfyui,
        custom_nodes=custom_nodes,
        files=files,
        build_arguments=build_arguments,
        layers=_build_layers(config),
        output_manifest=_build_output_manifest(custom_nodes, files),
    )


def _build_comfyui_plan(
    config: Config,
    paths: PathsPlan,
    cli_version: str,
    comfyui_version: str,
) -> ComfyUIPlan:
    install_arguments = (
        "--nvidia",
        "--version",
        comfyui_version,
        "--skip-torch-or-directml",
        "--fast-deps",
    )
    if not config.comfyui.install_manager:
        install_arguments = (*install_arguments, "--skip-manager")

    launch_arguments = tuple(config.comfyui.launch_args)
    return ComfyUIPlan(
        cli_version=cli_version,
        cli_requirement=(
            "comfy-cli" if cli_version == "latest" else f"comfy-cli=={cli_version}"
        ),
        version=comfyui_version,
        install_manager=config.comfyui.install_manager,
        install_arguments=install_arguments,
        launch_arguments=launch_arguments,
        launch_command=(
            "python",
            str(PurePosixPath(paths.comfyui) / "main.py"),
            *launch_arguments,
        ),
    )


def _build_preinstall_directories(
    workspace: str,
    comfyui_path: str,
) -> tuple[str, ...]:
    """Return directories to create before comfy-cli owns the install target."""
    directories: list[str] = [workspace]
    comfyui_parent = str(PurePosixPath(comfyui_path).parent)
    if comfyui_parent != "/" and comfyui_parent not in directories:
        directories.append(comfyui_parent)
    return tuple(directories)


def _build_custom_nodes_plan(
    config: Config,
    scripts_dir: str | Path | None,
) -> CustomNodesPlan:
    items: list[CustomNodePlan] = []
    has_registry = False
    has_hooks = False

    for node in config.comfyui.custom_nodes:
        pre_hooks = tuple(node.pre_install_scripts)
        post_hooks = tuple(node.post_install_scripts)
        has_hooks = has_hooks or bool(pre_hooks or post_hooks)
        if isinstance(node, GitCustomNodeConfig):
            items.append(
                GitCustomNodePlan(
                    type="git",
                    url=node.url,
                    ref=node.ref,
                    target_dir=node.target_dir,
                    target=node.url if node.ref is None else f"{node.url}@{node.ref}",
                    pre_install_scripts=pre_hooks,
                    post_install_scripts=post_hooks,
                )
            )
        else:
            has_registry = True
            items.append(
                RegistryCustomNodePlan(
                    type="registry",
                    id=node.id,
                    version=node.version,
                    target=node.id
                    if node.version is None
                    else f"{node.id}@{node.version}",
                    pre_install_scripts=pre_hooks,
                    post_install_scripts=post_hooks,
                )
            )

    scripts_source_dir = None
    if has_hooks:
        if scripts_dir is None:
            raise RuntimeError("validated hook configuration has no scripts-dir")
        scripts_source_dir = Path(scripts_dir).resolve()

    return CustomNodesPlan(
        items=tuple(items),
        update_cache=has_registry,
        has_hooks=has_hooks,
        scripts_source_dir=scripts_source_dir,
    )


def _build_files_plan(config: Config, comfyui_path: str) -> FilesPlan:
    downloader = DownloaderPlan(
        default=config.downloader.default,
        aria2=Aria2Plan(
            rpc_port=config.downloader.aria2.rpc_port,
            split=config.downloader.aria2.split,
            max_connection_per_server=(
                config.downloader.aria2.max_connection_per_server
            ),
            min_split_size=config.downloader.aria2.min_split_size,
            resume_download=config.downloader.aria2.resume_download,
        ),
        httpx=HttpxPlan(
            timeout=config.downloader.httpx.timeout,
            retries=config.downloader.httpx.retries,
        ),
    )
    items: list[FilePlan] = []
    for file in config.files:
        effective_downloader = file.downloader or downloader.default
        target = str(PurePosixPath(comfyui_path) / file.dir / file.filename)
        items.append(
            FilePlan(
                url=file.url,
                directory=file.dir,
                filename=file.filename,
                target=target,
                overwrite=file.overwrite,
                downloader=effective_downloader,
            )
        )

    return FilesPlan(downloader=downloader, items=tuple(items))


def _build_arguments(
    config: Config,
    cuda_image_tag: str,
    wheel_tag: str,
    comfy_cli_version: str,
    comfyui_version: str,
) -> tuple[BuildArgument, ...]:
    return (
        BuildArgument("UV_IMAGE_TAG", config.python.uv_version),
        BuildArgument("CUDA_IMAGE_TAG", cuda_image_tag),
        BuildArgument("PYTHON_VERSION", config.python.version),
        BuildArgument("PYTORCH_VERSION", config.pytorch.version),
        BuildArgument("PYTORCH_WHEEL_TAG", wheel_tag),
        BuildArgument("COMFY_CLI_VERSION", comfy_cli_version),
        BuildArgument("COMFYUI_VERSION", comfyui_version),
        BuildArgument("UV_LINK_MODE", "copy"),
        BuildArgument("UV_PYTHON_CACHE_DIR", "/root/.cache/uv/python"),
    )


def _build_layers(config: Config) -> tuple[Layer, ...]:
    layers = [
        Layer.BASE_AND_UV,
        Layer.OS_PACKAGES,
        Layer.WORKSPACE_DIRECTORIES,
        Layer.PYTHON_VENV,
        Layer.PYTORCH,
    ]
    if config.python.extra_packages:
        layers.append(Layer.PYTHON_EXTRAS)
    layers.extend((Layer.COMFY_CLI, Layer.COMFYUI, Layer.CDH))
    if config.comfyui.custom_nodes:
        layers.append(Layer.CUSTOM_NODES)
    if config.files:
        layers.append(Layer.FILES)
    layers.append(Layer.FINAL)
    return tuple(layers)


def _build_output_manifest(
    custom_nodes: CustomNodesPlan,
    files: FilesPlan,
) -> OutputManifest:
    always = (
        OutputArtifact("Dockerfile", ArtifactKind.FILE),
        OutputArtifact(".cdh-rendered", ArtifactKind.FILE),
        OutputArtifact("packages/cdh/pyproject.toml", ArtifactKind.FILE),
        OutputArtifact("packages/cdh/src", ArtifactKind.TREE),
    )
    conditional: list[OutputArtifact] = []
    if custom_nodes.items:
        conditional.append(
            OutputArtifact(
                "config/custom-nodes.toml",
                ArtifactKind.FILE,
                ArtifactCondition.CUSTOM_NODES,
            )
        )
    if files.items:
        conditional.append(
            OutputArtifact(
                "config/files.toml",
                ArtifactKind.FILE,
                ArtifactCondition.FILES,
            )
        )
    if custom_nodes.has_hooks:
        conditional.append(
            OutputArtifact(
                "scripts",
                ArtifactKind.TREE,
                ArtifactCondition.HOOKS,
            )
        )
    return OutputManifest(always=always, conditional=tuple(conditional))


def _pytorch_wheel_tag(version: str) -> str:
    major, minor, *_ = version.split(".")
    return f"cu{major}{minor}"
