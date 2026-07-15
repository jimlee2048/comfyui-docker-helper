"""Deterministic target-platform and CUDA image selection."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from comfyui_docker_helper.config.final_models import (
    CudaImageDistro,
    CudaImageFlavor,
)
from comfyui_docker_helper.exact_ledger import CUDA_IMAGE_REPOSITORY


class TargetPlatform(StrEnum):
    """Container targets supported by the current build plan."""

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
