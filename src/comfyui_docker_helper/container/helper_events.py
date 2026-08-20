"""Immutable semantic events for serial Container build helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from comfyui_docker_helper.config.canonical_lock import (
    validate_exact_registry_version,
    validate_registry_id,
)
from comfyui_docker_helper.config.selector_validation import is_safe_git_target_dir


class ContainerHelperPhase(StrEnum):
    """User-meaningful long-work boundaries owned by Container helpers."""

    COMFYUI_SOURCE_CHECKOUT = "comfyui-source-checkout"
    COMFYUI_SOURCE_VERIFICATION = "comfyui-source-verification"
    PYTORCH_INSTALLATION = "pytorch-installation"
    PYTHON_EXTRAS_INSTALLATION = "python-extras-installation"
    COMFYUI_REQUIREMENTS_INSTALLATION = "comfyui-requirements-installation"
    MANAGER_INSTALLATION = "manager-installation"
    COMFYUI_FINAL_VERIFICATION = "comfyui-final-verification"
    CUSTOM_NODES_PREPARATION = "custom-nodes-preparation"
    CUSTOM_NODE_PRE_INSTALL = "custom-node-pre-install"
    CUSTOM_NODE_INSTALLATION = "custom-node-installation"
    CUSTOM_NODE_POST_INSTALL = "custom-node-post-install"
    CUSTOM_NODES_FINAL_VERIFICATION = "custom-nodes-final-verification"
    FINAL_STATE_VERIFICATION = "final-state-verification"
    FINAL_MANIFEST_WRITE = "final-manifest-write"


@dataclass(frozen=True, slots=True)
class ContainerHelperPhaseStarted:
    """Begin one serial helper phase."""

    phase: ContainerHelperPhase

    def __post_init__(self) -> None:
        _require_phase(self.phase)


@dataclass(frozen=True, slots=True)
class ContainerHelperPhaseCompleted:
    """Complete the current serial helper phase successfully."""

    phase: ContainerHelperPhase

    def __post_init__(self) -> None:
        _require_phase(self.phase)


@dataclass(frozen=True, slots=True)
class RegistryCustomNodeStarted:
    """Begin one Registry custom node in admitted order."""

    index: int
    total: int
    id: str
    version: str
    pre_hook_count: int
    post_hook_count: int

    def __post_init__(self) -> None:
        _require_item_position(self.index, self.total)
        _require_string(self.id, "Registry custom-node ID")
        _require_string(self.version, "Registry custom-node version")
        validate_registry_id(self.id)
        validate_exact_registry_version(self.version)
        _require_non_negative_integer(self.pre_hook_count, "pre-hook count")
        _require_non_negative_integer(self.post_hook_count, "post-hook count")


@dataclass(frozen=True, slots=True)
class GitCustomNodeStarted:
    """Begin one direct-Git custom node in admitted order."""

    index: int
    total: int
    target_name: str
    pre_hook_count: int
    post_hook_count: int

    def __post_init__(self) -> None:
        _require_item_position(self.index, self.total)
        if type(self.target_name) is not str or not is_safe_git_target_dir(
            self.target_name
        ):
            raise ValueError("Git custom-node target must be one safe target leaf")
        _require_non_negative_integer(self.pre_hook_count, "pre-hook count")
        _require_non_negative_integer(self.post_hook_count, "post-hook count")


@dataclass(frozen=True, slots=True)
class CustomNodeCompleted:
    """Complete one custom node after its final proof boundary."""

    index: int
    total: int

    def __post_init__(self) -> None:
        _require_item_position(self.index, self.total)


@dataclass(frozen=True, slots=True)
class ComfyUIInstallCompleted:
    """Complete the ComfyUI installation helper."""


@dataclass(frozen=True, slots=True)
class CustomNodesInstallCompleted:
    """Complete the custom-node installation helper."""

    node_count: int

    def __post_init__(self) -> None:
        _require_non_negative_integer(self.node_count, "custom-node count")


@dataclass(frozen=True, slots=True)
class FinalManifestCompleted:
    """Complete final-state observation and manifest publication."""


type ContainerHelperEvent = (
    ContainerHelperPhaseStarted
    | ContainerHelperPhaseCompleted
    | RegistryCustomNodeStarted
    | GitCustomNodeStarted
    | CustomNodeCompleted
    | ComfyUIInstallCompleted
    | CustomNodesInstallCompleted
    | FinalManifestCompleted
)


def _require_phase(value: object) -> None:
    if not isinstance(value, ContainerHelperPhase):
        raise ValueError("helper phase must be an admitted phase")


def _require_item_position(index: object, total: object) -> None:
    _require_positive_integer(index, "custom-node index")
    _require_positive_integer(total, "custom-node total")
    if index > total:
        raise ValueError("custom-node index must not exceed its total")


def _require_positive_integer(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _require_non_negative_integer(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _require_string(value: object, label: str) -> None:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
