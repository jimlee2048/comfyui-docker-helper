"""Validation result types shared by the authored validation stages."""

from dataclasses import dataclass
from pathlib import PurePosixPath

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticError,
    DiagnosticPath,
)
from comfyui_docker_helper.config.validation.requirements import (
    DirectRequirementIdentity,
)


class FinalConfigError(DiagnosticError):
    """Expected final-config validation failure."""


@dataclass(frozen=True, slots=True)
class LocatedValue:
    """One domain-normalized value and its public diagnostic path."""

    path: DiagnosticPath
    value: str


@dataclass(frozen=True, slots=True)
class NormalizedFile:
    """Canonical target information for one admitted file."""

    relative_target: str


@dataclass(frozen=True, slots=True)
class NormalizedRequirement:
    """Resolution-relevant identity of one validated direct requirement."""

    path: DiagnosticPath
    value: str
    identity: DirectRequirementIdentity

    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def extras(self) -> tuple[str, ...]:
        return self.identity.extras

    @property
    def specifier(self) -> str:
        return self.identity.specifier

    @property
    def canonical_value(self) -> str:
        return self.identity.canonical_value


@dataclass(frozen=True, slots=True)
class FinalConfigDomainResult:
    """Focused domain-pass output consumed by the single semantic pass."""

    diagnostics: tuple[Diagnostic, ...]
    platforms: tuple[LocatedValue, ...]
    authored_package_requirements: tuple[NormalizedRequirement, ...]
    package_requirements: tuple[NormalizedRequirement, ...]
    authored_apt_packages: tuple[LocatedValue, ...]
    apt_packages: tuple[LocatedValue, ...]
    ssh_public_keys: tuple[str, ...]
    registry_ids: tuple[LocatedValue, ...]
    registry_nodes: tuple[DiagnosticPath, ...]
    git_urls: tuple[LocatedValue, ...]
    git_targets: tuple[LocatedValue, ...]
    file_targets: tuple[LocatedValue, ...]
    files: tuple[NormalizedFile, ...]
    controlled_extra_args: tuple[LocatedValue, ...]
    git_credential_contexts: tuple[LocatedValue, ...]
    downloader_credential_contexts: tuple[LocatedValue, ...]
    workspace: PurePosixPath | None
    comfyui_path: PurePosixPath | None
