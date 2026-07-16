"""Strict admission of one canonical materialized BuildPlan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    BuildPlan,
    CustomNodesPhase,
    FilesPhase,
    ToolchainPhase,
    build_plan_digest,
    parse_build_plan_json,
)
from comfyui_docker_helper.container.file_admission import read_regular_absolute_file

MATERIALIZED_BUILD_PLAN_PATH = Path("/opt/cdh/build/build-plan.json")


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
