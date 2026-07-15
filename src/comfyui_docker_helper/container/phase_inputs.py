"""Strict BuildPlan-derived phase admission without config or lock re-planning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    BuildOutputPlan,
    BuildPlan,
    CustomNodesPhase,
    FilesPhase,
    RuntimePhase,
    ToolchainPhase,
    build_plan_digest,
    parse_build_plan_json,
)
from comfyui_docker_helper.container.file_admission import read_regular_absolute_file

_PHASE_SCHEMA_VERSION = 1
MATERIALIZED_BUILD_PLAN_PATH = Path("/opt/cdh/build/build-plan.json")
type PhasePayload = (
    BuildOutputPlan
    | ToolchainPhase
    | ApplicationPhase
    | CustomNodesPhase
    | FilesPhase
    | RuntimePhase
)


class _PhaseDocument(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )

    schema_version: Literal[1]
    build_plan_digest: str


class BuildPhaseDocument(_PhaseDocument):
    payload: BuildOutputPlan


class ToolchainPhaseDocument(_PhaseDocument):
    payload: ToolchainPhase


class ApplicationPhaseDocument(_PhaseDocument):
    payload: ApplicationPhase


class CustomNodesPhaseDocument(_PhaseDocument):
    payload: CustomNodesPhase


class FilesPhaseDocument(_PhaseDocument):
    payload: FilesPhase


class RuntimePhaseDocument(_PhaseDocument):
    payload: RuntimePhase


_PHASE_TYPES: dict[
    str,
    tuple[type[BaseModel], TypeAdapter[BaseModel]],
] = {
    "build": (BuildPhaseDocument, TypeAdapter(BuildPhaseDocument)),
    "toolchain": (ToolchainPhaseDocument, TypeAdapter(ToolchainPhaseDocument)),
    "application": (ApplicationPhaseDocument, TypeAdapter(ApplicationPhaseDocument)),
    "custom-nodes": (
        CustomNodesPhaseDocument,
        TypeAdapter(CustomNodesPhaseDocument),
    ),
    "files": (FilesPhaseDocument, TypeAdapter(FilesPhaseDocument)),
    "runtime": (RuntimePhaseDocument, TypeAdapter(RuntimePhaseDocument)),
}

_PLAN_PHASE_FIELDS = {
    "build": "build",
    "toolchain": "toolchain",
    "application": "application",
    "custom-nodes": "custom_nodes",
    "files": "files",
    "runtime": "runtime",
}


def phase_document(
    phase: str,
    payload: PhasePayload,
    build_plan_digest: str,
) -> BaseModel:
    """Wrap exactly one BuildPlan-owned payload for materialization."""
    document_type, _ = _phase_type(phase)
    return document_type(
        schema_version=_PHASE_SCHEMA_VERSION,
        build_plan_digest=build_plan_digest,
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class PhaseInputAdmission:
    """Invocation-scoped admission of narrow phases from one canonical plan."""

    _plan: BuildPlan
    _build_plan_digest: str

    @classmethod
    def from_path(
        cls,
        build_plan_path: str | Path,
        *,
        expected_build_plan_digest: str,
    ) -> PhaseInputAdmission:
        try:
            plan = parse_build_plan_json(read_regular_absolute_file(build_plan_path))
        except ValidationError as error:
            raise ValueError("canonical BuildPlan is invalid") from error
        except (OSError, ValueError) as error:
            raise ValueError("could not read canonical BuildPlan") from error
        observed_digest = build_plan_digest(plan)
        if observed_digest != expected_build_plan_digest:
            raise ValueError("canonical BuildPlan does not match the expected digest")
        return cls(plan, observed_digest)

    def load(self, path: str | Path, phase: str) -> PhasePayload:
        """Return one typed payload only after exact plan-field admission."""
        _, adapter = _phase_type(phase)
        try:
            document = adapter.validate_json(read_regular_absolute_file(path))
        except ValidationError:
            raise
        except (OSError, ValueError) as error:
            raise ValueError(f"could not read {phase} phase input") from error
        if document.build_plan_digest != self._build_plan_digest:
            raise ValueError(f"{phase} phase input belongs to a different BuildPlan")
        expected_payload = getattr(self._plan, _PLAN_PHASE_FIELDS[phase])
        if document.payload != expected_payload:
            raise ValueError(
                f"{phase} phase input does not match the canonical BuildPlan"
            )
        return document.payload


def _phase_type(phase: str) -> tuple[type[BaseModel], TypeAdapter[BaseModel]]:
    try:
        return _PHASE_TYPES[phase]
    except KeyError as error:
        raise ValueError(f"unknown phase {phase!r}") from error
