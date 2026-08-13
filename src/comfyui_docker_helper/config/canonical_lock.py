"""Canonical config-lock schema v1 and resolver request identities."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import tomli_w
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from comfyui_docker_helper.config.final_validation import (
    is_git_ref,
    is_git_source_url,
    is_oci_tag,
)
from comfyui_docker_helper.config.registry_identity import (
    registry_distribution_identity,
)
from comfyui_docker_helper.config.registry_identity import (
    validate_registry_id as validate_registry_resource_id,
)
from comfyui_docker_helper.config.requirement_validation import (
    parse_direct_requirement,
)
from comfyui_docker_helper.config.selector_validation import (
    normalize_comfyui_version,
    normalize_registry_version,
)
from comfyui_docker_helper.config.url_validation import is_http_url
from comfyui_docker_helper.config.value_validation import (
    has_control_characters,
    validate_managed_python_catalog_key,
)
from comfyui_docker_helper.exact_ledger import (
    COMFY_CLI_MINIMUM_VERSION,
    COMFYUI_MINIMUM_VERSION,
)

CANONICAL_LOCK_SCHEMA_VERSION = 1
MAX_COMFYUI_REQUIREMENTS_BYTES = 1024 * 1024
INVALID_CANONICAL_LOCK_MESSAGE = (
    "config.lock.toml is invalid; remove it and regenerate the build context"
)

_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_PACKAGE_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_EXTRA_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_EXACT_STABLE_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
_CUDA_CHANNEL_PATTERN = re.compile(r"cu[0-9]+\Z")
_OCI_REPOSITORY_PATTERN = re.compile(
    r"(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?/)?"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z"
)
_ENVIRONMENT_PATTERN = re.compile(r"(?:application|uv-tool:[a-z0-9]+(?:-[a-z0-9]+)*)\Z")


class CanonicalLockError(ValueError):
    """One stable invalid-lock error for every parse or schema failure."""


class _StrictLockModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


class _ResolverEntry(_StrictLockModel):
    request_digest: str

    @field_validator("request_digest")
    @classmethod
    def _validate_request_digest(cls, value: str) -> str:
        return _require_sha256(value)


class ProtectedRequirementProjection(_StrictLockModel):
    """One target-active normalized protected requirement."""

    package: str
    extras: tuple[str, ...]
    selector: str

    @field_validator("package")
    @classmethod
    def _validate_package(cls, value: str) -> str:
        return _require_normalized_package(value)

    @field_validator("extras", mode="before")
    @classmethod
    def _validate_extras(cls, value: object) -> tuple[str, ...]:
        return _require_normalized_extras(_require_tuple(value, "extras"))

    @field_validator("selector")
    @classmethod
    def _validate_selector(cls, value: str) -> str:
        return _require_direct_package_specifier(value)


class OciLockEntry(_ResolverEntry):
    """Common immutable OCI image result stored in one fixed image domain."""

    repository: str = Field(min_length=1)
    tag: str = Field(min_length=1)
    digest: str
    kind: Literal["index", "manifest"]
    platform: Literal["linux/amd64"]

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return _require_oci_repository(value)

    @field_validator("tag")
    @classmethod
    def _validate_tag(cls, value: str) -> str:
        return _require_oci_tag(value)


class CudaImageLockEntry(OciLockEntry):
    """Exact CUDA base-image descriptor."""


class UvImageLockEntry(OciLockEntry):
    """Exact uv image descriptor plus independently observed uv version."""

    observed_version: str = Field(min_length=1)

    @field_validator("observed_version")
    @classmethod
    def _validate_observed_version(cls, value: str) -> str:
        return _require_exact_stable_version(value)


class ManagedPythonLockEntry(_ResolverEntry):
    """Exact uv-managed CPython artifact result."""

    version: str = Field(min_length=1)
    platform: Literal["linux/amd64"]
    libc: Literal["gnu"]
    catalog_digest: str
    artifact_key: str = Field(min_length=1)
    artifact_url: str = Field(min_length=1)

    @field_validator("catalog_digest")
    @classmethod
    def _validate_content_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("version")
    @classmethod
    def _validate_exact_stable_version(cls, value: str) -> str:
        return _require_exact_stable_version(value)

    @field_validator("artifact_key")
    @classmethod
    def _validate_artifact_key(cls, value: str) -> str:
        return validate_managed_python_catalog_key(value)

    @field_validator("artifact_url")
    @classmethod
    def _validate_artifact_url(cls, value: str) -> str:
        return _require_http_url(value, "artifact_url")


class OfficialComfyUILockEntry(_ResolverEntry):
    """Exact official ComfyUI source selection."""

    repository: str = Field(min_length=1)
    commit: str
    formal_release: str | None = None

    @field_validator("commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return _require_commit(value)

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return _require_git_url(value)

    @field_validator("formal_release")
    @classmethod
    def _validate_formal_release(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            normalized = normalize_comfyui_version(value)
        except ValueError as error:
            raise ValueError(
                "formal_release must be a stable ComfyUI "
                f"{COMFYUI_MINIMUM_VERSION}+ release"
            ) from error
        if (
            normalized != value
            or value in {"latest", "nightly"}
            or any(character in value for character in "<>=!,")
            or _EXACT_STABLE_PATTERN.fullmatch(value) is None
            or Version(value) < Version(COMFYUI_MINIMUM_VERSION)
        ):
            raise ValueError(
                "formal_release must be a stable ComfyUI "
                f"{COMFYUI_MINIMUM_VERSION}+ release"
            )
        return value


class ComfyUIRequirementsLockEntry(_ResolverEntry):
    """Exact bounded UTF-8 requirements source snapshot."""

    digest: str
    content: str

    @model_validator(mode="after")
    def _validate_source(self) -> ComfyUIRequirementsLockEntry:
        try:
            content = self.content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("requirements content must be strict UTF-8") from error
        if len(content) > MAX_COMFYUI_REQUIREMENTS_BYTES:
            raise ValueError("requirements content exceeds the supported size")
        expected = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if self.digest != expected:
            raise ValueError("requirements content digest does not match content")
        return self


class RegistryNodeLockEntry(_ResolverEntry):
    """Minimal exact Registry node identity."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _require_registry_id(value)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _require_exact_registry_version(value)


class DirectGitLockEntry(_ResolverEntry):
    """Minimal exact direct-Git root identity."""

    url: str = Field(min_length=1)
    commit: str

    @field_validator("commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return _require_commit(value)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _require_git_url(value)


class ResolvedPythonPackage(_StrictLockModel):
    """One exact top-level package result inside its owning atomic group."""

    name: str
    extras: tuple[str, ...]
    version: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _require_normalized_package(value)

    @field_validator("extras", mode="before")
    @classmethod
    def _validate_extras(cls, value: object) -> tuple[str, ...]:
        return _require_normalized_extras(_require_tuple(value, "extras"))

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _require_exact_distribution_version(value)


class PythonPackageGroupLockEntry(_ResolverEntry):
    """One complete atomic direct-package resolution result."""

    packages: tuple[ResolvedPythonPackage, ...] = Field(min_length=1)

    @field_validator("packages", mode="before")
    @classmethod
    def _validate_packages(cls, value: object) -> tuple[ResolvedPythonPackage, ...]:
        value = _require_tuple(value, "packages")
        packages = tuple(
            item
            if isinstance(item, ResolvedPythonPackage)
            else ResolvedPythonPackage.model_validate(item)
            for item in value
        )
        names = [item.name for item in packages]
        if names != sorted(set(names)):
            raise ValueError("packages must be sorted and unique")
        return packages


class ApplicationExtrasLockEntry(PythonPackageGroupLockEntry):
    """Atomic application-extra package result."""


class PyTorchLockEntry(PythonPackageGroupLockEntry):
    """Atomic PyTorch package and wheel-metadata compatibility result."""

    setuptools_specifier: str | None = None

    @field_validator("setuptools_specifier")
    @classmethod
    def _validate_setuptools_specifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = SpecifierSet(value)
        except InvalidSpecifier as error:
            raise ValueError("setuptools_specifier must be canonical") from error
        if not value or str(parsed) != value:
            raise ValueError("setuptools_specifier must be canonical")
        return value

    @model_validator(mode="after")
    def _validate_torch(self) -> PyTorchLockEntry:
        if "torch" not in {package.name for package in self.packages}:
            raise ValueError("pytorch packages must include torch")
        return self


class UvToolLockEntry(_ResolverEntry):
    """One independently resolved isolated uv-tool result."""

    name: str
    extras: tuple[str, ...]
    version: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _require_normalized_package(value)

    @field_validator("extras", mode="before")
    @classmethod
    def _validate_extras(cls, value: object) -> tuple[str, ...]:
        return _require_normalized_extras(_require_tuple(value, "extras"))

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _require_exact_distribution_version(value)

    @model_validator(mode="after")
    def _validate_comfy_cli_floor(self) -> UvToolLockEntry:
        if self.name == "comfy-cli":
            version = _require_exact_stable_public_version(self.version)
            if Version(version) < Version(COMFY_CLI_MINIMUM_VERSION):
                raise ValueError(
                    f"version must be comfy-cli {COMFY_CLI_MINIMUM_VERSION} or newer"
                )
        return self


class LocalExecutableLockEntry(_StrictLockModel):
    """Content-owned baked executable identity; no resolver request exists."""

    relative_path: str
    digest: str

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        parts = value.split("/")
        if (
            value.startswith("/")
            or "\\" in value
            or has_control_characters(value)
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("relative_path must be one canonical relative file path")
        return value

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _require_sha256(value)


class BuildHookLockEntry(LocalExecutableLockEntry):
    """One baked build-hook identity."""


class RuntimeHookLockEntry(LocalExecutableLockEntry):
    """One baked runtime hook identity."""


class LocalFileLockEntry(_StrictLockModel):
    """Content identity for one locked host-local build file."""

    relative_target: str
    digest: str

    @field_validator("relative_target")
    @classmethod
    def _validate_relative_target(cls, value: str) -> str:
        parts = value.split("/")
        if (
            value.startswith("/")
            or "\\" in value
            or has_control_characters(value)
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("relative_target must be one canonical relative file path")
        return value

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _require_sha256(value)


CanonicalLockEntry = (
    CudaImageLockEntry
    | UvImageLockEntry
    | ManagedPythonLockEntry
    | OfficialComfyUILockEntry
    | ComfyUIRequirementsLockEntry
    | RegistryNodeLockEntry
    | DirectGitLockEntry
    | ApplicationExtrasLockEntry
    | PyTorchLockEntry
    | UvToolLockEntry
    | BuildHookLockEntry
    | RuntimeHookLockEntry
    | LocalFileLockEntry
)


class ImagesLock(_StrictLockModel):
    cuda: CudaImageLockEntry
    uv: UvImageLockEntry


class PythonPackageGroupsLock(_StrictLockModel):
    pytorch: PyTorchLockEntry
    application_extras: ApplicationExtrasLockEntry | None = None


class PythonLock(_StrictLockModel):
    interpreter: ManagedPythonLockEntry
    package_groups: PythonPackageGroupsLock
    uv_tools: tuple[UvToolLockEntry, ...] = ()

    @field_validator("uv_tools", mode="before")
    @classmethod
    def _validate_uv_tools(cls, value: object) -> tuple[UvToolLockEntry, ...]:
        value = _require_tuple(value, "uv_tools")
        tools = tuple(
            item
            if isinstance(item, UvToolLockEntry)
            else UvToolLockEntry.model_validate(item)
            for item in value
        )
        names = [item.name for item in tools]
        if names != sorted(set(names)):
            raise ValueError("uv_tools must be sorted and unique")
        return tools


class ComfyUILock(_StrictLockModel):
    request_digest: str
    repository: str = Field(min_length=1)
    commit: str
    formal_release: str | None = None
    requirements: ComfyUIRequirementsLockEntry

    @field_validator("request_digest")
    @classmethod
    def _validate_request_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return _require_git_url(value)

    @field_validator("commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return _require_commit(value)

    @field_validator("formal_release")
    @classmethod
    def _validate_formal_release(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return OfficialComfyUILockEntry._validate_formal_release(value)


class CustomNodesLock(_StrictLockModel):
    registry: tuple[RegistryNodeLockEntry, ...] = ()
    git: tuple[DirectGitLockEntry, ...] = ()

    @field_validator("registry", "git", mode="before")
    @classmethod
    def _freeze_nodes(cls, value: object) -> tuple[object, ...]:
        return _require_tuple(value, "custom_nodes")

    @model_validator(mode="after")
    def _validate_nodes(self) -> CustomNodesLock:
        registry_ids = [entry.id for entry in self.registry]
        git_urls = [entry.url for entry in self.git]
        if registry_ids != sorted(set(registry_ids)):
            raise ValueError("registry nodes must be sorted and unique")
        if git_urls != sorted(set(git_urls)):
            raise ValueError("git nodes must be sorted and unique")
        return self


class HooksLock(_StrictLockModel):
    build: tuple[BuildHookLockEntry, ...] = ()
    runtime: tuple[RuntimeHookLockEntry, ...] = ()

    @field_validator("build", "runtime", mode="before")
    @classmethod
    def _freeze_hooks(cls, value: object) -> tuple[object, ...]:
        return _require_tuple(value, "hooks")

    @model_validator(mode="after")
    def _validate_hooks(self) -> HooksLock:
        for tree, entries in (
            ("build", self.build),
            ("runtime", self.runtime),
        ):
            paths = [entry.relative_path for entry in entries]
            if paths != sorted(set(paths)):
                raise ValueError(f"{tree} hooks must be sorted and unique")
        return self


class FilesLock(_StrictLockModel):
    """Optional locked identities for host-local build files."""

    local: tuple[LocalFileLockEntry, ...] = ()

    @field_validator("local", mode="before")
    @classmethod
    def _freeze_local(cls, value: object) -> tuple[object, ...]:
        return _require_tuple(value, "files")

    @model_validator(mode="after")
    def _validate_local(self) -> FilesLock:
        targets = [entry.relative_target for entry in self.local]
        if targets != sorted(set(targets)):
            raise ValueError("local file targets must be sorted and unique")
        return self


class CanonicalLock(_StrictLockModel):
    """Complete strict domain-grouped canonical config-lock schema v1."""

    schema_version: Literal[1]
    images: ImagesLock
    python: PythonLock
    comfyui: ComfyUILock
    custom_nodes: CustomNodesLock = Field(default_factory=CustomNodesLock)
    hooks: HooksLock = Field(default_factory=HooksLock)
    files: FilesLock = Field(default_factory=FilesLock)

    @property
    def entries(self) -> tuple[CanonicalLockEntry, ...]:
        """Expose typed reconciliation units without flattening serialized groups."""
        comfyui_source = OfficialComfyUILockEntry(
            request_digest=self.comfyui.request_digest,
            repository=self.comfyui.repository,
            commit=self.comfyui.commit,
            formal_release=self.comfyui.formal_release,
        )
        values: list[CanonicalLockEntry] = [
            self.images.cuda,
            self.images.uv,
            self.python.interpreter,
            comfyui_source,
            self.comfyui.requirements,
            self.python.package_groups.pytorch,
            *self.python.uv_tools,
            *self.custom_nodes.registry,
            *self.custom_nodes.git,
            *self.hooks.build,
            *self.hooks.runtime,
            *self.files.local,
        ]
        if self.python.package_groups.application_extras is not None:
            values.append(self.python.package_groups.application_extras)
        return tuple(values)


class OciRequestIdentity(_StrictLockModel):
    type: Literal["oci"]
    role: Literal["cuda-base", "uv-tool"]
    repository: str = Field(min_length=1)
    tag: str = Field(min_length=1)
    platform: Literal["linux/amd64"]

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return _require_oci_repository(value)

    @field_validator("tag")
    @classmethod
    def _validate_tag(cls, value: str) -> str:
        return _require_oci_tag(value)


class ManagedPythonRequestIdentity(_StrictLockModel):
    type: Literal["managed-python"]
    version: str = Field(min_length=1)
    implementation: Literal["cpython"]
    platform: Literal["linux/amd64"]
    libc: Literal["gnu"]
    catalog_descriptor_digest: str

    @field_validator("catalog_descriptor_digest")
    @classmethod
    def _validate_catalog_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _require_exact_stable_version(value)


class ComfyUIRequestIdentity(_StrictLockModel):
    type: Literal["comfyui"]
    repository: str = Field(min_length=1)
    selector: str = Field(min_length=1)

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return _require_git_url(value)

    @field_validator("selector")
    @classmethod
    def _normalize_selector(cls, value: str) -> str:
        return normalize_comfyui_version(_require_token(value, "selector"))


class ComfyUIRequirementsRequestIdentity(_StrictLockModel):
    """Request for one exact commit's root requirements source."""

    type: Literal["comfyui-requirements"]
    repository: str = Field(min_length=1)
    commit: str
    floor_commit: str
    path: Literal["requirements.txt"]

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return _require_git_url(value)

    @field_validator("commit", "floor_commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return _require_commit(value)


class ComfyCliRequestIdentity(_StrictLockModel):
    """Internal highest-stable request for the optional isolated user tool."""

    type: Literal["comfy-cli"]
    package: Literal["comfy-cli"]
    policy: Literal["highest-target-compatible-stable"]
    minimum_version: str
    environment: Literal["uv-tool:comfy-cli"]
    index_url: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    platform: Literal["linux/amd64"]
    resolver_descriptor_digest: str

    @field_validator("minimum_version")
    @classmethod
    def _validate_minimum_version(cls, value: str) -> str:
        if value != COMFY_CLI_MINIMUM_VERSION:
            raise ValueError("minimum_version must match the exact ledger")
        return value

    @field_validator("index_url")
    @classmethod
    def _validate_index_url(cls, value: str) -> str:
        return _require_http_url(value, "index_url")

    @field_validator("python_version")
    @classmethod
    def _validate_python_version(cls, value: str) -> str:
        return _require_exact_stable_version(value)

    @field_validator("resolver_descriptor_digest")
    @classmethod
    def _validate_resolver_digest(cls, value: str) -> str:
        return _require_sha256(value)


class RegistryRequestIdentity(_StrictLockModel):
    type: Literal["registry"]
    id: str = Field(min_length=1)
    selector: str = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _require_registry_id(value)

    @field_validator("selector")
    @classmethod
    def _normalize_selector(cls, value: str) -> str:
        return normalize_registry_version(_require_token(value, "selector"))


class DirectGitRequestIdentity(_StrictLockModel):
    type: Literal["git"]
    url: str = Field(min_length=1)
    ref: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _require_git_url(value)

    @field_validator("ref")
    @classmethod
    def _validate_ref(cls, value: str) -> str:
        if not is_git_ref(value):
            raise ValueError("ref must be one canonical Git ref or full commit")
        return value


class DirectPythonRequestMember(_StrictLockModel):
    """One normalized member of a cohesive direct-Python resolution group."""

    package: str
    extras: tuple[str, ...]
    specifier: str
    direct_reference: str | None = None

    @field_validator("package")
    @classmethod
    def _validate_package(cls, value: str) -> str:
        return _require_normalized_package(value)

    @field_validator("extras", mode="before")
    @classmethod
    def _validate_extras(cls, value: object) -> tuple[str, ...]:
        return _require_normalized_extras(_require_tuple(value, "extras"))

    @field_validator("specifier")
    @classmethod
    def _validate_specifier(cls, value: str) -> str:
        return _require_direct_package_specifier(value)

    @model_validator(mode="after")
    def _validate_source(self) -> DirectPythonRequestMember:
        if self.direct_reference is not None and self.specifier:
            raise ValueError("direct-reference members must not contain a specifier")
        if self.direct_reference is not None:
            identity = parse_direct_requirement(self.resolver_requirement)
            if (
                identity.name != self.package
                or identity.extras != self.extras
                or identity.specifier
                or identity.direct_reference != self.direct_reference
                or identity.marker is not None
            ):
                raise ValueError(
                    "direct-reference member must be one canonical "
                    "marker-free requirement"
                )
        return self

    @property
    def resolver_requirement(self) -> str:
        """Render the complete marker-free requirement consumed by uv."""
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        dependency = f"{self.package}{extras}"
        if self.direct_reference is not None:
            return f"{dependency} @ {self.direct_reference}"
        return f"{dependency}{self.specifier}"


class DirectPythonRequestIdentity(_StrictLockModel):
    """Complete normalized generic group identity shared by resolved members."""

    type: Literal["python-group"]
    environment: str
    group: Literal["application-extra", "uv-tool"]
    python_version: str = Field(min_length=1)
    platform: Literal["linux/amd64"]
    index_url: str = Field(min_length=1)
    resolver_descriptor_digest: str
    members: tuple[DirectPythonRequestMember, ...] = Field(min_length=1)

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, value: str) -> str:
        return _require_environment(value)

    @field_validator("members", mode="before")
    @classmethod
    def _normalize_members(cls, value: object) -> tuple[DirectPythonRequestMember, ...]:
        value = _require_tuple(value, "members")
        members = tuple(
            item
            if isinstance(item, DirectPythonRequestMember)
            else DirectPythonRequestMember.model_validate(item)
            for item in value
        )
        packages = [member.package for member in members]
        if len(packages) != len(set(packages)):
            raise ValueError("members must contain each package exactly once")
        return tuple(sorted(members, key=lambda member: member.package))

    @field_validator("python_version")
    @classmethod
    def _validate_python_version(cls, value: str) -> str:
        return _require_exact_stable_version(value)

    @field_validator("index_url")
    @classmethod
    def _validate_index_url(cls, value: str) -> str:
        return _require_http_url(value, "index_url")

    @field_validator("resolver_descriptor_digest")
    @classmethod
    def _validate_resolver_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def _validate_group_ownership(self) -> DirectPythonRequestIdentity:
        if self.group == "uv-tool":
            package = self.environment.removeprefix("uv-tool:")
            if (
                not self.environment.startswith("uv-tool:")
                or len(self.members) != 1
                or self.members[0].package != package
            ):
                raise ValueError(
                    "uv-tool groups require one matching isolated tool member"
                )
        elif self.environment != "application":
            raise ValueError("application groups require the application environment")
        return self


class PyTorchRequestIdentity(_StrictLockModel):
    """Typed backend package-group request with independent channel identity."""

    type: Literal["pytorch-group"]
    environment: Literal["application"]
    group: Literal["pytorch"]
    backend: Literal["cuda"]
    channel: str
    python_version: str = Field(min_length=1)
    platform: Literal["linux/amd64"]
    python_index_url: str = Field(min_length=1)
    pytorch_index_url: str = Field(min_length=1)
    resolver_descriptor_digest: str
    upstream_protected: tuple[ProtectedRequirementProjection, ...] = Field(
        default_factory=tuple
    )
    members: tuple[DirectPythonRequestMember, ...] = Field(min_length=1)

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, value: str) -> str:
        if _CUDA_CHANNEL_PATTERN.fullmatch(value) is None:
            raise ValueError("channel must be one canonical CUDA wheel channel")
        return value

    @field_validator("members", mode="before")
    @classmethod
    def _normalize_members(cls, value: object) -> tuple[DirectPythonRequestMember, ...]:
        value = _require_tuple(value, "members")
        members = tuple(
            item
            if isinstance(item, DirectPythonRequestMember)
            else DirectPythonRequestMember.model_validate(item)
            for item in value
        )
        packages = [member.package for member in members]
        if len(packages) != len(set(packages)):
            raise ValueError("members must contain each package exactly once")
        if "torch" not in packages:
            raise ValueError("pytorch groups must include torch")
        return tuple(sorted(members, key=lambda member: member.package))

    @field_validator("upstream_protected", mode="before")
    @classmethod
    def _normalize_upstream_protected(
        cls, value: object
    ) -> tuple[ProtectedRequirementProjection, ...]:
        value = _require_tuple(value, "upstream_protected")
        projection = tuple(
            item
            if isinstance(item, ProtectedRequirementProjection)
            else ProtectedRequirementProjection.model_validate(item)
            for item in value
        )
        packages = [member.package for member in projection]
        if packages != sorted(set(packages)):
            raise ValueError("upstream_protected must be sorted and unique")
        return projection

    @model_validator(mode="after")
    def _validate_upstream_members(self) -> PyTorchRequestIdentity:
        members = {member.package: member for member in self.members}
        for upstream in self.upstream_protected:
            member = members.get(upstream.package)
            if member is None or not set(upstream.extras).issubset(member.extras):
                raise ValueError("upstream protected requirements must enter the group")
        return self

    @field_validator("python_version")
    @classmethod
    def _validate_python_version(cls, value: str) -> str:
        return _require_exact_stable_version(value)

    @field_validator("python_index_url", "pytorch_index_url")
    @classmethod
    def _validate_index_url(cls, value: str) -> str:
        return _require_http_url(value, "index_url")

    @field_validator("resolver_descriptor_digest")
    @classmethod
    def _validate_resolver_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def _validate_source_channel(self) -> PyTorchRequestIdentity:
        if not pytorch_index_matches_channel(self.pytorch_index_url, self.channel):
            raise ValueError("pytorch_index_url must end with the derived channel")
        if self.python_index_url == self.pytorch_index_url:
            raise ValueError("Python and PyTorch indexes must be distinct")
        return self


PythonGroupRequestIdentity = DirectPythonRequestIdentity | PyTorchRequestIdentity


ResolverRequestIdentity = (
    OciRequestIdentity
    | ManagedPythonRequestIdentity
    | ComfyUIRequestIdentity
    | ComfyUIRequirementsRequestIdentity
    | ComfyCliRequestIdentity
    | RegistryRequestIdentity
    | DirectGitRequestIdentity
    | DirectPythonRequestIdentity
    | PyTorchRequestIdentity
)


def compute_request_digest(request: ResolverRequestIdentity) -> str:
    """Bind one normalized, resolution-affecting request identity."""
    canonical = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def parse_canonical_lock_toml(document: str | bytes) -> CanonicalLock:
    """Parse only canonical v1; every invalid/current/future input fails alike."""
    try:
        if isinstance(document, bytes):
            document = document.decode("utf-8")
        data = tomllib.loads(document)
        return CanonicalLock.model_validate(data)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as error:
        raise CanonicalLockError(INVALID_CANONICAL_LOCK_MESSAGE) from error


def load_canonical_lock(path: str | Path) -> CanonicalLock:
    """Load one strict canonical config-lock v1 document."""
    try:
        document = Path(path).read_bytes()
    except OSError as error:
        raise CanonicalLockError(INVALID_CANONICAL_LOCK_MESSAGE) from error
    return parse_canonical_lock_toml(document)


def dump_canonical_lock_toml(lock: CanonicalLock) -> str:
    """Serialize the strict grouped model in deterministic domain order."""
    return tomli_w.dumps(
        lock.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    )


def canonical_entry_key(entry: CanonicalLockEntry) -> tuple[str, ...]:
    """Return the stable logical identity used by lock reconciliation."""
    if isinstance(entry, CudaImageLockEntry):
        return ("images", "cuda")
    if isinstance(entry, UvImageLockEntry):
        return ("images", "uv")
    if isinstance(entry, ManagedPythonLockEntry):
        return ("python", "interpreter")
    if isinstance(entry, OfficialComfyUILockEntry):
        return ("comfyui",)
    if isinstance(entry, ComfyUIRequirementsLockEntry):
        return ("comfyui", "requirements")
    if isinstance(entry, PyTorchLockEntry):
        return ("python", "package_groups", "pytorch")
    if isinstance(entry, ApplicationExtrasLockEntry):
        return ("python", "package_groups", "application_extras")
    if isinstance(entry, UvToolLockEntry):
        return ("python", "uv_tools", entry.name)
    if isinstance(entry, RegistryNodeLockEntry):
        return ("custom_nodes", "registry", entry.id)
    if isinstance(entry, DirectGitLockEntry):
        return ("custom_nodes", "git", entry.url)
    if isinstance(entry, BuildHookLockEntry):
        return ("hooks", "build", entry.relative_path)
    if isinstance(entry, RuntimeHookLockEntry):
        return ("hooks", "runtime", entry.relative_path)
    if isinstance(entry, LocalFileLockEntry):
        return ("files", "local", entry.relative_target)
    raise TypeError(f"unsupported canonical lock entry: {type(entry).__name__}")


def canonical_lock_from_entries(
    entries: tuple[CanonicalLockEntry, ...] | list[CanonicalLockEntry],
) -> CanonicalLock:
    """Assemble one complete grouped lock from atomic reconciliation units."""
    by_key = {canonical_entry_key(entry): entry for entry in entries}
    if len(by_key) != len(entries):
        raise ValueError("canonical lock entries must have unique logical identities")

    def require(key: tuple[str, ...], expected: type):
        entry = by_key.get(key)
        if not isinstance(entry, expected):
            raise ValueError(f"canonical lock is missing required identity {key!r}")
        return entry

    cuda = require(("images", "cuda"), CudaImageLockEntry)
    uv = require(("images", "uv"), UvImageLockEntry)
    interpreter = require(("python", "interpreter"), ManagedPythonLockEntry)
    pytorch = require(("python", "package_groups", "pytorch"), PyTorchLockEntry)
    comfyui = require(("comfyui",), OfficialComfyUILockEntry)
    requirements = require(("comfyui", "requirements"), ComfyUIRequirementsLockEntry)
    application_extras = by_key.get(("python", "package_groups", "application_extras"))
    if application_extras is not None and not isinstance(
        application_extras, ApplicationExtrasLockEntry
    ):
        raise ValueError("canonical lock application extras identity is invalid")

    known = {
        ("images", "cuda"),
        ("images", "uv"),
        ("python", "interpreter"),
        ("python", "package_groups", "pytorch"),
        ("comfyui",),
        ("comfyui", "requirements"),
    }
    if application_extras is not None:
        known.add(("python", "package_groups", "application_extras"))
    known.update(key for key in by_key if key[:2] == ("python", "uv_tools"))
    known.update(key for key in by_key if key[:2] == ("custom_nodes", "registry"))
    known.update(key for key in by_key if key[:2] == ("custom_nodes", "git"))
    known.update(key for key in by_key if key[:2] == ("hooks", "build"))
    known.update(key for key in by_key if key[:2] == ("hooks", "runtime"))
    known.update(key for key in by_key if key[:2] == ("files", "local"))
    if set(by_key) != known:
        raise ValueError("canonical lock contains unsupported identities")

    return CanonicalLock(
        schema_version=1,
        images=ImagesLock(cuda=cuda, uv=uv),
        python=PythonLock(
            interpreter=interpreter,
            package_groups=PythonPackageGroupsLock(
                pytorch=pytorch,
                application_extras=application_extras,
            ),
            uv_tools=tuple(
                entry
                for key, entry in sorted(by_key.items())
                if key[:2] == ("python", "uv_tools")
                and isinstance(entry, UvToolLockEntry)
            ),
        ),
        comfyui=ComfyUILock(
            request_digest=comfyui.request_digest,
            repository=comfyui.repository,
            commit=comfyui.commit,
            formal_release=comfyui.formal_release,
            requirements=requirements,
        ),
        custom_nodes=CustomNodesLock(
            registry=tuple(
                entry
                for key, entry in sorted(by_key.items())
                if key[:2] == ("custom_nodes", "registry")
                and isinstance(entry, RegistryNodeLockEntry)
            ),
            git=tuple(
                entry
                for key, entry in sorted(by_key.items())
                if key[:2] == ("custom_nodes", "git")
                and isinstance(entry, DirectGitLockEntry)
            ),
        ),
        hooks=HooksLock(
            build=tuple(
                entry
                for key, entry in sorted(by_key.items())
                if key[:2] == ("hooks", "build")
                and isinstance(entry, BuildHookLockEntry)
            ),
            runtime=tuple(
                entry
                for key, entry in sorted(by_key.items())
                if key[:2] == ("hooks", "runtime")
                and isinstance(entry, RuntimeHookLockEntry)
            ),
        ),
        files=FilesLock(
            local=tuple(
                entry
                for key, entry in sorted(by_key.items())
                if key[:2] == ("files", "local")
                and isinstance(entry, LocalFileLockEntry)
            ),
        ),
    )


def validate_sha256_digest(value: str) -> str:
    """Validate one canonical SHA-256 identity at any execution boundary."""
    return _require_sha256(value)


def validate_git_commit(value: str) -> str:
    """Validate one exact Git commit at any execution boundary."""
    return _require_commit(value)


def validate_exact_stable_version(value: str) -> str:
    return _require_exact_stable_version(value)


def validate_exact_stable_distribution_version(value: str) -> str:
    return _require_exact_stable_distribution_version(value)


def validate_exact_distribution_version(value: str) -> str:
    return _require_exact_distribution_version(value)


def validate_oci_repository(value: str) -> str:
    return _require_oci_repository(value)


def validate_oci_tag(value: str) -> str:
    return _require_oci_tag(value)


def validate_http_url(value: str, field: str) -> str:
    return _require_http_url(value, field)


def validate_git_url(value: str) -> str:
    return _require_git_url(value)


def validate_normalized_package(value: str) -> str:
    return _require_normalized_package(value)


def validate_normalized_extras(value: tuple[str, ...]) -> tuple[str, ...]:
    return _require_normalized_extras(value)


def validate_environment(value: str) -> str:
    return _require_environment(value)


def validate_registry_id(value: str) -> str:
    return validate_registry_resource_id(value)


def normalized_registry_id(value: str) -> str:
    """Return the PyPA installed-distribution identity for one Registry ID."""
    return registry_distribution_identity(value)


def validate_exact_registry_version(value: str) -> str:
    return _require_exact_registry_version(value)


def _require_sha256(value: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("digest must be sha256:<64 lowercase hex>")
    return value


def _require_commit(value: str) -> str:
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise ValueError("commit must be 40 lowercase hexadecimal characters")
    return value


def _require_exact_stable_version(value: str) -> str:
    if _EXACT_STABLE_PATTERN.fullmatch(value) is None:
        raise ValueError("version must be one canonical exact stable release")
    try:
        parsed = Version(value)
    except InvalidVersion as error:
        raise ValueError(
            "version must be one canonical exact stable release"
        ) from error
    if parsed.public != value or parsed.is_prerelease or parsed.is_devrelease:
        raise ValueError("version must be one canonical exact stable release")
    return value


def _require_exact_public_version(value: str) -> str:
    try:
        parsed = Version(value)
    except InvalidVersion as error:
        raise ValueError(
            "version must be one canonical exact public version"
        ) from error
    if parsed.local is not None or parsed.public != value:
        raise ValueError("version must be one canonical exact public version")
    return value


def _require_exact_stable_public_version(value: str) -> str:
    value = _require_exact_public_version(value)
    parsed = Version(value)
    if parsed.is_prerelease or parsed.is_devrelease:
        raise ValueError("version must be one canonical exact stable public version")
    return value


def _require_exact_stable_distribution_version(value: str) -> str:
    value = _require_exact_distribution_version(value)
    parsed = Version(value)
    if parsed.is_prerelease or parsed.is_devrelease:
        raise ValueError(
            "version must be one canonical exact stable distribution version"
        )
    return value


def _require_exact_distribution_version(value: str) -> str:
    try:
        parsed = Version(value)
    except InvalidVersion as error:
        raise ValueError(
            "version must be one canonical exact distribution version"
        ) from error
    if str(parsed) != value:
        raise ValueError("version must be one canonical exact distribution version")
    return value


def pytorch_core_version_matches_channel(
    package: str, version: str, channel: str
) -> bool:
    """Bind only the approved PyTorch core distributions to a backend channel."""
    if package not in {"torch", "torchvision"}:
        return True
    try:
        parsed = Version(version)
    except InvalidVersion:
        return False
    return parsed.local == channel


def pytorch_index_matches_channel(index_url: str, channel: str) -> bool:
    """Require the derived channel to be the final PyTorch index path segment."""
    path = urlsplit(index_url).path.rstrip("/")
    return bool(path) and path.rsplit("/", maxsplit=1)[-1] == channel


def uv_image_version_matches_tag(tag: str, resolved_version: str | None) -> bool:
    """Bind cdh's exact or rolling Debian provider tag to observed uv."""
    if resolved_version is None:
        return False
    try:
        resolved = Version(resolved_version)
    except InvalidVersion:
        return False
    if (
        str(resolved) != resolved_version
        or resolved.is_prerelease
        or resolved.is_devrelease
    ):
        return False
    if tag == "debian-slim":
        return True
    suffix = "-debian-slim"
    if not tag.endswith(suffix):
        return False
    return resolved_version == tag.removesuffix(suffix)


def _require_exact_registry_version(value: str) -> str:
    normalized = normalize_registry_version(value)
    if (
        normalized != value
        or value == "latest"
        or any(character in value for character in "<>=!,")
    ):
        raise ValueError("version must be one canonical exact Registry version")
    return value


def _require_token(value: str, field: str) -> str:
    if not value or value != value.strip() or has_control_characters(value):
        raise ValueError(f"{field} must be one canonical non-empty value")
    return value


def _require_registry_id(value: str) -> str:
    return validate_registry_resource_id(value)


def _require_direct_package_specifier(value: str) -> str:
    try:
        specifiers = SpecifierSet(value)
    except InvalidSpecifier as error:
        raise ValueError("specifier must be a supported package specifier") from error
    if str(specifiers) != value:
        raise ValueError("specifier must be one canonical package specifier")
    return value


def _require_oci_repository(value: str) -> str:
    if _OCI_REPOSITORY_PATTERN.fullmatch(value) is None:
        raise ValueError("repository must be one canonical OCI repository")
    return value


def _require_oci_tag(value: str) -> str:
    if not is_oci_tag(value):
        raise ValueError("tag must be one canonical OCI tag")
    return value


def _require_http_url(value: str, field: str) -> str:
    if value != value.strip() or not is_http_url(value):
        raise ValueError(f"{field} must be one canonical HTTP(S) URL")
    return value


def _require_git_url(value: str) -> str:
    if value != value.strip() or not is_git_source_url(value):
        raise ValueError("URL must be one canonical Git source URL")
    return value


def _require_normalized_package(value: str) -> str:
    if _PACKAGE_PATTERN.fullmatch(value) is None:
        raise ValueError("package must be a normalized package name")
    return value


def _require_normalized_extras(value: tuple[str, ...]) -> tuple[str, ...]:
    if any(_EXTRA_PATTERN.fullmatch(extra) is None for extra in value):
        raise ValueError("extras must contain normalized extra names")
    if value != tuple(sorted(set(value))):
        raise ValueError("extras must be unique and sorted")
    return value


def _require_tuple(value: object, field: str) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be one array")
    return tuple(value)


def _require_environment(value: str) -> str:
    if _ENVIRONMENT_PATTERN.fullmatch(value) is None:
        raise ValueError("environment must be application or uv-tool:<name>")
    return value
