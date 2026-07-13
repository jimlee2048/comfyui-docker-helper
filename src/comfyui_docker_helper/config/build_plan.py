"""Immutable BuildPlan v1 authority and deterministic construction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Literal

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from comfyui_docker_helper.comfyui_requirements import (
    COMFYUI_REQUIREMENTS_PATH,
    CUDA_PROTECTED_REQUIREMENTS,
    ComfyUIRequirementsError,
    merge_pytorch_requirements,
    protected_policy_digest,
    requirement_text,
)
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    CanonicalLockEntry,
    ComfyCliLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectGitLockEntry,
    DirectPythonLockEntry,
    DirectPythonRequestMember,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    OciLockEntry,
    OfficialComfyUILockEntry,
    ProtectedRequirementProjection,
    PyTorchCompatibilityLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    canonical_entry_key,
    compute_request_digest,
    dump_canonical_lock_toml,
    pytorch_core_version_matches_channel,
    pytorch_index_matches_channel,
    uv_image_version_matches_tag,
)
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.final_models import (
    FinalConfig,
    FinalGitCustomNodeConfig,
    FinalRegistryCustomNodeConfig,
)
from comfyui_docker_helper.config.final_planning import (
    CudaBackendAdapter,
    CudaVersion,
    TargetPlatform,
)
from comfyui_docker_helper.config.final_validation import (
    is_managed_environment_name,
    validate_direct_requirement,
)
from comfyui_docker_helper.config.runtime_hooks import (
    CUSTOM_NODE_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS,
)
from comfyui_docker_helper.config.selector_validation import (
    normalize_comfyui_version,
    normalize_registry_version,
    resolve_git_target_dir,
)
from comfyui_docker_helper.config.url_validation import is_http_url
from comfyui_docker_helper.config.value_validation import has_control_characters
from comfyui_docker_helper.exact_ledger import (
    COMFY_CLI_MINIMUM_VERSION,
    COMFYUI_FLOOR_COMMIT,
    COMFYUI_REPOSITORY,
    UV_IMAGE_REPOSITORY,
)
from comfyui_docker_helper.release_artifacts import (
    production_inventory,
    production_requirements_digest,
)

BUILD_PLAN_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
_VENV_PATH = "/opt/venv"
_DEFAULT_OS_PACKAGES = (
    "bash",
    "ca-certificates",
    "curl",
    "git",
    "build-essential",
    "aria2",
    "openssh-server",
)


class _PlanModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class ImagePlan(_PlanModel):
    role: Literal["cuda-base", "uv-tool"]
    repository: str
    tag: str
    descriptor_digest: str
    descriptor_kind: Literal["index", "manifest"]
    platform: Literal["linux/amd64"]
    resolved_version: str | None = None

    @model_validator(mode="after")
    def _validate_role_version(self) -> ImagePlan:
        if (self.role == "uv-tool") != (self.resolved_version is not None):
            raise ValueError("only uv-tool images require a resolved version")
        if self.role == "uv-tool" and not uv_image_version_matches_tag(
            self.tag, self.resolved_version
        ):
            raise ValueError("uv image resolved version does not match its exact tag")
        return self

    @property
    def reference(self) -> str:
        return f"{self.repository}:{self.tag}@{self.descriptor_digest}"


class ManagedPythonPlan(_PlanModel):
    version: str
    implementation: Literal["cpython"]
    platform: Literal["linux/amd64"]
    libc: Literal["gnu"]
    provider: Literal["uv-managed"]
    catalog_descriptor_digest: str
    catalog_key: str
    catalog_url: str
    pip_version: str
    cdh_version: str
    cdh_source_digest: str
    uv_build_version: str


class ExactDistributionPlan(_PlanModel):
    name: str
    version: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if canonicalize_name(value) != value:
            raise ValueError("name must be one normalized distribution name")
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _exact_distribution_version(value)


class UvToolPlan(_PlanModel):
    name: str
    extras: tuple[str, ...]
    version: str
    environment: str

    @model_validator(mode="after")
    def _validate_identity(self) -> UvToolPlan:
        if canonicalize_name(self.name) != self.name:
            raise ValueError("uv tool name must be normalized")
        _exact_distribution_version(self.version)
        if self.environment != f"uv-tool:{self.name}":
            raise ValueError("uv tool environment must match its distribution")
        if tuple(sorted(set(self.extras))) != self.extras or any(
            canonicalize_name(extra) != extra for extra in self.extras
        ):
            raise ValueError("uv tool extras must be sorted, unique, and normalized")
        return self

    @property
    def requirement(self) -> str:
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        return f"{self.name}{extras}=={self.version}"


class ComfyCliToolPlan(_PlanModel):
    """Exact dedicated isolated comfy-cli tool consumer."""

    name: Literal["comfy-cli"]
    version: str
    environment: Literal["uv-tool:comfy-cli"]
    executables: tuple[Literal["comfy"], Literal["comfy-cli"], Literal["comfycli"]]
    inventory_path: Literal["/opt/cdh/build/comfy-cli-inventory.txt"]

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        version = Version(_exact_distribution_version(value))
        if version.local is not None or version < Version(COMFY_CLI_MINIMUM_VERSION):
            raise ValueError("comfy-cli version is below the supported tool floor")
        return value

    @property
    def requirement(self) -> str:
        return f"{self.name}=={self.version}"


class ToolStorePlan(_PlanModel):
    tool_dir: Literal["/opt/uv/tools"]
    bin_dir: Literal["/opt/uv/bin"]
    cdh_environment: Literal["/opt/uv/tools/comfyui-docker-helper"]
    cdh_executable: Literal["/opt/uv/bin/cdh"]
    requirements_digest: str
    cdh_closure: tuple[ExactDistributionPlan, ...]
    comfy_cli: ComfyCliToolPlan | None
    uv_tools: tuple[UvToolPlan, ...]

    @model_validator(mode="after")
    def _validate_unique_tools(self) -> ToolStorePlan:
        names = [tool.name for tool in self.uv_tools]
        if self.comfy_cli is not None:
            if self.comfy_cli.name != "comfy-cli":
                raise ValueError("optional comfy-cli tool has the wrong owner")
            names.append(self.comfy_cli.name)
        if len(names) != len(set(names)):
            raise ValueError("uv tools must have unique distribution owners")
        closure = tuple((item.name, item.version) for item in self.cdh_closure)
        if not closure or tuple(sorted(set(closure))) != closure:
            raise ValueError("cdh closure must be non-empty, sorted, and unique")
        if (
            not self.requirements_digest.startswith("sha256:")
            or len(self.requirements_digest) != 71
            or any(
                character not in "0123456789abcdef"
                for character in self.requirements_digest.removeprefix("sha256:")
            )
        ):
            raise ValueError("requirements_digest must be one SHA-256 digest")
        return self


class ToolchainPhase(_PlanModel):
    platform: Literal["linux/amd64"]
    cuda_version: str
    pytorch_channel: str
    cuda_image: ImagePlan
    uv_image: ImagePlan
    python: ManagedPythonPlan
    tool_store: ToolStorePlan


class PathsPlan(_PlanModel):
    workspace: str
    comfyui: str
    venv: Literal["/opt/venv"]


class ExactPackagePlan(_PlanModel):
    name: str
    extras: tuple[str, ...]
    version: str
    environment: Literal["application"]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if canonicalize_name(value) != value:
            raise ValueError("package name must be one normalized distribution name")
        if value == "comfy-cli":
            raise ValueError("comfy-cli is reserved to the dedicated optional tool")
        return value

    @field_validator("extras")
    @classmethod
    def _validate_extras(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(canonicalize_name(extra) for extra in value)
        if normalized != value or tuple(sorted(set(normalized))) != value:
            raise ValueError("package extras must be sorted, unique, and normalized")
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        try:
            version = Version(value)
        except InvalidVersion as error:
            raise ValueError(
                "version must be one canonical exact stable distribution version"
            ) from error
        if str(version) != value or version.is_prerelease or version.is_devrelease:
            raise ValueError(
                "version must be one canonical exact stable distribution version"
            )
        return value

    @property
    def requirement(self) -> str:
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        return f"{self.name}{extras}=={self.version}"


class PackageGroupPlan(_PlanModel):
    group: Literal["application-extra", "pytorch"]
    python_version: str
    platform: Literal["linux/amd64"]
    index_url: str
    packages: tuple[ExactPackagePlan, ...]


class PyTorchGroupPlan(_PlanModel):
    group: Literal["pytorch"]
    backend: Literal["cuda"]
    channel: str
    python_version: str
    platform: Literal["linux/amd64"]
    python_index_url: str
    pytorch_index_url: str
    packages: tuple[ExactPackagePlan, ...]
    setuptools_specifier: str | None

    @field_validator("python_index_url", "pytorch_index_url")
    @classmethod
    def _validate_index_url(cls, value: str) -> str:
        if not is_http_url(value):
            raise ValueError("index URL must be one HTTP(S) URL")
        return value

    @field_validator("setuptools_specifier")
    @classmethod
    def _validate_setuptools_specifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = SpecifierSet(value)
        if not value or str(parsed) != value:
            raise ValueError("setuptools specifier must be canonical")
        return value

    @model_validator(mode="after")
    def _validate_group_identity(self) -> PyTorchGroupPlan:
        if re.fullmatch(r"cu[0-9]+", self.channel) is None:
            raise ValueError("PyTorch channel must be canonical")
        if not pytorch_index_matches_channel(self.pytorch_index_url, self.channel):
            raise ValueError("PyTorch index must end with its derived channel")
        if self.python_index_url == self.pytorch_index_url:
            raise ValueError("Python and PyTorch indexes must be distinct")
        names = tuple(canonicalize_name(package.name) for package in self.packages)
        if "torch" not in names or len(names) != len(set(names)):
            raise ValueError("PyTorch packages must be unique and include torch")
        if names != tuple(sorted(names, key=lambda name: (name != "torch", name))):
            raise ValueError("PyTorch packages must be canonically ordered")
        if any(
            not pytorch_core_version_matches_channel(
                package.name, package.version, self.channel
            )
            for package in self.packages
        ):
            raise ValueError("PyTorch core package does not match the group channel")
        return self


def managed_constraints_bytes(group: PyTorchGroupPlan) -> bytes:
    """Project exact protected packages plus wheel-derived compatibility."""
    requirements = [f"{package.name}=={package.version}" for package in group.packages]
    if group.setuptools_specifier is not None:
        requirements.append(f"setuptools{group.setuptools_specifier}")
    requirements.sort()
    return ("\n".join(requirements) + "\n").encode("utf-8")


class ProtectedRequirementPlan(_PlanModel):
    package: str
    extras: tuple[str, ...]
    selector: str


class ComfyUIRequirementsPlan(_PlanModel):
    path: Literal["requirements.txt"]
    floor_commit: str
    python_version: str
    platform: Literal["linux/amd64"]
    protected_names: tuple[str, ...]
    protected_policy_digest: str
    digest: str
    protected: tuple[ProtectedRequirementPlan, ...]

    @model_validator(mode="after")
    def _validate_projection(self) -> ComfyUIRequirementsPlan:
        if self.floor_commit != COMFYUI_FLOOR_COMMIT:
            raise ValueError("ComfyUI requirements floor does not match the ledger")
        names = tuple(item.package for item in self.protected)
        if names != tuple(sorted(set(names))):
            raise ValueError("protected requirements must be sorted and unique")
        if any(name not in self.protected_names for name in names):
            raise ValueError("protected requirement is not adapter-owned")
        return self


class ComfyUIPlan(_PlanModel):
    repository: str
    commit: str
    floor_commit: str
    formal_release: str | None
    install_manager: bool
    requirements: ComfyUIRequirementsPlan

    @field_validator("floor_commit")
    @classmethod
    def _validate_floor_commit(cls, value: str) -> str:
        if value != COMFYUI_FLOOR_COMMIT:
            raise ValueError("ComfyUI floor commit does not match the exact ledger")
        return value


class ApplicationPhase(_PlanModel):
    paths: PathsPlan
    os_packages: tuple[str, ...]
    python_index_url: str
    python_extras: PackageGroupPlan | None
    pytorch: PyTorchGroupPlan
    comfyui: ComfyUIPlan

    @model_validator(mode="after")
    def _validate_python_sources(self) -> ApplicationPhase:
        if not is_http_url(self.python_index_url):
            raise ValueError("python_index_url must be one HTTP(S) URL")
        if self.pytorch.python_index_url != self.python_index_url:
            raise ValueError("PyTorch generic dependencies must use the Python index")
        if self.python_extras is not None and (
            self.python_extras.index_url != self.python_index_url
            or self.python_extras.python_version != self.pytorch.python_version
            or self.python_extras.platform != self.pytorch.platform
        ):
            raise ValueError("application Python groups must share target and index")
        requirements = self.comfyui.requirements
        if (
            requirements.python_version != self.pytorch.python_version
            or requirements.platform != self.pytorch.platform
            or requirements.floor_commit != self.comfyui.floor_commit
        ):
            raise ValueError("ComfyUI requirements target must match PyTorch")
        return self


class HookPlan(_PlanModel):
    relative_path: str
    digest: str

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or value.startswith("/")
            or "\\" in value
            or has_control_characters(value)
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("relative_path must be one canonical safe POSIX path")
        return value


class RegistryNodePlan(_PlanModel):
    type: Literal["registry"]
    id: str
    version: str
    pre_install: tuple[HookPlan, ...]
    post_install: tuple[HookPlan, ...]


class GitNodePlan(_PlanModel):
    type: Literal["git"]
    url: str
    commit: str
    target: str
    pre_install: tuple[HookPlan, ...]
    post_install: tuple[HookPlan, ...]


CustomNodePlan = Annotated[RegistryNodePlan | GitNodePlan, Field(discriminator="type")]


class CustomNodesPhase(_PlanModel):
    install_manager: bool
    nodes: tuple[CustomNodePlan, ...]


class Aria2Plan(_PlanModel):
    rpc_port: int
    split: int
    max_connection_per_server: int
    min_split_size: str
    resume_download: bool


class HttpxPlan(_PlanModel):
    timeout: int | float
    retries: int


class DownloaderPlan(_PlanModel):
    default: Literal["aria2", "httpx"]
    aria2: Aria2Plan
    httpx: HttpxPlan


class FilePlan(_PlanModel):
    url: str
    target: str
    overwrite: bool
    downloader: Literal["aria2", "httpx"]
    download_mode: Literal["sync", "async"]
    downloader_explicit: bool
    download_mode_explicit: bool


class FilesPhase(_PlanModel):
    downloader: DownloaderPlan
    default_download_mode: Literal["sync", "async"]
    download_max_attempts: int
    download_failure_policy: Literal["continue", "fail"]
    files: tuple[FilePlan, ...]


class EnvironmentPlan(_PlanModel):
    name: str
    value: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if is_managed_environment_name(value):
            raise ValueError("environment name is reserved to cdh image authority")
        return value


class SshPlan(_PlanModel):
    enable: bool
    port: int
    password: str
    pub_keys: tuple[str, ...]


class RuntimePhase(_PlanModel):
    environment: tuple[EnvironmentPlan, ...]
    ssh: SshPlan
    launch_command: tuple[str, ...]
    hooks: tuple[HookPlan, ...]
    download_failure_policy: Literal["continue", "fail"] | None


class BuildOutputPlan(_PlanModel):
    tags: tuple[str, ...]
    output: Literal["load", "push"]
    platforms: tuple[Literal["linux/amd64"], ...]


class BuildPlan(_PlanModel):
    """Complete immutable build execution authority."""

    schema_version: Literal[1]
    config_digest: str
    lock_digest: str
    build: BuildOutputPlan
    toolchain: ToolchainPhase
    application: ApplicationPhase
    custom_nodes: CustomNodesPhase
    files: FilesPhase
    runtime: RuntimePhase

    @model_validator(mode="after")
    def _validate_pytorch_channel(self) -> BuildPlan:
        pytorch = self.application.pytorch
        if (
            pytorch.channel != self.toolchain.pytorch_channel
            or pytorch.python_version != self.toolchain.python.version
            or pytorch.platform != self.toolchain.platform
            or self.toolchain.python.platform != self.toolchain.platform
            or self.toolchain.uv_image.role != "uv-tool"
            or self.toolchain.cuda_image.role != "cuda-base"
            or self.toolchain.uv_image.platform != self.toolchain.platform
            or self.toolchain.cuda_image.platform != self.toolchain.platform
        ):
            raise ValueError("PyTorch application target does not match the toolchain")
        return self


class ManifestBinding(_PlanModel):
    """Stable final-verification binding without observed evidence or timestamps."""

    schema_version: Literal[1]
    build_plan_schema_version: Literal[1]
    build_plan_digest: str
    config_digest: str
    lock_digest: str


@dataclass(frozen=True, slots=True)
class RuntimePlanningProvenance:
    """Authorship needed when projecting host config into runtime config."""

    failure_policy_explicit: bool = False
    file_downloader_explicit: tuple[bool, ...] = ()
    file_download_mode_explicit: tuple[bool, ...] = ()


def construct_build_plan(
    config: FinalConfig,
    resolution: AcceptedCanonicalLock,
    *,
    runtime_provenance: RuntimePlanningProvenance | None = None,
) -> BuildPlan:
    """Construct BuildPlan exactly once from validated config and accepted lock."""
    config = FinalConfig.model_validate(config.model_dump(mode="python"))
    lock = CanonicalLock.model_validate(resolution.lock.model_dump(mode="python"))
    target_platform = TargetPlatform(config.build.platforms[0])
    backend = CudaBackendAdapter().derive(
        CudaVersion.from_validated(config.compute_platform.cuda.version),
        target_platform,
    )
    entries = {canonical_entry_key(entry): entry for entry in lock.entries}
    if len(entries) != len(lock.entries):  # defensive after strict reconstruction
        raise ValueError("canonical lock contains duplicate logical identities")
    used: set[tuple[str, ...]] = set()

    cuda_entry = _take(entries, used, ("oci", "cuda-base"), OciLockEntry)
    uv_entry = _take(entries, used, ("oci", "uv-tool"), OciLockEntry)
    python_entry = _take(
        entries,
        used,
        ("managed-python", "cpython", "linux/amd64"),
        ManagedPythonLockEntry,
    )
    comfyui_entry = _take(
        entries, used, ("comfyui", COMFYUI_REPOSITORY), OfficialComfyUILockEntry
    )
    requirements_entry = _take(
        entries,
        used,
        ("comfyui-requirements", COMFYUI_REPOSITORY),
        ComfyUIRequirementsLockEntry,
    )
    cli_entry = None
    if config.comfyui.install_cli:
        cli_entry = _take(
            entries,
            used,
            ("comfy-cli", "comfy-cli", "uv-tool:comfy-cli"),
            ComfyCliLockEntry,
        )

    expected_cuda_repository, expected_cuda_tag = backend.base_image.split(":", 1)
    if (
        cuda_entry.repository != expected_cuda_repository
        or cuda_entry.tag != expected_cuda_tag
        or cuda_entry.platform != target_platform.value
    ):
        raise ValueError("canonical CUDA image does not match final config")
    if (
        uv_entry.repository != UV_IMAGE_REPOSITORY
        or uv_entry.tag != config.python.uv_version
        or uv_entry.platform != target_platform.value
    ):
        raise ValueError("canonical uv image does not match final config")
    if (
        python_entry.version != config.python.version
        or python_entry.platform != target_platform.value
        or python_entry.catalog_descriptor_digest != uv_entry.descriptor_digest
    ):
        raise ValueError(
            "canonical managed Python does not match final config/toolchain"
        )
    if comfyui_entry.repository != COMFYUI_REPOSITORY:
        raise ValueError("canonical ComfyUI source is not official")
    if not _comfyui_selector_accepts(config.comfyui.version, comfyui_entry):
        raise ValueError("canonical ComfyUI identity does not match final config")
    requirements_request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository=COMFYUI_REPOSITORY,
        commit=comfyui_entry.commit,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path=COMFYUI_REQUIREMENTS_PATH,
        python_version=config.python.version,
        platform=target_platform.value,
        protected_names=list(sorted(CUDA_PROTECTED_REQUIREMENTS)),
        protected_policy_digest=protected_policy_digest(
            tuple(sorted(CUDA_PROTECTED_REQUIREMENTS))
        ),
    )
    if (
        requirements_entry.request_digest
        != compute_request_digest(requirements_request)
        or requirements_entry.repository != comfyui_entry.repository
        or requirements_entry.commit != comfyui_entry.commit
        or requirements_entry.floor_commit != COMFYUI_FLOOR_COMMIT
        or requirements_entry.path != requirements_request.path
        or requirements_entry.python_version != config.python.version
        or requirements_entry.platform != target_platform.value
        or requirements_entry.protected_names != requirements_request.protected_names
        or requirements_entry.protected_policy_digest
        != requirements_request.protected_policy_digest
    ):
        raise ValueError("canonical ComfyUI requirements do not match source/target")
    comfy_cli = None
    if cli_entry is not None:
        request = ComfyCliRequestIdentity(
            type="comfy-cli",
            package="comfy-cli",
            policy="highest-target-compatible-stable",
            minimum_version=COMFY_CLI_MINIMUM_VERSION,
            environment="uv-tool:comfy-cli",
            index_url=config.python.index_url,
            python_version=config.python.version,
            platform=target_platform.value,
        )
        if cli_entry.request_digest != compute_request_digest(request):
            raise ValueError("canonical comfy-cli identity does not match final config")
        comfy_cli = ComfyCliToolPlan(
            name=cli_entry.package,
            version=cli_entry.version,
            environment=cli_entry.environment,
            executables=("comfy", "comfy-cli", "comfycli"),
            inventory_path="/opt/cdh/build/comfy-cli-inventory.txt",
        )

    python_packages = _package_group(
        config.python.extra_packages,
        "application-extra",
        config.python.version,
        target_platform.value,
        config.python.index_url,
        entries,
        used,
        package_channel=None,
    )
    uv_tools = tuple(
        _uv_tool(requirement, config.python.version, entries, used)
        for requirement in config.python.uv_tools
    )
    configured_members: list[DirectPythonRequestMember] = []
    for index, value in enumerate(config.pytorch.extra_packages):
        diagnostics: list = []
        normalized = validate_direct_requirement(
            value, ("pytorch", "extra_packages", index), diagnostics
        )
        if normalized is None or diagnostics:
            raise ValueError("validated config contains an invalid package requirement")
        configured_members.append(
            DirectPythonRequestMember(
                package=normalized.name,
                extras=list(normalized.extras),
                selector=normalized.specifier,
            )
        )
    upstream = tuple(
        DirectPythonRequestMember(
            package=item.package,
            extras=item.extras,
            selector=item.selector,
        )
        for item in requirements_entry.protected
    )
    try:
        pytorch_members = list(
            merge_pytorch_requirements(
                DirectPythonRequestMember(
                    package="torch",
                    extras=[],
                    selector=f"=={config.pytorch.version}",
                ),
                upstream,
                tuple(configured_members),
            )
        )
    except ComfyUIRequirementsError as error:
        raise ValueError("protected PyTorch requirements conflict") from error
    pytorch_requirements = [requirement_text(member) for member in pytorch_members]
    pytorch_packages = _package_group(
        pytorch_requirements,
        "pytorch",
        config.python.version,
        target_platform.value,
        f"{config.pytorch.index_base_url.rstrip('/')}/{backend.package_channel}",
        entries,
        used,
        package_channel=backend.package_channel,
    )
    pytorch_compatibility = _take(
        entries,
        used,
        ("pytorch-compatibility", "application"),
        PyTorchCompatibilityLockEntry,
    )
    expected_pytorch_digest = compute_request_digest(
        PyTorchRequestIdentity(
            type="pytorch-group",
            environment="application",
            group="pytorch",
            backend="cuda",
            channel=backend.package_channel,
            python_version=config.python.version,
            platform=target_platform.value,
            python_index_url=config.python.index_url,
            pytorch_index_url=(
                f"{config.pytorch.index_base_url.rstrip('/')}/{backend.package_channel}"
            ),
            upstream_protected=[
                ProtectedRequirementProjection(
                    package=item.package,
                    extras=item.extras,
                    selector=item.selector,
                )
                for item in upstream
            ],
            members=pytorch_members,
        )
    )
    if pytorch_compatibility.request_digest != expected_pytorch_digest or any(
        entries[("python-package", "application", package.name)].request_digest
        != expected_pytorch_digest
        for package in pytorch_packages.packages
    ):
        raise ValueError(
            "canonical PyTorch packages and compatibility policy "
            "must share one request digest"
        )
    torch = next(item for item in pytorch_packages.packages if item.name == "torch")
    if Version(torch.version).public != config.pytorch.version:
        raise ValueError("canonical torch version does not match final config")

    workspace = str(PurePosixPath(config.system.workspace))
    comfyui_path = str(
        PurePosixPath(config.system.comfyui_path)
        if config.system.comfyui_path is not None
        else PurePosixPath(workspace) / "ComfyUI"
    )
    paths = PathsPlan(workspace=workspace, comfyui=comfyui_path, venv=_VENV_PATH)
    custom_nodes = tuple(
        _custom_node(node, comfyui_path, entries, used)
        for node in config.comfyui.custom_nodes
    )
    provenance = runtime_provenance or RuntimePlanningProvenance()
    if provenance.file_downloader_explicit and len(
        provenance.file_downloader_explicit
    ) != len(config.files):
        raise ValueError("runtime file downloader provenance does not match config")
    if provenance.file_download_mode_explicit and len(
        provenance.file_download_mode_explicit
    ) != len(config.files):
        raise ValueError("runtime file download-mode provenance does not match config")
    downloader_explicit = provenance.file_downloader_explicit or (False,) * len(
        config.files
    )
    download_mode_explicit = provenance.file_download_mode_explicit or (False,) * len(
        config.files
    )
    files = tuple(
        FilePlan(
            url=item.url,
            target=str(PurePosixPath(comfyui_path) / item.dir / item.filename),
            overwrite=item.overwrite,
            downloader=item.downloader or config.cdh.default_downloader,
            download_mode=item.download_mode or config.cdh.default_download_mode,
            downloader_explicit=downloader_explicit[index],
            download_mode_explicit=download_mode_explicit[index],
        )
        for index, item in enumerate(config.files)
    )
    runtime_hooks = _runtime_hooks(entries, used)
    unused = sorted(set(entries) - used)
    if unused:
        raise ValueError(f"canonical lock contains unused identities: {unused!r}")

    plan = BuildPlan(
        schema_version=BUILD_PLAN_SCHEMA_VERSION,
        config_digest=_config_digest(config),
        lock_digest=_digest_bytes(dump_canonical_lock_toml(lock).encode("utf-8")),
        build=BuildOutputPlan(
            tags=tuple(config.build.tags),
            output=config.build.output,
            platforms=tuple(config.build.platforms),
        ),
        toolchain=ToolchainPhase(
            platform=target_platform.value,
            cuda_version=backend.version.value,
            pytorch_channel=backend.package_channel,
            cuda_image=_image_plan(cuda_entry),
            uv_image=_image_plan(uv_entry),
            python=ManagedPythonPlan.model_validate(
                python_entry.model_dump(
                    mode="python", exclude={"type", "request_digest"}
                )
            ),
            tool_store=ToolStorePlan(
                tool_dir="/opt/uv/tools",
                bin_dir="/opt/uv/bin",
                cdh_environment="/opt/uv/tools/comfyui-docker-helper",
                cdh_executable="/opt/uv/bin/cdh",
                requirements_digest=production_requirements_digest(),
                cdh_closure=tuple(
                    ExactDistributionPlan(name=name, version=version)
                    for name, version in production_inventory(config.python.version)
                ),
                comfy_cli=comfy_cli,
                uv_tools=uv_tools,
            ),
        ),
        application=ApplicationPhase(
            paths=paths,
            os_packages=(*_DEFAULT_OS_PACKAGES, *config.system.extra_packages),
            python_index_url=config.python.index_url,
            python_extras=python_packages if python_packages.packages else None,
            pytorch=PyTorchGroupPlan(
                group="pytorch",
                backend="cuda",
                channel=backend.package_channel,
                python_version=pytorch_packages.python_version,
                platform=pytorch_packages.platform,
                python_index_url=config.python.index_url,
                pytorch_index_url=(
                    f"{config.pytorch.index_base_url.rstrip('/')}/"
                    f"{backend.package_channel}"
                ),
                packages=pytorch_packages.packages,
                setuptools_specifier=pytorch_compatibility.setuptools_specifier,
            ),
            comfyui=ComfyUIPlan(
                repository=comfyui_entry.repository,
                commit=comfyui_entry.commit,
                floor_commit=COMFYUI_FLOOR_COMMIT,
                formal_release=comfyui_entry.formal_release,
                install_manager=config.comfyui.install_manager,
                requirements=ComfyUIRequirementsPlan(
                    path=requirements_entry.path,
                    floor_commit=requirements_entry.floor_commit,
                    python_version=requirements_entry.python_version,
                    platform=requirements_entry.platform,
                    protected_names=tuple(requirements_entry.protected_names),
                    protected_policy_digest=requirements_entry.protected_policy_digest,
                    digest=requirements_entry.requirements_digest,
                    protected=tuple(
                        ProtectedRequirementPlan(
                            package=item.package,
                            extras=tuple(item.extras),
                            selector=item.selector,
                        )
                        for item in requirements_entry.protected
                    ),
                ),
            ),
        ),
        custom_nodes=CustomNodesPhase(
            install_manager=config.comfyui.install_manager,
            nodes=custom_nodes,
        ),
        files=FilesPhase(
            downloader=DownloaderPlan(
                default=config.cdh.default_downloader,
                aria2=Aria2Plan.model_validate(
                    config.cdh.downloader.aria2.model_dump(mode="python")
                ),
                httpx=HttpxPlan.model_validate(
                    config.cdh.downloader.httpx.model_dump(mode="python")
                ),
            ),
            default_download_mode=config.cdh.default_download_mode,
            download_max_attempts=config.cdh.download_max_attempts,
            download_failure_policy=config.cdh.download_failure_policy,
            files=files,
        ),
        runtime=RuntimePhase(
            environment=tuple(
                EnvironmentPlan(name=name, value=value)
                for name, value in sorted(config.system.env.items())
            ),
            ssh=SshPlan(
                enable=config.system.ssh.enable,
                port=config.system.ssh.port,
                password=config.system.ssh.password,
                pub_keys=tuple(config.system.ssh.pub_keys),
            ),
            launch_command=(
                f"{_VENV_PATH}/bin/python",
                f"{comfyui_path}/main.py",
                "--listen",
                config.comfyui.listen,
                "--port",
                str(config.comfyui.port),
                "--disable-auto-launch",
                *config.comfyui.extra_args,
            ),
            hooks=runtime_hooks,
            download_failure_policy=(
                config.cdh.download_failure_policy
                if provenance.failure_policy_explicit
                else None
            ),
        ),
    )
    return BuildPlan.model_validate(plan.model_dump(mode="python"))


def build_plan_digest(plan: BuildPlan) -> str:
    return _digest_bytes(dump_build_plan_json(plan))


def dump_build_plan_json(plan: BuildPlan) -> bytes:
    return _canonical_json(plan.model_dump(mode="json"))


def parse_build_plan_json(document: str | bytes) -> BuildPlan:
    return BuildPlan.model_validate_json(document)


def manifest_binding(plan: BuildPlan) -> ManifestBinding:
    return ManifestBinding(
        schema_version=MANIFEST_SCHEMA_VERSION,
        build_plan_schema_version=plan.schema_version,
        build_plan_digest=build_plan_digest(plan),
        config_digest=plan.config_digest,
        lock_digest=plan.lock_digest,
    )


def parse_manifest_binding_json(document: str | bytes) -> ManifestBinding:
    return ManifestBinding.model_validate_json(document)


def _take(
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
    key: tuple[str, ...],
    expected_type: type[CanonicalLockEntry],
) -> CanonicalLockEntry:
    entry = entries.get(key)
    if entry is None or not isinstance(entry, expected_type):
        raise ValueError(f"canonical lock is missing required identity {key!r}")
    used.add(key)
    return entry


def _package_group(
    requirements: list[str],
    group: Literal["application-extra", "pytorch"],
    python_version: str,
    platform: Literal["linux/amd64"],
    index_url: str,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
    *,
    package_channel: str | None,
) -> PackageGroupPlan:
    packages: list[ExactPackagePlan] = []
    for index, value in enumerate(requirements):
        diagnostics = []
        normalized = validate_direct_requirement(
            value, (group, "requirements", index), diagnostics
        )
        if normalized is None or diagnostics:
            raise ValueError("validated config contains an invalid package requirement")
        key = ("python-package", "application", normalized.name)
        entry = _take(entries, used, key, DirectPythonLockEntry)
        if entry.extras != list(normalized.extras) or not _selector_accepts(
            normalized.specifier, entry.version
        ):
            raise ValueError(f"canonical package does not satisfy {normalized.name}")
        if package_channel is not None and not pytorch_core_version_matches_channel(
            entry.package, entry.version, package_channel
        ):
            raise ValueError(
                f"canonical {entry.package} version does not match PyTorch channel"
            )
        packages.append(
            ExactPackagePlan(
                name=entry.package,
                extras=tuple(entry.extras),
                version=entry.version,
                environment="application",
            )
        )
    packages.sort(key=lambda item: (item.name != "torch", item.name))
    return PackageGroupPlan(
        group=group,
        python_version=python_version,
        platform=platform,
        index_url=index_url,
        packages=tuple(packages),
    )


def _uv_tool(
    requirement: str,
    python_version: str,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
) -> UvToolPlan:
    diagnostics = []
    normalized = validate_direct_requirement(
        requirement, ("python", "uv_tools"), diagnostics
    )
    if normalized is None or diagnostics:
        raise ValueError("validated config contains an invalid uv tool requirement")
    environment = f"uv-tool:{normalized.name}"
    entry = _take(
        entries,
        used,
        ("python-package", environment, normalized.name),
        DirectPythonLockEntry,
    )
    if entry.extras != list(normalized.extras) or not _selector_accepts(
        normalized.specifier, entry.version
    ):
        raise ValueError(f"canonical uv tool does not satisfy {normalized.name}")
    return UvToolPlan(
        name=entry.package,
        extras=tuple(entry.extras),
        version=entry.version,
        environment=environment,
    )


def _selector_accepts(selector: str, version: str) -> bool:
    return not selector or SpecifierSet(selector).contains(version, prereleases=False)


def _exact_distribution_version(value: str) -> str:
    try:
        version = Version(value)
    except InvalidVersion as error:
        raise ValueError(
            "version must be one exact stable distribution version"
        ) from error
    if str(version) != value or version.is_prerelease or version.is_devrelease:
        raise ValueError("version must be one exact stable distribution version")
    return value


def _custom_node(
    node: FinalRegistryCustomNodeConfig | FinalGitCustomNodeConfig,
    comfyui_path: str,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
) -> CustomNodePlan:
    pre = tuple(_hook(value, entries, used) for value in node.pre_install_scripts)
    post = tuple(_hook(value, entries, used) for value in node.post_install_scripts)
    if isinstance(node, FinalRegistryCustomNodeConfig):
        entry = _take(entries, used, ("registry", node.id), RegistryNodeLockEntry)
        selector = normalize_registry_version(node.version or "latest")
        if not _published_selector_accepts(selector, entry.version):
            raise ValueError("canonical Registry identity does not match final config")
        return RegistryNodePlan(
            type="registry",
            id=entry.id,
            version=entry.version,
            pre_install=pre,
            post_install=post,
        )
    entry = _take(entries, used, ("git", node.url), DirectGitLockEntry)
    if node.ref is not None and len(node.ref) == 40 and entry.commit != node.ref:
        raise ValueError("canonical Git identity does not match final config")
    target = resolve_git_target_dir(node.url, node.target_dir)
    return GitNodePlan(
        type="git",
        url=entry.url,
        commit=entry.commit,
        target=str(PurePosixPath(comfyui_path) / "custom_nodes" / target),
        pre_install=pre,
        post_install=post,
    )


def _hook(
    relative_path: str,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
) -> HookPlan:
    identity_path = f"{CUSTOM_NODE_HOOK_LOCK_PREFIX}/{relative_path}"
    entry = _take(
        entries,
        used,
        ("local-executable", identity_path),
        LocalExecutableLockEntry,
    )
    return HookPlan(relative_path=relative_path, digest=entry.digest)


def _runtime_hooks(
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
) -> tuple[HookPlan, ...]:
    prefix = f"{RUNTIME_HOOK_LOCK_PREFIX}/"
    hooks: list[HookPlan] = []
    phase_order = {
        directory: index
        for index, (_, directory) in enumerate(RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS)
    }
    runtime_keys = [
        key
        for key in entries
        if key[0] == "local-executable" and key[1].startswith(prefix)
    ]
    runtime_keys.sort(
        key=lambda key: (
            phase_order.get(key[1].removeprefix(prefix).split("/", 1)[0], 99),
            key[1],
        )
    )
    for key in runtime_keys:
        entry = _take(entries, used, key, LocalExecutableLockEntry)
        hooks.append(
            HookPlan(
                relative_path=entry.relative_path.removeprefix(prefix),
                digest=entry.digest,
            )
        )
    return tuple(hooks)


def _image_plan(entry: OciLockEntry) -> ImagePlan:
    return ImagePlan.model_validate(
        entry.model_dump(mode="python", exclude={"type", "request_digest"})
    )


def _comfyui_selector_accepts(selector: str, entry: OfficialComfyUILockEntry) -> bool:
    normalized = normalize_comfyui_version(selector)
    if len(normalized) == 40:
        return entry.commit == normalized
    if normalized == "nightly":
        return entry.formal_release is None
    if normalized == "latest":
        return entry.formal_release is not None
    if entry.formal_release is None:
        return False
    if any(character in normalized for character in "<>=!,"):
        return SpecifierSet(normalized).contains(
            Version(entry.formal_release), prereleases=False
        )
    return entry.formal_release == normalized


def _published_selector_accepts(selector: str, version: str) -> bool:
    if selector == "latest":
        return True
    if any(character in selector for character in "<>=!,"):
        return SpecifierSet(selector).contains(Version(version), prereleases=False)
    return selector == version


def _config_digest(config: FinalConfig) -> str:
    return _digest_bytes(_canonical_json(config.model_dump(mode="json")))


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
