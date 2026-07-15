"""Typed machine authority for build-fixture acceptance scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class ScenarioClass(StrEnum):
    RELEASE = "release"
    CANARY = "canary"
    COMPONENT = "component"


class Capability(StrEnum):
    APPLICATION = "application"
    CLI = "cli"
    MANAGER = "manager"
    CUSTOM_NODES = "custom-nodes"
    HOOKS = "hooks"
    FILES = "files"
    CPU_AUDIO = "cpu-audio"
    GPU_AUDIO = "gpu-audio"


class Cost(StrEnum):
    NETWORK = "network"
    DOCKER = "docker"
    GPU = "gpu"
    SLOW = "slow"


@dataclass(frozen=True, slots=True)
class AcceptanceScenario:
    id: str
    fixture: str
    python_version: Literal["3.12.13", "3.13.14", "3.14.6"]
    classification: ScenarioClass
    capabilities: frozenset[Capability]
    costs: frozenset[Cost]
    image_variable: str | None
    context_variable: str | None
    image_flavor: Literal["base", "runtime", "devel", "cudnn-runtime", "cudnn-devel"]
    image_distro: Literal["ubuntu22.04", "ubuntu24.04"]

    @property
    def install_cli(self) -> bool:
        return Capability.CLI in self.capabilities

    @property
    def install_manager(self) -> bool:
        return Capability.MANAGER in self.capabilities

    @property
    def mixed(self) -> bool:
        return Capability.CUSTOM_NODES in self.capabilities

    @property
    def hooks(self) -> bool:
        return Capability.HOOKS in self.capabilities


_BASE_COSTS = frozenset({Cost.NETWORK, Cost.DOCKER, Cost.SLOW})
_FULL_CAPABILITIES = frozenset(
    {
        Capability.APPLICATION,
        Capability.CLI,
        Capability.MANAGER,
        Capability.CUSTOM_NODES,
        Capability.HOOKS,
        Capability.CPU_AUDIO,
        Capability.GPU_AUDIO,
    }
)

ACCEPTANCE_SCENARIOS = (
    AcceptanceScenario(
        "py313-full",
        "application-full.toml",
        "3.13.14",
        ScenarioClass.RELEASE,
        _FULL_CAPABILITIES,
        _BASE_COSTS | {Cost.GPU},
        "CDH_APPLICATION_FULL_IMAGE",
        "CDH_APPLICATION_FULL_CONTEXT",
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "py313-zero",
        "application-zero.toml",
        "3.13.14",
        ScenarioClass.RELEASE,
        frozenset({Capability.APPLICATION, Capability.CPU_AUDIO}),
        _BASE_COSTS,
        "CDH_APPLICATION_ZERO_IMAGE",
        "CDH_APPLICATION_ZERO_CONTEXT",
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "py313-manager-off-cli-on",
        "application-manager-disabled.toml",
        "3.13.14",
        ScenarioClass.RELEASE,
        frozenset({Capability.APPLICATION, Capability.CLI, Capability.CPU_AUDIO}),
        _BASE_COSTS,
        "CDH_APPLICATION_MANAGER_DISABLED_IMAGE",
        "CDH_APPLICATION_MANAGER_DISABLED_CONTEXT",
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "py313-cli-off-manager-on-mixed",
        "application-cli-disabled-mixed.toml",
        "3.13.14",
        ScenarioClass.RELEASE,
        frozenset(
            {
                Capability.APPLICATION,
                Capability.MANAGER,
                Capability.CUSTOM_NODES,
                Capability.HOOKS,
                Capability.CPU_AUDIO,
            }
        ),
        _BASE_COSTS,
        "CDH_APPLICATION_CLI_DISABLED_MIXED_IMAGE",
        "CDH_APPLICATION_CLI_DISABLED_MIXED_CONTEXT",
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "py312-full",
        "application-py312-full.toml",
        "3.12.13",
        ScenarioClass.RELEASE,
        _FULL_CAPABILITIES,
        _BASE_COSTS | {Cost.GPU},
        "CDH_APPLICATION_PY312_FULL_IMAGE",
        "CDH_APPLICATION_PY312_FULL_CONTEXT",
        "cudnn-devel",
        "ubuntu22.04",
    ),
    AcceptanceScenario(
        "py314-full",
        "application-py314-full.toml",
        "3.14.6",
        ScenarioClass.RELEASE,
        _FULL_CAPABILITIES,
        _BASE_COSTS | {Cost.GPU},
        "CDH_APPLICATION_PY314_FULL_IMAGE",
        "CDH_APPLICATION_PY314_FULL_CONTEXT",
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "latest",
        "latest.toml",
        "3.13.14",
        ScenarioClass.CANARY,
        frozenset({Capability.APPLICATION, Capability.CLI}),
        _BASE_COSTS,
        None,
        None,
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "nightly",
        "nightly.toml",
        "3.13.14",
        ScenarioClass.CANARY,
        frozenset({Capability.APPLICATION, Capability.CLI}),
        _BASE_COSTS,
        None,
        None,
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "manager-only",
        "manager-only.toml",
        "3.13.14",
        ScenarioClass.COMPONENT,
        frozenset({Capability.APPLICATION, Capability.MANAGER}),
        _BASE_COSTS,
        None,
        None,
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "registry-node",
        "registry-node.toml",
        "3.13.14",
        ScenarioClass.COMPONENT,
        frozenset(
            {
                Capability.APPLICATION,
                Capability.MANAGER,
                Capability.CUSTOM_NODES,
            }
        ),
        _BASE_COSTS,
        None,
        None,
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "git-node",
        "git-node.toml",
        "3.13.14",
        ScenarioClass.CANARY,
        frozenset(
            {
                Capability.APPLICATION,
                Capability.CLI,
                Capability.MANAGER,
                Capability.CUSTOM_NODES,
            }
        ),
        _BASE_COSTS,
        None,
        None,
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "hooks",
        "hooks.toml",
        "3.13.14",
        ScenarioClass.CANARY,
        frozenset(
            {
                Capability.APPLICATION,
                Capability.CLI,
                Capability.MANAGER,
                Capability.CUSTOM_NODES,
                Capability.HOOKS,
            }
        ),
        _BASE_COSTS,
        None,
        None,
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "httpx-files",
        "httpx-files.toml",
        "3.13.14",
        ScenarioClass.CANARY,
        frozenset({Capability.APPLICATION, Capability.CLI, Capability.FILES}),
        _BASE_COSTS,
        None,
        None,
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "aria2-files",
        "aria2-files.toml",
        "3.13.14",
        ScenarioClass.CANARY,
        frozenset({Capability.APPLICATION, Capability.CLI, Capability.FILES}),
        _BASE_COSTS,
        None,
        None,
        "cudnn-devel",
        "ubuntu24.04",
    ),
    AcceptanceScenario(
        "full-moving",
        "full.toml",
        "3.13.14",
        ScenarioClass.CANARY,
        frozenset(
            {
                Capability.APPLICATION,
                Capability.CLI,
                Capability.MANAGER,
                Capability.CUSTOM_NODES,
                Capability.HOOKS,
                Capability.FILES,
            }
        ),
        _BASE_COSTS,
        None,
        None,
        "cudnn-devel",
        "ubuntu24.04",
    ),
)

RELEASE_SCENARIOS = tuple(
    scenario
    for scenario in ACCEPTANCE_SCENARIOS
    if scenario.classification is ScenarioClass.RELEASE
)
