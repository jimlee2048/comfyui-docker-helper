"""Strict observational final-image manifest schema v1."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Literal

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from comfyui_docker_helper.config.build_plan import ManifestBinding
from comfyui_docker_helper.config.canonical_lock import (
    validate_exact_stable_distribution_version,
    validate_exact_stable_version,
    validate_git_commit,
    validate_git_url,
    validate_http_url,
    validate_normalized_extras,
    validate_normalized_package,
    validate_oci_repository,
    validate_oci_tag,
    validate_sha256_digest,
)
from comfyui_docker_helper.config.custom_node_inventory import CustomNodeInventory
from comfyui_docker_helper.config.file_checksum import (
    validate_canonical_file_checksum,
)
from comfyui_docker_helper.config.hook_validation import (
    validate_hook_digest,
    validate_hook_relative_path,
)
from comfyui_docker_helper.config.os_packages import validate_apt_package_identity
from comfyui_docker_helper.config.shutdown_timeout import ShutdownTimeout
from comfyui_docker_helper.config.value_validation import has_control_characters

FINAL_MANIFEST_SCHEMA_VERSION = 1

type FinalBuildCheckId = Literal[
    "torch-import",
    "torch-cpu-tensor",
    "torchvision-import",
    "torchaudio-import",
    "torchaudio-cpu-resample",
    "comfyui-folder-paths-import",
    "comfyui-comfy-import",
    "comfyui-manager-import",
]


class _ManifestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class VersionEvidence(_ManifestModel):
    intended: str
    observed: str

    @field_validator("intended", "observed")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return validate_exact_stable_distribution_version(value)

    @model_validator(mode="after")
    def _validate_equality(self) -> VersionEvidence:
        if self.intended != self.observed:
            raise ValueError("intended and observed versions must match")
        return self


class InventoryDistribution(_ManifestModel):
    name: str
    version: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_normalized_package(value)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        try:
            canonical = str(Version(value))
        except InvalidVersion as error:
            raise ValueError("inventory version must be valid PEP 440") from error
        if canonical != value:
            raise ValueError("inventory version must be canonical PEP 440")
        return value


class ImageEvidence(_ManifestModel):
    role: Literal["cuda-base", "uv-tool"]
    repository: str
    tag: str
    descriptor_digest: str
    descriptor_kind: Literal["index", "manifest"]
    platform: Literal["linux/amd64"]

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
    def _validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)


class PlatformEvidence(_ManifestModel):
    platform: Literal["linux/amd64"]
    backend: Literal["cuda"]
    backend_version: str
    channel: str
    cuda_image: ImageEvidence
    uv_image: ImageEvidence

    @field_validator("backend_version")
    @classmethod
    def _validate_backend_version(cls, value: str) -> str:
        return validate_exact_stable_version(value)

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, value: str) -> str:
        if re.fullmatch(r"cu[0-9]+", value) is None:
            raise ValueError("backend channel must be canonical")
        return value


class ToolEnvironmentEvidence(_ManifestModel):
    name: str
    environment: str
    direct: VersionEvidence
    inventory: tuple[InventoryDistribution, ...]
    dependency_check: Literal["passed"]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_normalized_package(value)

    @model_validator(mode="after")
    def _validate_inventory(self) -> ToolEnvironmentEvidence:
        names = tuple(item.name for item in self.inventory)
        if names != tuple(sorted(set(names))):
            raise ValueError("tool inventory must be sorted and unique")
        if not any(
            item.name == self.name and item.version == self.direct.observed
            for item in self.inventory
        ):
            raise ValueError("tool inventory does not contain its direct identity")
        return self


class CdhToolEnvironmentEvidence(ToolEnvironmentEvidence):
    name: Literal["comfyui-docker-helper"]
    environment: Literal["uv-tool:comfyui-docker-helper"]
    wheel_digest: str

    @field_validator("wheel_digest")
    @classmethod
    def _validate_wheel_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)


class ComfyCliEvidence(ToolEnvironmentEvidence):
    name: Literal["comfy-cli"]
    environment: Literal["uv-tool:comfy-cli"]
    entrypoints: tuple[Literal["comfy", "comfy-cli", "comfycli"], ...]

    @field_validator("entrypoints")
    @classmethod
    def _validate_entrypoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ("comfy", "comfy-cli", "comfycli"):
            raise ValueError("comfy-cli entrypoints must match the owned command set")
        return value


class ToolchainEvidence(_ManifestModel):
    container_uv: VersionEvidence
    container_uvx: VersionEvidence
    python: VersionEvidence
    python_provider: Literal["uv-managed"]
    python_catalog_descriptor_digest: str
    cdh: CdhToolEnvironmentEvidence
    comfy_cli: ComfyCliEvidence | None = None
    uv_tools: tuple[ToolEnvironmentEvidence, ...]

    @field_validator("python_catalog_descriptor_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)

    @model_validator(mode="after")
    def _validate_tool_order(self) -> ToolchainEvidence:
        names = tuple(tool.name for tool in self.uv_tools)
        if names != tuple(sorted(set(names))):
            raise ValueError("uv tools must be sorted and unique")
        return self


class ProtectedRequirementEvidence(_ManifestModel):
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


class ComfyUISourceEvidence(_ManifestModel):
    repository: str
    intended_commit: str
    observed_commit: str
    floor_commit: str
    formal_release: str | None = None
    requirements_intended_digest: str
    requirements_observed_digest: str
    protected: tuple[ProtectedRequirementEvidence, ...]

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        return validate_git_url(value)

    @field_validator("intended_commit", "observed_commit", "floor_commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return validate_git_commit(value)

    @field_validator("formal_release")
    @classmethod
    def _validate_release(cls, value: str | None) -> str | None:
        return None if value is None else validate_exact_stable_version(value)

    @field_validator("requirements_intended_digest", "requirements_observed_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)

    @model_validator(mode="after")
    def _validate_equality(self) -> ComfyUISourceEvidence:
        if self.intended_commit != self.observed_commit:
            raise ValueError("ComfyUI observed commit must match the intended commit")
        if self.requirements_intended_digest != self.requirements_observed_digest:
            raise ValueError("ComfyUI requirements digest must match")
        return self


class SetuptoolsEvidence(_ManifestModel):
    compatibility: str
    observed: str

    @field_validator("compatibility")
    @classmethod
    def _validate_compatibility(cls, value: str) -> str:
        parsed = SpecifierSet(value)
        if not value or str(parsed) != value:
            raise ValueError("setuptools compatibility must be canonical")
        return value

    @field_validator("observed")
    @classmethod
    def _validate_observed(cls, value: str) -> str:
        return validate_exact_stable_distribution_version(value)

    @model_validator(mode="after")
    def _validate_satisfaction(self) -> SetuptoolsEvidence:
        if not SpecifierSet(self.compatibility).contains(self.observed):
            raise ValueError("observed setuptools does not satisfy compatibility")
        return self


class EnabledManagerEvidence(_ManifestModel):
    enabled: Literal[True]
    distribution: Literal["comfyui-manager"]
    version: VersionEvidence
    import_name: Literal["comfyui_manager"]
    executable: Literal["/opt/venv/bin/cm-cli"]
    registry_control: Literal["direct-cm-cli"]


class DisabledManagerEvidence(_ManifestModel):
    enabled: Literal[False]
    observed: Literal["absent"]


class FinalBuildProbeEvidence(_ManifestModel):
    stage: Literal["final-build"]
    result: Literal["passed"]
    checks: tuple[FinalBuildCheckId, ...]


def final_build_check_ids(
    direct_packages: tuple[str, ...],
    *,
    manager_enabled: bool,
) -> tuple[FinalBuildCheckId, ...]:
    """Return the exact ordered probe checks selected by admitted intent."""
    members = set(direct_packages)
    checks: list[FinalBuildCheckId] = ["torch-import", "torch-cpu-tensor"]
    if "torchvision" in members:
        checks.append("torchvision-import")
    if "torchaudio" in members:
        checks.extend(("torchaudio-import", "torchaudio-cpu-resample"))
    checks.extend(("comfyui-folder-paths-import", "comfyui-comfy-import"))
    if manager_enabled:
        checks.append("comfyui-manager-import")
    return tuple(checks)


class ApplicationEvidence(_ManifestModel):
    pip: VersionEvidence
    direct_packages: tuple[tuple[str, VersionEvidence], ...]
    setuptools: SetuptoolsEvidence | None = None
    inventory: tuple[InventoryDistribution, ...]
    dependency_check: Literal["passed"]
    source: ComfyUISourceEvidence
    manager: EnabledManagerEvidence | DisabledManagerEvidence
    final_probe: FinalBuildProbeEvidence

    @model_validator(mode="after")
    def _validate_application(self) -> ApplicationEvidence:
        direct_names = tuple(name for name, _identity in self.direct_packages)
        direct_names = tuple(validate_normalized_package(name) for name in direct_names)
        if direct_names != tuple(sorted(set(direct_names))):
            raise ValueError("application direct packages must be sorted and unique")
        inventory_names = tuple(item.name for item in self.inventory)
        if inventory_names != tuple(sorted(set(inventory_names))):
            raise ValueError("application inventory must be sorted and unique")
        inventory = {item.name: item.version for item in self.inventory}
        for name, identity in self.direct_packages:
            if inventory.get(name) != identity.observed:
                raise ValueError("application inventory does not match direct identity")
        if inventory.get("pip") != self.pip.observed:
            raise ValueError("application inventory does not match pip")
        if self.setuptools is not None and (
            inventory.get("setuptools") != self.setuptools.observed
        ):
            raise ValueError("application inventory does not match setuptools")
        if isinstance(self.manager, EnabledManagerEvidence):
            if (
                inventory.get(self.manager.distribution)
                != self.manager.version.observed
            ):
                raise ValueError("application inventory does not match Manager")
        elif "comfyui-manager" in inventory:
            raise ValueError("disabled Manager must be absent from inventory")
        expected_checks = final_build_check_ids(
            direct_names,
            manager_enabled=isinstance(self.manager, EnabledManagerEvidence),
        )
        if self.final_probe.checks != expected_checks:
            raise ValueError("final probe checks do not match the application intent")
        return self


class FileEvidence(_ManifestModel):
    url: str
    target: str
    verification: Literal["sha256", "unverified-moving"]
    intended_checksum: str | None = None
    observed_checksum: str | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return validate_http_url(value, "file URL")

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or path.as_posix() != value:
            raise ValueError("file target must be one canonical absolute path")
        return value

    @field_validator("intended_checksum", "observed_checksum")
    @classmethod
    def _validate_checksum(cls, value: str | None) -> str | None:
        return None if value is None else validate_canonical_file_checksum(value)

    @model_validator(mode="after")
    def _validate_verification(self) -> FileEvidence:
        expected = self.intended_checksum
        observed = self.observed_checksum
        if self.verification == "sha256":
            if expected is None or expected != observed:
                raise ValueError("verified file checksum must match")
        elif expected is not None or observed is not None:
            raise ValueError("moving file evidence must omit content checksums")
        return self


class HookEvidence(_ManifestModel):
    domain: Literal["build", "runtime"]
    relative_path: str
    intended_digest: str
    observed_digest: str
    effects: Literal["trusted-opaque"]

    @field_validator("relative_path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_hook_relative_path(value)

    @field_validator("intended_digest", "observed_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return validate_hook_digest(value)

    @model_validator(mode="after")
    def _validate_equality(self) -> HookEvidence:
        if self.intended_digest != self.observed_digest:
            raise ValueError("materialized hook digest must match")
        return self


class AptPackageEvidence(_ManifestModel):
    name: str
    observed_version: str
    resolution: Literal["external-moving"]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_apt_package_identity(value)

    @field_validator("observed_version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not value or has_control_characters(value):
            raise ValueError("APT observed version must be one safe non-empty value")
        return value


class DigestEvidence(_ManifestModel):
    intended: str
    observed: str

    @field_validator("intended", "observed")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)

    @model_validator(mode="after")
    def _validate_equality(self) -> DigestEvidence:
        if self.intended != self.observed:
            raise ValueError("materialized input digest must match")
        return self


class MaterializedInputsEvidence(_ManifestModel):
    comfyui_requirements: DigestEvidence


class LifecycleEvidence(_ManifestModel):
    tini_executable: Literal["/usr/bin/tini"]
    tini_observed_version: str
    stop_signal: Literal["SIGTERM"]
    entrypoint: tuple[
        Literal[
            "/usr/bin/tini",
            "--",
            "/opt/uv/bin/cdh",
            "container",
            "runtime",
            "serve",
        ],
        ...,
    ]
    shutdown_timeout: ShutdownTimeout

    @field_validator("entrypoint")
    @classmethod
    def _validate_entrypoint(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        expected = (
            "/usr/bin/tini",
            "--",
            "/opt/uv/bin/cdh",
            "container",
            "runtime",
            "serve",
        )
        if value != expected:
            raise ValueError("entrypoint does not match the image init contract")
        return value

    @field_validator("tini_observed_version")
    @classmethod
    def _validate_tini_version(cls, value: str) -> str:
        if not value or has_control_characters(value):
            raise ValueError("Tini observed version must be one safe non-empty value")
        return value


class FinalManifest(_ManifestModel):
    schema_version: Literal[1]
    binding: ManifestBinding
    platform: PlatformEvidence
    toolchain: ToolchainEvidence
    application: ApplicationEvidence
    custom_nodes: CustomNodeInventory
    files: tuple[FileEvidence, ...]
    hooks: tuple[HookEvidence, ...]
    apt: tuple[AptPackageEvidence, ...]
    materialized_inputs: MaterializedInputsEvidence
    lifecycle: LifecycleEvidence


def dump_final_manifest(manifest: FinalManifest) -> bytes:
    """Serialize one manifest to canonical ASCII-safe JSON plus a newline."""
    return (
        json.dumps(
            manifest.model_dump(mode="json", exclude_none=True),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def parse_final_manifest(document: str | bytes) -> FinalManifest:
    """Parse one strict manifest schema v1 document."""
    return FinalManifest.model_validate_json(document)
