"""Deterministic target-platform and CUDA image selection."""

from dataclasses import dataclass
from enum import StrEnum

from comfyui_docker_helper.config.final_models import (
    CudaImageDistro,
    CudaImageFlavor,
)
from comfyui_docker_helper.exact_ledger import (
    CUDA_IMAGE_REPOSITORY,
    CUDA_PROTECTED_REQUIREMENTS,
)


class TargetPlatform(StrEnum):
    """Container targets supported by the current build plan."""

    LINUX_AMD64 = "linux/amd64"


@dataclass(frozen=True, slots=True)
class CudaVersion:
    """One structurally validated CUDA version and its channel components."""

    value: str
    major: str
    minor: str

    @classmethod
    def from_validated(cls, value: str) -> "CudaVersion":
        """Preserve the full image version while exposing channel components."""
        major, minor, *_ = value.split(".")
        return cls(
            value=value,
            major=major,
            minor=minor,
        )


@dataclass(frozen=True, slots=True)
class BackendPlan:
    """Backend-derived inputs shared by later identity and build planning."""

    version: CudaVersion
    base_image: str
    package_channel: str


@dataclass(frozen=True, slots=True)
class CudaBackendAdapter:
    """Construct the NVIDIA tag while deriving its PyTorch channel from version."""

    @property
    def protected_requirement_names(self) -> tuple[str, ...]:
        """Return the complete direct-package source-ownership policy."""
        return tuple(sorted(CUDA_PROTECTED_REQUIREMENTS))

    def derive(
        self,
        version: CudaVersion,
        *,
        image_flavor: CudaImageFlavor,
        image_distro: CudaImageDistro,
    ) -> BackendPlan:
        return BackendPlan(
            version=version,
            base_image=(
                f"{CUDA_IMAGE_REPOSITORY}:{version.value}-{image_flavor}-{image_distro}"
            ),
            package_channel=f"cu{version.major}{version.minor}",
        )
