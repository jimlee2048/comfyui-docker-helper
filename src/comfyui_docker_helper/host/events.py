"""Immutable semantic events for operator-facing Host workflows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class HostPhase(Enum):
    """Retained user-domain phases in Host render and build preparation."""

    CONFIGURATION_VALIDATION = "configuration-validation"
    BUILD_INPUT_RESOLUTION = "build-input-resolution"
    LOCK_RECONCILIATION = "lock-reconciliation"
    BUILD_PLAN_PREPARATION = "build-plan-preparation"
    CONTEXT_RENDER_CHECK = "context-render-check"


class HostSubphase(Enum):
    """Safe implementation facts shown only at verbose detail or above."""

    CANONICAL_WHEEL_PREPARATION = "canonical-wheel-preparation"
    CANONICAL_IDENTITY_RECONCILIATION = "canonical-identity-reconciliation"


@dataclass(frozen=True, slots=True)
class HostPhaseStarted:
    """One retained Host phase has begun."""

    phase: HostPhase


@dataclass(frozen=True, slots=True)
class HostPhaseCompleted:
    """One retained Host phase completed successfully."""

    phase: HostPhase


@dataclass(frozen=True, slots=True)
class HostSubphaseStarted:
    """One safe implementation subphase has begun."""

    subphase: HostSubphase


@dataclass(frozen=True, slots=True)
class HostSubphaseCompleted:
    """One safe implementation subphase completed successfully."""

    subphase: HostSubphase


@dataclass(frozen=True, slots=True)
class HostPhaseFailed:
    """The current retained Host phase failed."""

    phase: HostPhase


@dataclass(frozen=True, slots=True)
class HostPhaseInterrupted:
    """The current retained Host phase was interrupted by the operator."""

    phase: HostPhase


@dataclass(frozen=True, slots=True)
class HostWorkflowSucceeded:
    """All retained Host phases completed successfully."""


type HostWorkflowTerminalEvent = (
    HostWorkflowSucceeded | HostPhaseFailed | HostPhaseInterrupted
)
type HostWorkflowEvent = (
    HostPhaseStarted
    | HostPhaseCompleted
    | HostSubphaseStarted
    | HostSubphaseCompleted
    | HostWorkflowTerminalEvent
)
