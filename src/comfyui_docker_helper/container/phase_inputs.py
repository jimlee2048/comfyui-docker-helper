"""Strict BuildPlan-derived phase loaders; no root config or lock re-planning."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    BuildOutputPlan,
    CustomNodesPhase,
    FilesPhase,
    RuntimePhase,
    ToolchainPhase,
)

_PHASE_SCHEMA_VERSION = 1
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


def load_phase_input(
    path: str | Path,
    phase: str,
    *,
    expected_build_plan_digest: str,
) -> BaseModel:
    """Load one narrow phase and verify that it belongs to the expected plan."""
    _, adapter = _phase_type(phase)
    try:
        document = adapter.validate_json(Path(path).read_bytes())
    except OSError as error:
        raise ValueError(f"could not read {phase} phase input") from error
    if document.build_plan_digest != expected_build_plan_digest:
        raise ValueError(f"{phase} phase input belongs to a different BuildPlan")
    return document.payload


def _phase_type(phase: str) -> tuple[type[BaseModel], TypeAdapter[BaseModel]]:
    try:
        return _PHASE_TYPES[phase]
    except KeyError as error:
        raise ValueError(f"unknown phase {phase!r}") from error
