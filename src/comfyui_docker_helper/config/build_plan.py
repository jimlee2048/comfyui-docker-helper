"""Immutable BuildPlan v1 authority and deterministic construction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Literal, get_args

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from comfyui_docker_helper.comfyui_requirements import protected_policy_digest
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    CanonicalLockEntry,
    ComfyCliLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    DirectPythonLockEntry,
    DirectPythonRequestIdentity,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciLockEntry,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    PyTorchCompatibilityLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    RegistryRequestIdentity,
    ResolverRequestIdentity,
    canonical_entry_key,
    dump_canonical_lock_toml,
    pytorch_core_version_matches_channel,
    pytorch_index_matches_channel,
    uv_image_version_matches_tag,
    validate_environment,
    validate_exact_registry_version,
    validate_exact_stable_distribution_version,
    validate_exact_stable_version,
    validate_git_commit,
    validate_git_url,
    validate_http_url,
    validate_normalized_extras,
    validate_normalized_package,
    validate_oci_repository,
    validate_oci_tag,
    validate_registry_id,
    validate_sha256_digest,
)
from comfyui_docker_helper.config.canonical_request import (
    CanonicalRequestGraph,
    CustomNodeRequest,
    GitNodeRequest,
    RegistryNodeRequest,
)
from comfyui_docker_helper.config.canonical_resolver import (
    entries_satisfy_request,
)
from comfyui_docker_helper.config.final_models import CudaImageDistro, CudaImageFlavor
from comfyui_docker_helper.config.final_planning import CudaBackendAdapter
from comfyui_docker_helper.config.final_validation import (
    is_aria2_argument_value,
    is_managed_environment_name,
)
from comfyui_docker_helper.config.os_packages import validate_apt_package_identity
from comfyui_docker_helper.config.runtime_hooks import (
    CUSTOM_NODE_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS,
)
from comfyui_docker_helper.config.selector_validation import resolve_git_target_dir
from comfyui_docker_helper.config.ssh_keys import normalize_ssh_public_keys
from comfyui_docker_helper.config.url_validation import is_http_url
from comfyui_docker_helper.config.value_validation import (
    has_control_characters,
    is_argv_value,
    validate_managed_python_catalog_key,
    validate_managed_python_support_range,
)
from comfyui_docker_helper.exact_ledger import (
    CDH_VERSION,
    COMFY_CLI_MINIMUM_VERSION,
    COMFYUI_FLOOR_COMMIT,
    COMFYUI_MINIMUM_VERSION,
    COMFYUI_REPOSITORY,
    CUDA_IMAGE_REPOSITORY,
    PIP_VERSION,
    UV_IMAGE_REPOSITORY,
    UV_VERSION,
)

BUILD_PLAN_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
_VENV_PATH = "/opt/venv"


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

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return validate_oci_repository(value)

    @field_validator("tag")
    @classmethod
    def _validate_tag(cls, value: str) -> str:
        return validate_oci_tag(value)

    @field_validator("descriptor_digest")
    @classmethod
    def _validate_descriptor_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)

    @field_validator("resolved_version")
    @classmethod
    def _validate_resolved_version(cls, value: str | None) -> str | None:
        return None if value is None else validate_exact_stable_version(value)

    @model_validator(mode="after")
    def _validate_role_version(self) -> ImagePlan:
        if (self.role == "uv-tool") != (self.resolved_version is not None):
            raise ValueError("only uv-tool images require a resolved version")
        if self.role == "uv-tool" and not uv_image_version_matches_tag(
            self.tag, self.resolved_version
        ):
            raise ValueError("uv image resolved version does not match its exact tag")
        expected_repository = (
            CUDA_IMAGE_REPOSITORY if self.role == "cuda-base" else UV_IMAGE_REPOSITORY
        )
        if self.repository != expected_repository:
            raise ValueError(f"{self.role} image repository does not match the ledger")
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

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return validate_managed_python_support_range(
            validate_exact_stable_version(value)
        )

    @field_validator("pip_version")
    @classmethod
    def _validate_pip_version(cls, value: str) -> str:
        return validate_exact_stable_version(value)

    @field_validator("cdh_version")
    @classmethod
    def _validate_cdh_version(cls, value: str) -> str:
        if validate_exact_stable_version(value) != CDH_VERSION:
            raise ValueError("cdh version does not match the exact ledger")
        return value

    @field_validator("uv_build_version")
    @classmethod
    def _validate_uv_build_version(cls, value: str) -> str:
        if validate_exact_stable_version(value) != UV_VERSION:
            raise ValueError("uv-build version does not match the exact ledger")
        return value

    @field_validator("catalog_descriptor_digest", "cdh_source_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)

    @field_validator("catalog_key")
    @classmethod
    def _validate_catalog_key(cls, value: str) -> str:
        return validate_managed_python_catalog_key(value)

    @field_validator("catalog_url")
    @classmethod
    def _validate_catalog_url(cls, value: str) -> str:
        return validate_http_url(value, "catalog_url")


class ExactDistributionPlan(_PlanModel):
    name: str
    version: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        try:
            return validate_normalized_package(value)
        except ValueError as error:
            raise ValueError("name must be one normalized distribution name") from error

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return validate_exact_stable_distribution_version(value)


class UvToolPlan(_PlanModel):
    name: str
    extras: tuple[str, ...]
    version: str
    environment: str

    @model_validator(mode="after")
    def _validate_identity(self) -> UvToolPlan:
        validate_normalized_package(self.name)
        validate_exact_stable_distribution_version(self.version)
        validate_normalized_extras(self.extras)
        validate_environment(self.environment)
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
        validate_sha256_digest(self.requirements_digest)
        return self


class ToolchainPhase(_PlanModel):
    platform: Literal["linux/amd64"]
    cuda_version: str
    pytorch_channel: str
    cuda_image: ImagePlan
    uv_image: ImagePlan
    python: ManagedPythonPlan
    tool_store: ToolStorePlan

    @field_validator("cuda_version")
    @classmethod
    def _validate_cuda_version(cls, value: str) -> str:
        return validate_exact_stable_version(value)

    @field_validator("pytorch_channel")
    @classmethod
    def _validate_channel(cls, value: str) -> str:
        if re.fullmatch(r"cu[0-9]+", value) is None:
            raise ValueError("PyTorch channel must be canonical")
        return value


class PathsPlan(_PlanModel):
    workspace: str
    comfyui: str
    venv: Literal["/opt/venv"]

    @field_validator("workspace", "comfyui", "venv")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _absolute_posix_path(value, "application path")

    @model_validator(mode="after")
    def _validate_distinct_roots(self) -> PathsPlan:
        if self.comfyui == self.workspace:
            raise ValueError("ComfyUI and workspace paths must be different")
        return self


class ExactPackagePlan(_PlanModel):
    name: str
    extras: tuple[str, ...]
    version: str
    environment: Literal["application"]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        try:
            validate_normalized_package(value)
        except ValueError as error:
            raise ValueError(
                "package name must be normalized distribution name"
            ) from error
        if value == "comfy-cli":
            raise ValueError("comfy-cli is reserved to the dedicated optional tool")
        return value

    @field_validator("extras")
    @classmethod
    def _validate_extras(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            return validate_normalized_extras(value)
        except ValueError as error:
            raise ValueError(
                "package extras must be sorted, unique, and normalized"
            ) from error

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return validate_exact_stable_distribution_version(value)

    @property
    def requirement(self) -> str:
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        return f"{self.name}{extras}=={self.version}"


class PackageGroupPlan(_PlanModel):
    group: Literal["application-extra"]
    python_version: str
    platform: Literal["linux/amd64"]
    index_url: str
    packages: tuple[ExactPackagePlan, ...]

    @model_validator(mode="after")
    def _validate_group_identity(self) -> PackageGroupPlan:
        names = tuple(package.name for package in self.packages)
        if len(names) != len(set(names)):
            raise ValueError("application-extra packages must be unique")
        if names != tuple(sorted(names)):
            raise ValueError("application-extra packages must be canonically ordered")
        if not is_http_url(self.index_url):
            raise ValueError("application-extra index must be one HTTP(S) URL")
        return self


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
        if {"pip", "setuptools"}.intersection(names):
            raise ValueError("PyTorch packages overlap application package owners")
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

    @field_validator("package")
    @classmethod
    def _validate_package(cls, value: str) -> str:
        return validate_normalized_package(value)

    @field_validator("extras")
    @classmethod
    def _validate_extras(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_normalized_extras(value)

    @field_validator("selector")
    @classmethod
    def _validate_selector(cls, value: str) -> str:
        parsed = SpecifierSet(value)
        if str(parsed) != value:
            raise ValueError("protected selector must be canonical")
        return value


class ComfyUIRequirementsPlan(_PlanModel):
    path: Literal["requirements.txt"]
    floor_commit: str
    python_version: str
    platform: Literal["linux/amd64"]
    protected_names: tuple[str, ...]
    protected_policy_digest: str
    digest: str
    protected: tuple[ProtectedRequirementPlan, ...]

    @field_validator("floor_commit")
    @classmethod
    def _validate_floor_commit(cls, value: str) -> str:
        return validate_git_commit(value)

    @field_validator("python_version")
    @classmethod
    def _validate_python_version(cls, value: str) -> str:
        return validate_exact_stable_version(value)

    @field_validator("protected_policy_digest", "digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)

    @field_validator("protected_names")
    @classmethod
    def _validate_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(validate_normalized_package(item) for item in value)
        if names != tuple(sorted(set(names))):
            raise ValueError("protected names must be sorted and unique")
        return names

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


class ManagerCapabilityPlan(_PlanModel):
    """Checkout-owned Manager capability in the application environment."""

    requirements_path: Literal["manager_requirements.txt"]
    distribution: Literal["comfyui-manager"]
    import_name: Literal["comfyui_manager"]
    executable: Literal["/opt/venv/bin/cm-cli"]
    entrypoint_name: Literal["cm-cli"]
    import_anchor: str

    @field_validator("import_anchor")
    @classmethod
    def _validate_import_anchor(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or path.name != "comfyui-docker-helper-comfyui.pth"
            or "site-packages" not in path.parts
            or path.as_posix() != value
        ):
            raise ValueError("Manager import anchor must be one application site path")
        return value


class ComfyUIPlan(_PlanModel):
    repository: str
    commit: str
    floor_commit: str
    formal_release: str | None
    requirements: ComfyUIRequirementsPlan
    manager: ManagerCapabilityPlan | None

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return validate_git_url(value)

    @field_validator("commit", "floor_commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return validate_git_commit(value)

    @field_validator("formal_release")
    @classmethod
    def _validate_formal_release(cls, value: str | None) -> str | None:
        return None if value is None else validate_exact_stable_version(value)

    @field_validator("floor_commit")
    @classmethod
    def _validate_floor_commit(cls, value: str) -> str:
        if value != COMFYUI_FLOOR_COMMIT:
            raise ValueError("ComfyUI floor commit does not match the exact ledger")
        return value

    @model_validator(mode="after")
    def _validate_official_release_authority(self) -> ComfyUIPlan:
        if self.repository != COMFYUI_REPOSITORY:
            raise ValueError("ComfyUI repository does not match the official ledger")
        if self.formal_release is not None and Version(self.formal_release) < Version(
            COMFYUI_MINIMUM_VERSION
        ):
            raise ValueError("ComfyUI formal release is below the supported floor")
        return self


class ApplicationPhase(_PlanModel):
    paths: PathsPlan
    os_packages: tuple[str, ...]
    python_index_url: str
    pip_version: str
    inventory_path: Literal["/opt/cdh/build/application-inventory.txt"]
    python_extras: PackageGroupPlan | None
    pytorch: PyTorchGroupPlan
    comfyui: ComfyUIPlan

    @field_validator("os_packages")
    @classmethod
    def _validate_os_packages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        packages = tuple(validate_apt_package_identity(item) for item in value)
        if len(packages) != len(set(packages)):
            raise ValueError("OS packages must contain unique canonical identities")
        return packages

    @model_validator(mode="after")
    def _validate_python_sources(self) -> ApplicationPhase:
        _exact_distribution_version(self.pip_version)
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
        python_names = (
            set()
            if self.python_extras is None
            else {package.name for package in self.python_extras.packages}
        )
        protected_names = {package.name for package in self.pytorch.packages}
        overlap = python_names.intersection({*protected_names, "pip", "setuptools"})
        if overlap:
            raise ValueError(
                "application Python extras overlap protected package owners: "
                f"{sorted(overlap)!r}"
            )
        requirements = self.comfyui.requirements
        if (
            requirements.python_version != self.pytorch.python_version
            or requirements.platform != self.pytorch.platform
            or requirements.floor_commit != self.comfyui.floor_commit
        ):
            raise ValueError("ComfyUI requirements target must match PyTorch")
        adapter_protected_names = CudaBackendAdapter().protected_requirement_names
        if requirements.protected_names != adapter_protected_names:
            raise ValueError("ComfyUI protected names do not match the backend adapter")
        if requirements.protected_policy_digest != protected_policy_digest(
            adapter_protected_names
        ):
            raise ValueError(
                "ComfyUI protected policy digest does not match the backend adapter"
            )
        resolved_pytorch_names = {package.name for package in self.pytorch.packages}
        unresolved = {item.package for item in requirements.protected}.difference(
            resolved_pytorch_names
        )
        if unresolved:
            raise ValueError(
                "ComfyUI protected requirements are missing exact PyTorch results: "
                f"{sorted(unresolved)!r}"
            )
        manager = self.comfyui.manager
        if manager is not None:
            python_minor = ".".join(self.pytorch.python_version.split(".")[:2])
            expected_anchor = (
                f"{self.paths.venv}/lib/python{python_minor}/site-packages/"
                "comfyui-docker-helper-comfyui.pth"
            )
            if manager.import_anchor != expected_anchor:
                raise ValueError("Manager import anchor does not match target Python")
        return self


class HookPlan(_PlanModel):
    relative_path: str
    digest: str

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)

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

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_registry_id(value)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return validate_exact_registry_version(value)


class GitNodePlan(_PlanModel):
    type: Literal["git"]
    url: str
    commit: str
    target: str
    pre_install: tuple[HookPlan, ...]
    post_install: tuple[HookPlan, ...]

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return validate_git_url(value)

    @field_validator("commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return validate_git_commit(value)

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _absolute_posix_path(value, "Git target")


CustomNodePlan = Annotated[RegistryNodePlan | GitNodePlan, Field(discriminator="type")]


class CustomNodesPhase(_PlanModel):
    install_manager: bool
    user_directory: str
    custom_node_inventory: Literal["/opt/cdh/build/custom-node-inventory.json"]
    nodes: tuple[CustomNodePlan, ...]

    @field_validator("user_directory")
    @classmethod
    def _validate_user_directory(cls, value: str) -> str:
        return _absolute_posix_path(value, "Registry user directory")


class Aria2Plan(_PlanModel):
    rpc_port: int = Field(ge=1, le=65535)
    split: int = Field(ge=1)
    max_connection_per_server: int = Field(ge=1)
    min_split_size: str
    resume_download: bool

    @field_validator("min_split_size")
    @classmethod
    def _validate_min_split_size(cls, value: str) -> str:
        if not is_aria2_argument_value(value):
            raise ValueError("min_split_size must be one canonical aria2 argument")
        return value


class HttpxPlan(_PlanModel):
    timeout: int | float = Field(gt=0)
    retries: int = Field(ge=0)


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

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return validate_http_url(value, "file URL")

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _absolute_posix_path(value, "file target")


class FilesPhase(_PlanModel):
    downloader: DownloaderPlan
    default_download_mode: Literal["sync", "async"]
    download_max_attempts: int = Field(ge=1)
    download_failure_policy: Literal["continue", "fail"]
    files: tuple[FilePlan, ...]


class EnvironmentPlan(_PlanModel):
    name: str
    value: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
            raise ValueError("environment name must be canonical")
        if is_managed_environment_name(value):
            raise ValueError("environment name is reserved to cdh image authority")
        return value

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        if has_control_characters(value):
            raise ValueError("environment value must not contain controls")
        return value


class SshPlan(_PlanModel):
    enable: bool
    port: int = Field(ge=1, le=65535)
    password: str
    pub_keys: tuple[str, ...]

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        if has_control_characters(value):
            raise ValueError("SSH password must not contain control characters")
        return value

    @field_validator("pub_keys")
    @classmethod
    def _validate_pub_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized, diagnostics = normalize_ssh_public_keys(
            list(value),
            path=("runtime", "ssh", "pub_keys"),
            code="ssh.invalid_public_key",
        )
        if diagnostics or normalized != value:
            raise ValueError("SSH public keys must be canonical and unique")
        return value


class RuntimePhase(_PlanModel):
    environment: tuple[EnvironmentPlan, ...]
    ssh: SshPlan
    launch_command: tuple[str, ...]
    hooks: tuple[HookPlan, ...]
    download_failure_policy: Literal["continue", "fail"] | None

    @field_validator("launch_command")
    @classmethod
    def _validate_launch_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not is_argv_value(item) for item in value):
            raise ValueError("launch command must contain canonical argv values")
        _absolute_posix_path(value[0], "launch executable")
        return value


class BuildOutputPlan(_PlanModel):
    tags: tuple[str, ...]
    output: Literal["load", "push"]
    platforms: tuple[Literal["linux/amd64"], ...]

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not tag
            or any(character.isspace() for character in tag)
            or has_control_characters(tag)
            for tag in value
        ):
            raise ValueError("build tags must contain no whitespace or controls")
        return value


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

    @field_validator("config_digest", "lock_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)

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
        if (
            self.application.pip_version != PIP_VERSION
            or self.toolchain.python.pip_version != PIP_VERSION
        ):
            raise ValueError("pip version does not match the exact ledger")
        if (
            self.toolchain.python.catalog_descriptor_digest
            != self.toolchain.uv_image.descriptor_digest
        ):
            raise ValueError("managed Python catalog is not bound to the uv image")
        if bool(self.application.comfyui.manager) != self.custom_nodes.install_manager:
            raise ValueError("Manager capability does not match custom-node intent")
        expected_user_directory = str(
            PurePosixPath(self.application.paths.comfyui) / "user"
        )
        if self.custom_nodes.user_directory != expected_user_directory:
            raise ValueError("Registry user directory does not match ComfyUI")
        expected_channel = _pytorch_channel(self.toolchain.cuda_version)
        expected_cuda_tag = _cuda_image_tag(
            self.toolchain.cuda_version,
            self.toolchain.cuda_image.tag,
        )
        if (
            self.toolchain.pytorch_channel != expected_channel
            or self.toolchain.cuda_image.tag != expected_cuda_tag
        ):
            raise ValueError(
                "CUDA image tag and PyTorch channel do not match toolchain"
            )

        custom_nodes_root = (
            PurePosixPath(self.application.paths.comfyui) / "custom_nodes"
        )
        git_targets: list[str] = []
        for node in self.custom_nodes.nodes:
            if not isinstance(node, GitNodePlan):
                continue
            target = PurePosixPath(node.target)
            target_name = resolve_git_target_dir(node.url, target.name)
            if target != custom_nodes_root / target_name:
                raise ValueError(
                    "Git node target must be one exact child of ComfyUI custom_nodes"
                )
            git_targets.append(node.target)
        if len(git_targets) != len(set(git_targets)):
            raise ValueError("Git node targets must be unique")

        comfyui_root = PurePosixPath(self.application.paths.comfyui)
        file_targets = tuple(PurePosixPath(item.target) for item in self.files.files)
        if any(
            target == comfyui_root or not target.is_relative_to(comfyui_root)
            for target in file_targets
        ):
            raise ValueError("file targets must be strict descendants of ComfyUI")
        if len(file_targets) != len(set(file_targets)):
            raise ValueError("file targets must be unique")
        expected_launch_head = (
            str(PurePosixPath(self.application.paths.venv) / "bin" / "python"),
            str(PurePosixPath(self.application.paths.comfyui) / "main.py"),
        )
        if self.runtime.launch_command[:2] != expected_launch_head:
            raise ValueError(
                "runtime launch executable and script must match the application"
            )
        return self


class ManifestBinding(_PlanModel):
    """Stable final-verification binding without observed evidence or timestamps."""

    schema_version: Literal[1]
    build_plan_schema_version: Literal[1]
    build_plan_digest: str
    config_digest: str
    lock_digest: str

    @field_validator("build_plan_digest", "config_digest", "lock_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)


@dataclass(frozen=True, slots=True)
class RuntimePlanningProvenance:
    """Authorship needed when projecting host config into runtime config."""

    failure_policy_explicit: bool
    file_downloader_explicit: tuple[bool, ...]
    file_download_mode_explicit: tuple[bool, ...]

    def __post_init__(self) -> None:
        if type(self.failure_policy_explicit) is not bool:
            raise TypeError("failure-policy provenance must be one bool")
        for field_name, values in (
            ("file downloader", self.file_downloader_explicit),
            ("file download-mode", self.file_download_mode_explicit),
        ):
            if not isinstance(values, tuple) or any(
                type(value) is not bool for value in values
            ):
                raise TypeError(f"{field_name} provenance must be one bool tuple")


@dataclass(frozen=True, slots=True)
class _ResolvedPackageGroup:
    """Internal exact package tuple before projection to one public owner type."""

    python_version: str
    platform: Literal["linux/amd64"]
    index_url: str
    packages: tuple[ExactPackagePlan, ...]


def construct_build_plan(
    graph: CanonicalRequestGraph,
    lock: CanonicalLock,
    *,
    runtime_provenance: RuntimePlanningProvenance,
) -> BuildPlan:
    """Construct BuildPlan once from the shared request graph and accepted lock."""
    entries = {canonical_entry_key(entry): entry for entry in lock.entries}
    if len(entries) != len(lock.entries):
        raise ValueError("canonical lock contains duplicate logical identities")
    _validate_lock_satisfies_graph(graph, entries)
    used: set[tuple[str, ...]] = set()
    toolchain = _project_toolchain(graph, entries, used)
    application = _project_application(graph, entries, used, toolchain)
    custom_nodes = _project_custom_nodes(graph, entries, used)
    files = _project_files(graph, runtime_provenance)
    runtime = _project_runtime(graph, entries, used, runtime_provenance)
    unused = sorted(set(entries) - used)
    if unused:
        raise ValueError(f"canonical lock contains unused identities: {unused!r}")
    return BuildPlan(
        schema_version=BUILD_PLAN_SCHEMA_VERSION,
        config_digest=graph.config_digest,
        lock_digest=_digest_bytes(dump_canonical_lock_toml(lock).encode("utf-8")),
        build=BuildOutputPlan(
            tags=graph.build.tags,
            output=graph.build.output,
            platforms=graph.build.platforms,
        ),
        toolchain=toolchain,
        application=application,
        custom_nodes=custom_nodes,
        files=files,
        runtime=runtime,
    )


def _validate_lock_satisfies_graph(
    graph: CanonicalRequestGraph,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
) -> None:
    for desired in graph.desired:
        resolved = tuple(entries[key] for key in desired.keys if key in entries)
        if isinstance(desired.request, PyTorchRequestIdentity) and any(
            getattr(entry, "request_digest", None) != desired.request_digest
            for entry in resolved
        ):
            raise ValueError(
                "canonical PyTorch packages and compatibility policy "
                "must share one request digest"
            )
        if isinstance(desired.request, PyTorchRequestIdentity) and any(
            isinstance(entry, DirectPythonLockEntry)
            and not pytorch_core_version_matches_channel(
                entry.package, entry.version, desired.request.channel
            )
            for entry in resolved
        ):
            raise ValueError("canonical core package does not match PyTorch channel")
        if len(resolved) != len(desired.keys) or not entries_satisfy_request(
            desired.request,
            resolved,
            desired.request_digest,
            desired.managed_python_release,
        ):
            raise ValueError(_request_mismatch_message(desired.request))


def _request_mismatch_message(request: ResolverRequestIdentity) -> str:
    if isinstance(request, OciRequestIdentity):
        owner = "CUDA image" if request.role == "cuda-base" else "uv image"
        return f"canonical {owner} does not match the request graph"
    if isinstance(request, ManagedPythonRequestIdentity):
        return "canonical managed Python does not match the request graph"
    if isinstance(request, ComfyUIRequestIdentity):
        return "canonical ComfyUI identity does not match the request graph"
    if isinstance(request, ComfyCliRequestIdentity):
        return "canonical comfy-cli identity does not match the request graph"
    if isinstance(request, RegistryRequestIdentity):
        return "canonical Registry identity does not match the request graph"
    if isinstance(request, DirectGitRequestIdentity):
        return "canonical Git identity does not match the request graph"
    if isinstance(request, (DirectPythonRequestIdentity, PyTorchRequestIdentity)):
        names = ", ".join(member.package for member in request.members)
        return f"canonical package does not satisfy {names}"
    return "canonical lock does not satisfy the request graph"


def _project_toolchain(
    graph: CanonicalRequestGraph,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
) -> ToolchainPhase:
    cuda_entry = _take(entries, used, ("oci", "cuda-base"), OciLockEntry)
    uv_entry = _take(entries, used, ("oci", "uv-tool"), OciLockEntry)
    python_entry = _take(
        entries,
        used,
        ("managed-python", "cpython", graph.target_platform.value),
        ManagedPythonLockEntry,
    )
    if python_entry.catalog_descriptor_digest != uv_entry.descriptor_digest:
        raise ValueError("managed Python catalog is not bound to the uv image")
    comfy_cli = None
    cli_key = ("comfy-cli", "comfy-cli", "uv-tool:comfy-cli")
    if any(cli_key in item.keys for item in graph.desired):
        cli_entry = _take(entries, used, cli_key, ComfyCliLockEntry)
        comfy_cli = ComfyCliToolPlan(
            name=cli_entry.package,
            version=cli_entry.version,
            environment=cli_entry.environment,
            executables=("comfy", "comfy-cli", "comfycli"),
            inventory_path="/opt/cdh/build/comfy-cli-inventory.txt",
        )
    uv_tools = tuple(
        _uv_tool(request, entries, used)
        for request in (
            item.request
            for item in graph.desired
            if isinstance(item.request, DirectPythonRequestIdentity)
            and item.request.group == "uv-tool"
        )
    )
    return ToolchainPhase(
        platform=graph.target_platform.value,
        cuda_version=graph.backend.version.value,
        pytorch_channel=graph.backend.package_channel,
        cuda_image=_image_plan(cuda_entry),
        uv_image=_image_plan(uv_entry),
        python=ManagedPythonPlan.model_validate(
            python_entry.model_dump(mode="python", exclude={"type", "request_digest"})
        ),
        tool_store=ToolStorePlan(
            tool_dir="/opt/uv/tools",
            bin_dir="/opt/uv/bin",
            cdh_environment="/opt/uv/tools/comfyui-docker-helper",
            cdh_executable="/opt/uv/bin/cdh",
            requirements_digest=graph.release.requirements_digest,
            cdh_closure=tuple(
                ExactDistributionPlan(name=name, version=version)
                for name, version in graph.release.cdh_closure
            ),
            comfy_cli=comfy_cli,
            uv_tools=uv_tools,
        ),
    )


def _project_application(
    graph: CanonicalRequestGraph,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
    toolchain: ToolchainPhase,
) -> ApplicationPhase:
    comfyui_request = next(
        item
        for item in graph.desired
        if isinstance(item.request, ComfyUIRequestIdentity)
    )
    requirements_request = next(
        item
        for item in graph.desired
        if isinstance(item.request, ComfyUIRequirementsRequestIdentity)
    )
    pytorch_request = next(
        item.request
        for item in graph.desired
        if isinstance(item.request, PyTorchRequestIdentity)
    )
    python_request = next(
        (
            item.request
            for item in graph.desired
            if isinstance(item.request, DirectPythonRequestIdentity)
            and item.request.group == "application-extra"
        ),
        None,
    )
    comfyui_entry = _take(
        entries, used, comfyui_request.keys[0], OfficialComfyUILockEntry
    )
    requirements_entry = _take(
        entries,
        used,
        requirements_request.keys[0],
        ComfyUIRequirementsLockEntry,
    )
    python_packages = (
        _package_group(python_request, entries, used, package_channel=None)
        if python_request is not None
        else None
    )
    pytorch_packages = _package_group(
        pytorch_request,
        entries,
        used,
        package_channel=pytorch_request.channel,
    )
    compatibility = _take(
        entries,
        used,
        ("pytorch-compatibility", "application"),
        PyTorchCompatibilityLockEntry,
    )
    manager = None
    if graph.application.install_manager:
        python_minor = ".".join(pytorch_request.python_version.split(".")[:2])
        manager = ManagerCapabilityPlan(
            requirements_path="manager_requirements.txt",
            distribution="comfyui-manager",
            import_name="comfyui_manager",
            executable="/opt/venv/bin/cm-cli",
            entrypoint_name="cm-cli",
            import_anchor=(
                f"{_VENV_PATH}/lib/python{python_minor}/site-packages/"
                "comfyui-docker-helper-comfyui.pth"
            ),
        )
    return ApplicationPhase(
        paths=PathsPlan(
            workspace=graph.application.workspace,
            comfyui=graph.application.comfyui_path,
            venv=_VENV_PATH,
        ),
        os_packages=graph.application.os_packages,
        python_index_url=graph.application.python_index_url,
        pip_version=toolchain.python.pip_version,
        inventory_path="/opt/cdh/build/application-inventory.txt",
        python_extras=(
            PackageGroupPlan(
                group="application-extra",
                python_version=python_packages.python_version,
                platform=python_packages.platform,
                index_url=python_packages.index_url,
                packages=python_packages.packages,
            )
            if python_packages is not None and python_packages.packages
            else None
        ),
        pytorch=PyTorchGroupPlan(
            group="pytorch",
            backend="cuda",
            channel=pytorch_request.channel,
            python_version=pytorch_packages.python_version,
            platform=pytorch_packages.platform,
            python_index_url=pytorch_request.python_index_url,
            pytorch_index_url=pytorch_request.pytorch_index_url,
            packages=pytorch_packages.packages,
            setuptools_specifier=compatibility.setuptools_specifier,
        ),
        comfyui=ComfyUIPlan(
            repository=comfyui_entry.repository,
            commit=comfyui_entry.commit,
            floor_commit=requirements_entry.floor_commit,
            formal_release=comfyui_entry.formal_release,
            requirements=ComfyUIRequirementsPlan(
                path=requirements_entry.path,
                floor_commit=requirements_entry.floor_commit,
                python_version=requirements_entry.python_version,
                platform=requirements_entry.platform,
                protected_names=requirements_entry.protected_names,
                protected_policy_digest=requirements_entry.protected_policy_digest,
                digest=requirements_entry.requirements_digest,
                protected=tuple(
                    ProtectedRequirementPlan(
                        package=item.package,
                        extras=item.extras,
                        selector=item.selector,
                    )
                    for item in requirements_entry.protected
                ),
            ),
            manager=manager,
        ),
    )


def _project_custom_nodes(
    graph: CanonicalRequestGraph,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
) -> CustomNodesPhase:
    nodes = tuple(_custom_node(node, entries, used) for node in graph.custom_nodes)
    return CustomNodesPhase(
        install_manager=graph.application.install_manager,
        user_directory=str(PurePosixPath(graph.application.comfyui_path) / "user"),
        custom_node_inventory="/opt/cdh/build/custom-node-inventory.json",
        nodes=nodes,
    )


def _project_files(
    graph: CanonicalRequestGraph,
    provenance: RuntimePlanningProvenance,
) -> FilesPhase:
    if len(provenance.file_downloader_explicit) != len(graph.files):
        raise ValueError("runtime file downloader provenance does not match config")
    if len(provenance.file_download_mode_explicit) != len(graph.files):
        raise ValueError("runtime file download-mode provenance does not match config")
    downloader_explicit = provenance.file_downloader_explicit
    mode_explicit = provenance.file_download_mode_explicit
    request = graph.downloader
    return FilesPhase(
        downloader=DownloaderPlan(
            default=request.default,
            aria2=Aria2Plan(
                rpc_port=request.aria2_rpc_port,
                split=request.aria2_split,
                max_connection_per_server=request.aria2_max_connection_per_server,
                min_split_size=request.aria2_min_split_size,
                resume_download=request.aria2_resume_download,
            ),
            httpx=HttpxPlan(
                timeout=request.httpx_timeout,
                retries=request.httpx_retries,
            ),
        ),
        default_download_mode=request.default_download_mode,
        download_max_attempts=request.download_max_attempts,
        download_failure_policy=request.download_failure_policy,
        files=tuple(
            FilePlan(
                url=item.url,
                target=item.target,
                overwrite=item.overwrite,
                downloader=item.downloader,
                download_mode=item.download_mode,
                downloader_explicit=downloader_explicit[index],
                download_mode_explicit=mode_explicit[index],
            )
            for index, item in enumerate(graph.files)
        ),
    )


def _project_runtime(
    graph: CanonicalRequestGraph,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
    provenance: RuntimePlanningProvenance,
) -> RuntimePhase:
    return RuntimePhase(
        environment=tuple(
            EnvironmentPlan(name=name, value=value)
            for name, value in graph.runtime.environment
        ),
        ssh=SshPlan(
            enable=graph.runtime.ssh.enable,
            port=graph.runtime.ssh.port,
            password=graph.runtime.ssh.password,
            pub_keys=graph.runtime.ssh.pub_keys,
        ),
        launch_command=graph.runtime.launch_command,
        hooks=_runtime_hooks(entries, used),
        download_failure_policy=(
            graph.downloader.download_failure_policy
            if provenance.failure_policy_explicit
            else None
        ),
    )


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
    request: DirectPythonRequestIdentity | PyTorchRequestIdentity,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
    *,
    package_channel: str | None,
) -> _ResolvedPackageGroup:
    packages: list[ExactPackagePlan] = []
    for member in request.members:
        key = ("python-package", request.environment, member.package)
        entry = _take(entries, used, key, DirectPythonLockEntry)
        if entry.extras != member.extras or not _selector_accepts(
            member.selector, entry.version
        ):
            raise ValueError(f"canonical package does not satisfy {member.package}")
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
    return _ResolvedPackageGroup(
        python_version=request.python_version,
        platform=request.platform,
        index_url=(
            request.pytorch_index_url
            if isinstance(request, PyTorchRequestIdentity)
            else request.index_url
        ),
        packages=tuple(packages),
    )


def _uv_tool(
    request: DirectPythonRequestIdentity,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
) -> UvToolPlan:
    member = request.members[0]
    entry = _take(
        entries,
        used,
        ("python-package", request.environment, member.package),
        DirectPythonLockEntry,
    )
    if entry.extras != member.extras or not _selector_accepts(
        member.selector, entry.version
    ):
        raise ValueError(f"canonical uv tool does not satisfy {member.package}")
    return UvToolPlan(
        name=entry.package,
        extras=entry.extras,
        version=entry.version,
        environment=request.environment,
    )


def _selector_accepts(selector: str, version: str) -> bool:
    return not selector or SpecifierSet(selector).contains(version, prereleases=False)


def _absolute_posix_path(value: str, field: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or has_control_characters(value)
        or not path.is_absolute()
        or path.as_posix() != value
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{field} must be one canonical absolute POSIX path")
    return value


def _pytorch_channel(cuda_version: str) -> str:
    major, minor, *_ = cuda_version.split(".")
    return f"cu{major}{minor}"


def _cuda_image_tag(cuda_version: str, tag: str) -> str:
    accepted = {
        f"{cuda_version}-{flavor}-{distro}"
        for flavor in get_args(CudaImageFlavor)
        for distro in get_args(CudaImageDistro)
    }
    if tag not in accepted:
        raise ValueError(
            "CUDA image tag must match version, flavor, and distro authority"
        )
    return tag


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
    node: CustomNodeRequest,
    entries: dict[tuple[str, ...], CanonicalLockEntry],
    used: set[tuple[str, ...]],
) -> CustomNodePlan:
    pre = tuple(_hook(value, entries, used) for value in node.pre_install)
    post = tuple(_hook(value, entries, used) for value in node.post_install)
    if isinstance(node, RegistryNodeRequest):
        entry = _take(entries, used, ("registry", node.id), RegistryNodeLockEntry)
        return RegistryNodePlan(
            type="registry",
            id=entry.id,
            version=entry.version,
            pre_install=pre,
            post_install=post,
        )
    if not isinstance(node, GitNodeRequest):  # pragma: no cover - closed union
        raise AssertionError("unsupported canonical custom-node request")
    entry = _take(entries, used, ("git", node.url), DirectGitLockEntry)
    return GitNodePlan(
        type="git",
        url=entry.url,
        commit=entry.commit,
        target=node.target,
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


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
