"""Strict admission of one canonical materialized BuildPlan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    BuildPlan,
    CustomNodesPhase,
    FilesPhase,
    ManifestBinding,
    ToolchainPhase,
    build_plan_digest,
    build_plan_hook_identities,
    manifest_binding,
    parse_build_plan_json,
)
from comfyui_docker_helper.config.final_manifest import (
    FinalBuildCheckId,
    final_build_check_ids,
)
from comfyui_docker_helper.config.shutdown_timeout import ShutdownTimeout
from comfyui_docker_helper.file_admission import read_regular_absolute_file

MATERIALIZED_BUILD_PLAN_PATH = Path("/opt/cdh/build/build-plan.json")


@dataclass(frozen=True, slots=True)
class FinalManifestInput:
    """Purpose-specific final observation projection from one admitted plan."""

    binding: ManifestBinding
    toolchain: ToolchainPhase
    application: ApplicationPhase
    custom_nodes: CustomNodesPhase
    files: tuple[FinalManifestFileInput, ...]
    materialized_hooks: tuple[FinalManifestHookInput, ...]
    final_probe: FinalCoreProbeInput
    shutdown_timeout: ShutdownTimeout


@dataclass(frozen=True, slots=True)
class FinalManifestHookInput:
    """One already-validated baked hook identity for final observation."""

    domain: Literal["build", "runtime"]
    relative_path: str
    digest: str


@dataclass(frozen=True, slots=True)
class FinalManifestFileInput:
    """Final file identity without downloader execution policy."""

    url: str
    target: str
    checksum: str | None


@dataclass(frozen=True, slots=True)
class FinalCoreProbeInput:
    """Narrow final-probe intent derived from one admitted BuildPlan."""

    workspace: str
    checks: tuple[FinalBuildCheckId, ...]


@dataclass(frozen=True, slots=True)
class BuildPlanInputAdmission:
    """Invocation-scoped admission with command-specific typed projections."""

    _plan: BuildPlan

    @classmethod
    def from_path(
        cls,
        build_plan_path: str | Path,
        *,
        expected_build_plan_digest: str,
    ) -> BuildPlanInputAdmission:
        try:
            plan = parse_build_plan_json(read_regular_absolute_file(build_plan_path))
        except ValidationError as error:
            raise ValueError("canonical BuildPlan is invalid") from error
        except (OSError, ValueError) as error:
            raise ValueError("could not read canonical BuildPlan") from error
        if build_plan_digest(plan) != expected_build_plan_digest:
            raise ValueError("canonical BuildPlan does not match the expected digest")
        return cls(plan)

    def comfyui_install(self) -> tuple[ApplicationPhase, ToolchainPhase]:
        """Project only the application installer inputs."""
        return self._plan.application, self._plan.toolchain

    def custom_node_install(self) -> tuple[CustomNodesPhase, ApplicationPhase]:
        """Project only the custom-node installer inputs."""
        return self._plan.custom_nodes, self._plan.application

    def file_downloads(self) -> tuple[FilesPhase, str]:
        """Project file policy with its authoritative ComfyUI root."""
        return self._plan.files, self._plan.application.paths.comfyui

    def final_manifest(self) -> FinalManifestInput:
        """Project only the complete cross-domain final observation inputs."""
        build_hooks, runtime_hooks = build_plan_hook_identities(
            self._plan.custom_nodes,
            self._plan.runtime,
        )
        hooks = tuple(
            FinalManifestHookInput(
                domain=domain,
                relative_path=identity.removeprefix(prefix),
                digest=hook.digest,
            )
            for domain, prefix, items in (
                ("build", "build-hooks/", build_hooks),
                ("runtime", "runtime-hooks/", runtime_hooks),
            )
            for identity, hook in sorted(items.items())
        )
        toolchain = self._plan.toolchain
        tool_store = toolchain.tool_store.model_copy(
            update={
                "uv_tools": tuple(
                    sorted(toolchain.tool_store.uv_tools, key=lambda tool: tool.name)
                )
            }
        )
        return FinalManifestInput(
            binding=manifest_binding(self._plan),
            toolchain=toolchain.model_copy(update={"tool_store": tool_store}),
            application=self._plan.application,
            custom_nodes=self._plan.custom_nodes,
            files=tuple(
                FinalManifestFileInput(
                    url=item.url,
                    target=item.target,
                    checksum=item.checksum,
                )
                for item in self._plan.files.files
            ),
            materialized_hooks=hooks,
            final_probe=FinalCoreProbeInput(
                workspace=self._plan.application.paths.comfyui,
                checks=final_build_check_ids(
                    tuple(
                        package.name
                        for package in self._plan.application.pytorch.packages
                    ),
                    manager_enabled=self._plan.application.comfyui.manager is not None,
                ),
            ),
            shutdown_timeout=self._plan.runtime.shutdown_timeout,
        )
