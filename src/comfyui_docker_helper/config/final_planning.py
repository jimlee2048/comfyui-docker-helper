"""Deterministic platform and backend planning for the active authority."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from comfyui_docker_helper.config.diagnostics import DiagnosticError
from comfyui_docker_helper.config.final_models import (
    CudaImageDistro,
    CudaImageFlavor,
    FinalConfig,
)
from comfyui_docker_helper.config.final_validation import (
    NormalizedRequirement,
    validate_final_config,
    validate_final_config_domains,
)
from comfyui_docker_helper.exact_ledger import CUDA_IMAGE_REPOSITORY


class TargetPlatform(StrEnum):
    """Container target values supported by the v0.5 planning domain."""

    LINUX_AMD64 = "linux/amd64"

    @property
    def operating_system(self) -> Literal["linux"]:
        return "linux"

    @property
    def architecture(self) -> Literal["amd64"]:
        return "amd64"


@dataclass(frozen=True, slots=True)
class CudaVersion:
    """One structurally validated CUDA version and its channel components."""

    value: str
    major: str
    minor: str
    patch: str | None

    @classmethod
    def from_validated(cls, value: str) -> "CudaVersion":
        """Preserve the full image version while exposing channel components."""
        major, minor, *remainder = value.split(".")
        return cls(
            value=value,
            major=major,
            minor=minor,
            patch=remainder[0] if remainder else None,
        )


@dataclass(frozen=True, slots=True)
class BackendPlan:
    """Backend-derived inputs shared by later identity and build planning."""

    backend: Literal["cuda"]
    version: CudaVersion
    target_platform: TargetPlatform
    base_image: str
    package_channel: str


class BackendAdapter(Protocol):
    """Typed seam for one compute backend's deterministic derivation rules."""

    def derive(
        self,
        version: CudaVersion,
        target_platform: TargetPlatform,
        *,
        image_flavor: CudaImageFlavor,
        image_distro: CudaImageDistro,
    ) -> BackendPlan: ...


@dataclass(frozen=True, slots=True)
class CudaBackendAdapter:
    """Construct the NVIDIA tag while deriving its PyTorch channel from version."""

    def derive(
        self,
        version: CudaVersion,
        target_platform: TargetPlatform,
        *,
        image_flavor: CudaImageFlavor,
        image_distro: CudaImageDistro,
    ) -> BackendPlan:
        return BackendPlan(
            backend="cuda",
            version=version,
            target_platform=target_platform,
            base_image=(
                f"{CUDA_IMAGE_REPOSITORY}:{version.value}-{image_flavor}-{image_distro}"
            ),
            package_channel=f"cu{version.major}{version.minor}",
        )


@dataclass(frozen=True, slots=True)
class PackageRequirementRequest:
    """One normalized direct requirement inside a cohesive resolver group."""

    name: str
    extras: tuple[str, ...]
    selector: str


@dataclass(frozen=True, slots=True)
class PackageGroupRequest:
    """Complete backend package group that the resolver must solve once."""

    owner: Literal["pytorch"]
    environment: Literal["application"]
    python_version: str
    target_platform: TargetPlatform
    index_url: str
    package_channel: str
    requirements: tuple[PackageRequirementRequest, ...]


@dataclass(frozen=True, slots=True)
class FinalPlanningDomain:
    """Deterministic platform/backend planning result."""

    target_platforms: tuple[TargetPlatform, ...]
    backend: BackendPlan
    pytorch_group: PackageGroupRequest


class FinalPlanningError(DiagnosticError):
    """Expected invalid-config failure at the planning boundary."""


def build_final_planning_domain(
    config: FinalConfig,
    *,
    backend_adapter: BackendAdapter | None = None,
) -> FinalPlanningDomain:
    """Construct target/backend/group requests without providers or I/O."""
    diagnostics = validate_final_config(config)
    if diagnostics:
        raise FinalPlanningError(diagnostics)

    domains = validate_final_config_domains(config)
    target_platforms = tuple(TargetPlatform(value) for value in config.build.platforms)
    target_platform = target_platforms[0]
    backend = (backend_adapter or CudaBackendAdapter()).derive(
        CudaVersion.from_validated(config.compute_platform.cuda.version),
        target_platform,
        image_flavor=config.compute_platform.cuda.image_flavor,
        image_distro=config.compute_platform.cuda.image_distro,
    )
    pytorch_extras = tuple(
        _requirement_request(requirement)
        for requirement in domains.package_requirements
        if requirement.path[:2] == ("pytorch", "extra_packages")
    )
    index_url = f"{config.pytorch.index_base_url.rstrip('/')}/{backend.package_channel}"
    pytorch_group = PackageGroupRequest(
        owner="pytorch",
        environment="application",
        python_version=config.python.version,
        target_platform=target_platform,
        index_url=index_url,
        package_channel=backend.package_channel,
        requirements=(
            PackageRequirementRequest(
                name="torch",
                extras=(),
                selector=f"=={config.pytorch.version}",
            ),
            *pytorch_extras,
        ),
    )
    return FinalPlanningDomain(target_platforms, backend, pytorch_group)


def _requirement_request(
    requirement: NormalizedRequirement,
) -> PackageRequirementRequest:
    return PackageRequirementRequest(
        name=requirement.name,
        extras=requirement.extras,
        selector=requirement.specifier,
    )
