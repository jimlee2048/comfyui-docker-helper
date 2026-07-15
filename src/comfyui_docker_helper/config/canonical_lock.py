"""Canonical config-lock schema v1 and resolver request identities."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import tomli_w
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from comfyui_docker_helper.config.final_validation import (
    is_git_ref,
    is_git_source_url,
    is_oci_tag,
)
from comfyui_docker_helper.config.selector_validation import (
    normalize_comfyui_version,
    normalize_registry_version,
)
from comfyui_docker_helper.config.url_validation import is_http_url
from comfyui_docker_helper.config.value_validation import has_control_characters
from comfyui_docker_helper.exact_ledger import (
    COMFY_CLI_MINIMUM_VERSION,
    COMFYUI_MINIMUM_VERSION,
)

CANONICAL_LOCK_SCHEMA_VERSION = 1
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
        return _require_direct_package_selector(value)


class OciLockEntry(_ResolverEntry):
    """An immutable descriptor selected from one human-readable OCI tag."""

    type: Literal["oci"]
    role: Literal["cuda-base", "uv-tool"]
    repository: str = Field(min_length=1)
    tag: str = Field(min_length=1)
    descriptor_digest: str
    descriptor_kind: Literal["index", "manifest"]
    platform: Literal["linux/amd64"]
    resolved_version: str | None = None

    @field_validator("descriptor_digest")
    @classmethod
    def _validate_descriptor_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return _require_oci_repository(value)

    @field_validator("tag")
    @classmethod
    def _validate_tag(cls, value: str) -> str:
        return _require_oci_tag(value)

    @field_validator("resolved_version")
    @classmethod
    def _validate_resolved_version(cls, value: str | None) -> str | None:
        return None if value is None else _require_exact_stable_version(value)

    @model_validator(mode="after")
    def _validate_role_version(self) -> OciLockEntry:
        if (self.role == "uv-tool") != (self.resolved_version is not None):
            raise ValueError("only uv-tool images require a resolved version")
        return self


class ManagedPythonLockEntry(_ResolverEntry):
    """Exact uv-managed CPython plus release-owned bootstrap/cdh inputs."""

    type: Literal["managed-python"]
    version: str = Field(min_length=1)
    implementation: Literal["cpython"]
    platform: Literal["linux/amd64"]
    libc: Literal["gnu"]
    provider: Literal["uv-managed"]
    catalog_descriptor_digest: str
    catalog_key: str = Field(min_length=1)
    catalog_url: str = Field(min_length=1)
    pip_version: str = Field(min_length=1)
    cdh_version: str = Field(min_length=1)
    cdh_source_digest: str
    uv_build_version: str = Field(min_length=1)

    @field_validator("catalog_descriptor_digest", "cdh_source_digest")
    @classmethod
    def _validate_content_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator(
        "version",
        "pip_version",
        "cdh_version",
        "uv_build_version",
    )
    @classmethod
    def _validate_exact_stable_version(cls, value: str) -> str:
        return _require_exact_stable_version(value)

    @field_validator("catalog_key")
    @classmethod
    def _validate_catalog_key(cls, value: str) -> str:
        return _require_token(value, "catalog_key")

    @field_validator("catalog_url")
    @classmethod
    def _validate_catalog_url(cls, value: str) -> str:
        return _require_http_url(value, "catalog_url")


class OfficialComfyUILockEntry(_ResolverEntry):
    """Exact official ComfyUI source selection."""

    type: Literal["comfyui"]
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
    """Exact root requirements bytes and target-active protected projection."""

    type: Literal["comfyui-requirements"]
    repository: str = Field(min_length=1)
    commit: str
    floor_commit: str
    path: Literal["requirements.txt"]
    python_version: str = Field(min_length=1)
    platform: Literal["linux/amd64"]
    protected_names: tuple[str, ...] = Field(min_length=1)
    protected_policy_digest: str
    requirements_digest: str
    protected: tuple[ProtectedRequirementProjection, ...]

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return _require_git_url(value)

    @field_validator("commit", "floor_commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return _require_commit(value)

    @field_validator("python_version")
    @classmethod
    def _validate_python_version(cls, value: str) -> str:
        return _require_exact_stable_version(value)

    @field_validator("protected_policy_digest", "requirements_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("protected_names", mode="before")
    @classmethod
    def _validate_protected_names(cls, value: object) -> tuple[str, ...]:
        value = _require_tuple(value, "protected_names")
        names = [_require_normalized_package(item) for item in value]
        if names != sorted(set(names)):
            raise ValueError("protected_names must be sorted and unique")
        return tuple(names)

    @field_validator("protected", mode="before")
    @classmethod
    def _validate_protected(
        cls, value: object
    ) -> tuple[ProtectedRequirementProjection, ...]:
        value = _require_tuple(value, "protected")
        projection = tuple(
            item
            if isinstance(item, ProtectedRequirementProjection)
            else ProtectedRequirementProjection.model_validate(item)
            for item in value
        )
        packages = [item.package for item in projection]
        if packages != sorted(set(packages)):
            raise ValueError("protected projection must be sorted and unique")
        return projection

    @model_validator(mode="after")
    def _validate_projection_names(self) -> ComfyUIRequirementsLockEntry:
        if any(item.package not in self.protected_names for item in self.protected):
            raise ValueError("protected projection contains an unowned package")
        return self


class ComfyCliLockEntry(_ResolverEntry):
    """Exact isolated comfy-cli user-tool selection."""

    type: Literal["comfy-cli"]
    package: Literal["comfy-cli"]
    version: str = Field(min_length=1)
    environment: Literal["uv-tool:comfy-cli"]

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        version = _require_exact_stable_public_version(value)
        if Version(version) < Version(COMFY_CLI_MINIMUM_VERSION):
            raise ValueError(
                f"version must be comfy-cli {COMFY_CLI_MINIMUM_VERSION} or newer"
            )
        return version


class RegistryNodeLockEntry(_ResolverEntry):
    """Minimal exact Registry node identity."""

    type: Literal["registry"]
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

    type: Literal["git"]
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


class DirectPythonLockEntry(_ResolverEntry):
    """Exact direct top-level package identity for one owning environment."""

    type: Literal["python-package"]
    package: str
    extras: tuple[str, ...]
    version: str = Field(min_length=1)
    environment: str

    @field_validator("package")
    @classmethod
    def _validate_package(cls, value: str) -> str:
        return _require_normalized_package(value)

    @field_validator("extras", mode="before")
    @classmethod
    def _validate_extras(cls, value: object) -> tuple[str, ...]:
        return _require_normalized_extras(_require_tuple(value, "extras"))

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, value: str) -> str:
        return _require_environment(value)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _require_exact_stable_distribution_version(value)


class PyTorchCompatibilityLockEntry(_ResolverEntry):
    """Wheel-metadata policy derived with one exact PyTorch group."""

    type: Literal["pytorch-compatibility"]
    environment: Literal["application"]
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


class LocalExecutableLockEntry(_StrictLockModel):
    """Content-owned baked executable identity; no resolver request exists."""

    type: Literal["local-executable"]
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


CanonicalLockEntry = Annotated[
    OciLockEntry
    | ManagedPythonLockEntry
    | OfficialComfyUILockEntry
    | ComfyUIRequirementsLockEntry
    | ComfyCliLockEntry
    | RegistryNodeLockEntry
    | DirectGitLockEntry
    | DirectPythonLockEntry
    | PyTorchCompatibilityLockEntry
    | LocalExecutableLockEntry,
    Field(discriminator="type"),
]


class CanonicalLock(_StrictLockModel):
    """Complete strict canonical config-lock schema v1."""

    schema_version: Literal[1]
    entries: tuple[CanonicalLockEntry, ...]

    @field_validator("entries", mode="before")
    @classmethod
    def _freeze_entries(cls, value: object) -> tuple[object, ...]:
        return _require_tuple(value, "entries")

    @model_validator(mode="after")
    def _validate_entries(self) -> CanonicalLock:
        keys = [canonical_entry_key(entry) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("lock entries must have unique logical identities")
        return self


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
    """Request for one exact commit's target-specific protected projection."""

    type: Literal["comfyui-requirements"]
    repository: str = Field(min_length=1)
    commit: str
    floor_commit: str
    path: Literal["requirements.txt"]
    python_version: str = Field(min_length=1)
    platform: Literal["linux/amd64"]
    protected_names: tuple[str, ...] = Field(min_length=1)
    protected_policy_digest: str

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return _require_git_url(value)

    @field_validator("commit", "floor_commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return _require_commit(value)

    @field_validator("python_version")
    @classmethod
    def _validate_python_version(cls, value: str) -> str:
        return _require_exact_stable_version(value)

    @field_validator("protected_names", mode="before")
    @classmethod
    def _validate_protected_names(cls, value: object) -> tuple[str, ...]:
        value = _require_tuple(value, "protected_names")
        names = [_require_normalized_package(item) for item in value]
        if names != sorted(set(names)):
            raise ValueError("protected_names must be sorted and unique")
        return tuple(names)

    @field_validator("protected_policy_digest")
    @classmethod
    def _validate_policy_digest(cls, value: str) -> str:
        return _require_sha256(value)


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
        return _require_direct_package_selector(value)


class DirectPythonRequestIdentity(_StrictLockModel):
    """Complete normalized generic group identity shared by resolved members."""

    type: Literal["python-group"]
    environment: str
    group: Literal["application-extra", "uv-tool"]
    python_version: str = Field(min_length=1)
    platform: Literal["linux/amd64"]
    index_url: str = Field(min_length=1)
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
    """Serialize canonical fields and entries in deterministic logical order."""
    entries = sorted(lock.entries, key=canonical_entry_key)
    data = {
        "schema_version": lock.schema_version,
        "entries": [
            entry.model_dump(mode="json", exclude_none=True) for entry in entries
        ],
    }
    return tomli_w.dumps(data)


def canonical_entry_key(entry: CanonicalLockEntry) -> tuple[str, ...]:
    """Return the stable logical identity used by lock reconciliation."""
    if isinstance(entry, OciLockEntry):
        return (entry.type, entry.role)
    if isinstance(entry, ManagedPythonLockEntry):
        return (entry.type, entry.implementation, entry.platform)
    if isinstance(entry, OfficialComfyUILockEntry):
        return (entry.type, entry.repository)
    if isinstance(entry, ComfyUIRequirementsLockEntry):
        return (entry.type, entry.repository)
    if isinstance(entry, ComfyCliLockEntry):
        return (entry.type, entry.package, entry.environment)
    if isinstance(entry, RegistryNodeLockEntry):
        return (entry.type, entry.id)
    if isinstance(entry, DirectGitLockEntry):
        return (entry.type, entry.url)
    if isinstance(entry, DirectPythonLockEntry):
        return (entry.type, entry.environment, entry.package)
    if isinstance(entry, PyTorchCompatibilityLockEntry):
        return (entry.type, entry.environment)
    return (entry.type, entry.relative_path)


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
    return _require_registry_id(value)


def validate_exact_registry_version(value: str) -> str:
    return _require_exact_registry_version(value)


def validate_canonical_token(value: str, field: str) -> str:
    return _require_token(value, field)


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
    try:
        parsed = Version(value)
    except InvalidVersion as error:
        raise ValueError(
            "version must be one canonical exact stable distribution version"
        ) from error
    if str(parsed) != value or parsed.is_prerelease or parsed.is_devrelease:
        raise ValueError(
            "version must be one canonical exact stable distribution version"
        )
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
    """Bind exact uv semver tags while allowing descriptor-locked moving tags."""
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
    try:
        selector = Version(tag)
    except InvalidVersion:
        return True
    if str(selector) != tag or selector.is_prerelease or selector.is_devrelease:
        return True
    return resolved_version == tag


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
    value = _require_token(value, "id")
    if value.startswith("-"):
        raise ValueError("id must be one argv-safe Registry ID")
    try:
        canonicalize_name(value, validate=True)
    except InvalidName as error:
        raise ValueError("id must be one valid Registry project name") from error
    return value


def _require_direct_package_selector(value: str) -> str:
    try:
        specifiers = SpecifierSet(value)
    except InvalidSpecifier as error:
        raise ValueError("selector must be a supported package selector") from error
    if str(specifiers) != value:
        raise ValueError("selector must be one canonical package selector")
    items = tuple(specifiers)
    if any(
        item.operator not in {"==", "!=", "<", "<=", ">", ">=", "~="}
        or "*" in item.version
        for item in items
    ):
        raise ValueError("selector must be a supported package selector")
    if any(not _is_stable_public_operand(item.version) for item in items):
        raise ValueError("selector operands must be stable public versions")
    operators = {item.operator for item in items}
    if operators == {"=="}:
        if len(items) != 1:
            raise ValueError("exact selectors must contain one version")
        return value
    return value


def _is_stable_public_operand(value: str) -> bool:
    try:
        parsed = Version(value)
    except InvalidVersion:
        return False
    return not (
        parsed.is_prerelease or parsed.is_devrelease or parsed.local is not None
    )


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
