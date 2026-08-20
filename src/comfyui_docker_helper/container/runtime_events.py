"""Immutable semantic facts for the durable Container Runtime lifecycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from comfyui_docker_helper.config.hook_validation import validate_hook_relative_path
from comfyui_docker_helper.config.runtime_hooks import (
    RUNTIME_HOOK_PHASE_DIRECTORIES_BY_PHASE,
    RUNTIME_HOOK_SOURCE_NAMES,
)
from comfyui_docker_helper.container.download_events import (
    DownloadBackendName,
    DownloadRetryReason,
    DownloadRetryScheduled,
    DownloadTransferProgress,
)

_GENERATION_PATTERN = re.compile(r"gen-[1-9][0-9]*\Z")
_RUNTIME_DOWNLOAD_MODES = frozenset({"sync", "async"})
_RUNTIME_DOWNLOAD_FAILURE_POLICIES = frozenset({"continue", "fail"})

type RuntimeHookPhase = Literal["pre-start", "post-start", "stop"]
type RuntimeHookSource = Literal["baked", "mounted"]
type RuntimeDownloadMode = Literal["sync", "async"]
type RuntimeDownloadFailurePolicy = Literal["continue", "fail"]


class RuntimePhase(StrEnum):
    """Serial user-meaningful Runtime lifecycle boundaries."""

    RUNTIME_FILES_PREPARATION = "runtime-files-preparation"
    PRE_START_HOOKS = "pre-start-hooks"
    SSH_STARTUP = "ssh-startup"
    COMFYUI_STARTUP = "comfyui-startup"
    COMFYUI_READINESS = "comfyui-readiness"
    POST_START_HOOKS = "post-start-hooks"
    STOP_HOOKS = "stop-hooks"
    GENERATION_CLEANUP = "generation-cleanup"


class RuntimeGenerationOperation(StrEnum):
    """Controlled reason for admitting one generation."""

    INITIAL_START = "initial-start"
    OPERATOR_RESTART = "operator-restart"


class RuntimeGenerationStopCause(StrEnum):
    """Controlled reason for stopping one admitted generation."""

    NATURAL_EXIT = "natural-exit"
    EXTERNAL_SHUTDOWN = "external-shutdown"
    OPERATOR_RESTART = "operator-restart"
    STARTUP_FAILURE = "startup-failure"
    CONTROLLER_FAILURE = "controller-failure"


class RuntimeSshStatus(StrEnum):
    """Successful SSH lifecycle outcome."""

    DISABLED = "disabled"
    ENABLED_WITHOUT_CREDENTIALS = "enabled-without-credentials"
    READY = "ready"


class RuntimeDownloadQueue(StrEnum):
    """Runtime download queue owner."""

    SYNCHRONOUS = "synchronous"
    ASYNCHRONOUS = "asynchronous"


class RuntimeDownloadQueueState(StrEnum):
    """Durable state transition for one runtime download queue."""

    ACCEPTED = "accepted"
    COMPLETED = "completed"


class RuntimeDownloadQueueWarningKind(StrEnum):
    """Controlled asynchronous queue warning outcomes."""

    STOPPED_AFTER_FAILURE = "stopped-after-failure"
    FORCE_TERMINATION_REQUIRED = "force-termination-required"


class RuntimeDownloadProgressState(StrEnum):
    """Durable cadence state for one Runtime transfer snapshot."""

    ACTIVE = "active"
    STALLED = "stalled"
    RECOVERED = "recovered"


class RuntimeWarningCategory(StrEnum):
    """Finite warning categories retained when background delivery is full."""

    STALE_CLEANUP = "stale-cleanup"
    DOWNLOAD_FAILURE = "download-failure"
    SSH = "ssh"


class RuntimeSshWarningKind(StrEnum):
    """Controlled SSH warning facts."""

    STARTUP_TERMINATION_FAILED = "startup-termination-failed"
    SERVICE_TERMINATION_FAILED = "service-termination-failed"
    SERVICE_REAP_FAILED = "service-reap-failed"
    STARTUP_PROCESS_SIGNAL_FAILED = "startup-process-signal-failed"
    MONITOR_FAILED = "monitor-failed"
    EXITED_UNEXPECTEDLY = "exited-unexpectedly"
    SERVICE_SHUTDOWN_FAILED = "service-shutdown-failed"
    FORCE_TERMINATION_REQUIRED = "force-termination-required"
    DIRECTORY_MODE_NONSTANDARD = "directory-mode-nonstandard"
    AUTHORIZED_KEYS_MODE_NONSTANDARD = "authorized-keys-mode-nonstandard"


class RuntimeHookWarningKind(StrEnum):
    """Controlled hook warning facts."""

    TERMINATION_FAILED = "termination-failed"


@dataclass(frozen=True, slots=True)
class RuntimePhaseStarted:
    """Begin one serial Runtime phase."""

    phase: RuntimePhase

    def __post_init__(self) -> None:
        _require_enum(self.phase, RuntimePhase, "Runtime phase")


@dataclass(frozen=True, slots=True)
class RuntimePhaseCompleted:
    """Complete one serial Runtime phase successfully."""

    phase: RuntimePhase

    def __post_init__(self) -> None:
        _require_enum(self.phase, RuntimePhase, "Runtime phase")


@dataclass(frozen=True, slots=True)
class RuntimePhaseFailed:
    """End one active Runtime phase without claiming successful completion."""

    phase: RuntimePhase

    def __post_init__(self) -> None:
        _require_enum(self.phase, RuntimePhase, "Runtime phase")


@dataclass(frozen=True, slots=True)
class RuntimeGenerationAdmitted:
    """Admit one initial or replacement generation."""

    generation: str
    operation: RuntimeGenerationOperation

    def __post_init__(self) -> None:
        _require_generation(self.generation)
        _require_enum(
            self.operation,
            RuntimeGenerationOperation,
            "Runtime generation operation",
        )


@dataclass(frozen=True, slots=True)
class RuntimeGenerationReady:
    """Mark one admitted generation ready for traffic."""

    generation: str

    def __post_init__(self) -> None:
        _require_generation(self.generation)


@dataclass(frozen=True, slots=True)
class RuntimeGenerationStopping:
    """Begin owned teardown of one generation."""

    generation: str

    def __post_init__(self) -> None:
        _require_generation(self.generation)


@dataclass(frozen=True, slots=True)
class RuntimeGenerationStopped:
    """Complete shutdown and cleanup of one generation."""

    generation: str
    cause: RuntimeGenerationStopCause

    def __post_init__(self) -> None:
        _require_generation(self.generation)
        _require_enum(self.cause, RuntimeGenerationStopCause, "Runtime stop cause")


@dataclass(frozen=True, slots=True)
class RuntimeHookStarted:
    """Begin one safe discovered runtime hook."""

    index: int
    total: int
    phase: RuntimeHookPhase
    source: RuntimeHookSource
    filename: str

    def __post_init__(self) -> None:
        _require_position(self.index, self.total, "Runtime hook")
        _require_hook_scope(self.phase, self.source, self.filename)


@dataclass(frozen=True, slots=True)
class RuntimeHookCompleted:
    """Complete one safe discovered runtime hook."""

    index: int
    total: int
    phase: RuntimeHookPhase
    source: RuntimeHookSource
    filename: str

    def __post_init__(self) -> None:
        _require_position(self.index, self.total, "Runtime hook")
        _require_hook_scope(self.phase, self.source, self.filename)


@dataclass(frozen=True, slots=True)
class RuntimeSshOutcome:
    """Report one successful SSH lifecycle outcome."""

    status: RuntimeSshStatus

    def __post_init__(self) -> None:
        _require_enum(self.status, RuntimeSshStatus, "Runtime SSH status")


@dataclass(frozen=True, slots=True)
class RuntimeDownloadReconciled:
    """Summarize one persisted runtime download reconciliation."""

    desired_count: int
    scheduled_sync_count: int
    scheduled_async_count: int
    already_present_count: int
    stale_count: int
    cleanup_pending_count: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.desired_count, "desired Runtime download count"),
            (self.scheduled_sync_count, "scheduled synchronous download count"),
            (self.scheduled_async_count, "scheduled asynchronous download count"),
            (self.already_present_count, "already-present Runtime download count"),
            (self.stale_count, "stale Runtime download count"),
            (self.cleanup_pending_count, "pending cleanup count"),
        ):
            _require_non_negative_integer(value, label)
        accounted = (
            self.scheduled_sync_count
            + self.scheduled_async_count
            + self.already_present_count
        )
        if accounted != self.desired_count:
            raise ValueError("Accounted Runtime downloads must equal desired files")
        if self.cleanup_pending_count > self.stale_count:
            raise ValueError("Pending cleanup count must not exceed stale files")


@dataclass(frozen=True, slots=True)
class RuntimeDownloadQueueSummary:
    """Summarize one runtime download queue transition."""

    queue: RuntimeDownloadQueue
    state: RuntimeDownloadQueueState
    item_count: int

    def __post_init__(self) -> None:
        _require_enum(self.queue, RuntimeDownloadQueue, "Runtime download queue")
        _require_enum(self.state, RuntimeDownloadQueueState, "Runtime queue state")
        _require_non_negative_integer(self.item_count, "Runtime queue item count")


@dataclass(frozen=True, slots=True)
class RuntimeDownloadAttemptStarted:
    """Begin one counted Runtime file transfer attempt."""

    index: int
    total: int
    target: str
    mode: RuntimeDownloadMode
    backend: DownloadBackendName
    attempt: int
    max_attempts: int

    def __post_init__(self) -> None:
        _require_runtime_download_item(
            self.index,
            self.total,
            self.target,
            self.mode,
            self.attempt,
            self.max_attempts,
        )
        _require_enum(self.backend, DownloadBackendName, "Runtime download backend")


@dataclass(frozen=True, slots=True)
class RuntimeDownloadItemProgress:
    """Wrap one shared transfer progress fact with its Runtime item identity."""

    index: int
    total: int
    target: str
    mode: RuntimeDownloadMode
    attempt: int
    max_attempts: int
    progress: DownloadTransferProgress
    state: RuntimeDownloadProgressState = RuntimeDownloadProgressState.ACTIVE

    def __post_init__(self) -> None:
        _require_runtime_download_item(
            self.index,
            self.total,
            self.target,
            self.mode,
            self.attempt,
            self.max_attempts,
        )
        if not isinstance(self.progress, DownloadTransferProgress):
            raise ValueError(
                "Runtime download progress must be a transfer progress fact"
            )
        _require_enum(
            self.state,
            RuntimeDownloadProgressState,
            "Runtime download progress state",
        )


@dataclass(frozen=True, slots=True)
class RuntimeDownloadItemRetryScheduled:
    """Wrap one shared retry fact with its Runtime item identity."""

    index: int
    total: int
    target: str
    mode: RuntimeDownloadMode
    max_attempts: int
    retry: DownloadRetryScheduled

    def __post_init__(self) -> None:
        _require_runtime_download_identity(
            self.index, self.total, self.target, self.mode, self.max_attempts
        )
        if not isinstance(self.retry, DownloadRetryScheduled):
            raise ValueError("Runtime download retry must be a retry fact")
        if self.retry.next_attempt > self.max_attempts:
            raise ValueError("Runtime download retry must fit its attempt limit")


@dataclass(frozen=True, slots=True)
class RuntimeDownloadItemCompleted:
    """Complete one Runtime file after its final postcondition."""

    index: int
    total: int
    target: str
    mode: RuntimeDownloadMode
    attempts: int
    max_attempts: int

    def __post_init__(self) -> None:
        _require_runtime_download_outcome(
            self.index,
            self.total,
            self.target,
            self.mode,
            self.attempts,
            self.max_attempts,
        )


@dataclass(frozen=True, slots=True)
class RuntimePresentationSaturated:
    """Aggregate informational transitions omitted because delivery was full."""

    omitted_transition_count: int

    def __post_init__(self) -> None:
        _require_positive_integer(
            self.omitted_transition_count,
            "omitted Runtime transition count",
        )


@dataclass(frozen=True, slots=True)
class RuntimeWarningsAggregated:
    """Retain one finite warning category count after diagnostic saturation."""

    category: RuntimeWarningCategory
    count: int

    def __post_init__(self) -> None:
        _require_enum(self.category, RuntimeWarningCategory, "Runtime warning category")
        _require_positive_integer(self.count, "aggregated Runtime warning count")


@dataclass(frozen=True, slots=True)
class RuntimeDownloadQueueWarning:
    """Report one controlled asynchronous queue warning outcome."""

    kind: RuntimeDownloadQueueWarningKind

    def __post_init__(self) -> None:
        _require_enum(
            self.kind,
            RuntimeDownloadQueueWarningKind,
            "Runtime download queue warning",
        )


@dataclass(frozen=True, slots=True)
class RuntimeStaleCleanupPending:
    """Report one stale target whose cleanup remains pending."""

    target: str

    def __post_init__(self) -> None:
        _require_canonical_target(self.target)


@dataclass(frozen=True, slots=True)
class RuntimeDownloadFailed:
    """Report one terminal Runtime download failure under its policy."""

    target: str
    mode: RuntimeDownloadMode
    policy: RuntimeDownloadFailurePolicy
    reason: DownloadRetryReason
    attempts: int
    max_attempts: int

    def __post_init__(self) -> None:
        _require_canonical_target(self.target)
        if self.mode not in _RUNTIME_DOWNLOAD_MODES:
            raise ValueError("Runtime download mode must be one controlled value")
        if self.policy not in _RUNTIME_DOWNLOAD_FAILURE_POLICIES:
            raise ValueError("Runtime download policy must be one controlled value")
        _require_enum(
            self.reason, DownloadRetryReason, "Runtime download failure reason"
        )
        _require_attempts_used(self.attempts, self.max_attempts)


@dataclass(frozen=True, slots=True)
class RuntimeSshWarning:
    """Report one controlled SSH warning."""

    kind: RuntimeSshWarningKind
    returncode: int | None = None

    def __post_init__(self) -> None:
        _require_enum(self.kind, RuntimeSshWarningKind, "Runtime SSH warning")
        if self.returncode is not None and type(self.returncode) is not int:
            raise ValueError("Runtime SSH return code must be an integer")
        if (
            self.returncode is not None
            and self.kind is not RuntimeSshWarningKind.EXITED_UNEXPECTEDLY
        ):
            raise ValueError("Only unexpected SSH exit warnings admit a return code")


@dataclass(frozen=True, slots=True)
class RuntimeHookWarning:
    """Report one controlled warning for one safe Runtime hook scope."""

    kind: RuntimeHookWarningKind
    phase: RuntimeHookPhase
    source: RuntimeHookSource
    filename: str

    def __post_init__(self) -> None:
        _require_enum(self.kind, RuntimeHookWarningKind, "Runtime hook warning")
        _require_hook_scope(self.phase, self.source, self.filename)


type RuntimeEvent = (
    RuntimePhaseStarted
    | RuntimePhaseCompleted
    | RuntimePhaseFailed
    | RuntimeGenerationAdmitted
    | RuntimeGenerationReady
    | RuntimeGenerationStopping
    | RuntimeGenerationStopped
    | RuntimeHookStarted
    | RuntimeHookCompleted
    | RuntimeSshOutcome
    | RuntimeDownloadReconciled
    | RuntimeDownloadQueueSummary
    | RuntimeDownloadAttemptStarted
    | RuntimeDownloadItemProgress
    | RuntimeDownloadItemRetryScheduled
    | RuntimeDownloadItemCompleted
    | RuntimePresentationSaturated
    | RuntimeWarningsAggregated
    | RuntimeDownloadQueueWarning
    | RuntimeStaleCleanupPending
    | RuntimeDownloadFailed
    | RuntimeSshWarning
    | RuntimeHookWarning
)


def _require_enum(value: object, expected: type[StrEnum], label: str) -> None:
    if not isinstance(value, expected):
        raise ValueError(f"{label} must be one controlled value")


def _require_generation(value: object) -> None:
    if type(value) is not str or _GENERATION_PATTERN.fullmatch(value) is None:
        raise ValueError("Runtime generation must use the controller-owned identity")


def _require_hook_scope(phase: object, source: object, filename: object) -> None:
    if phase not in RUNTIME_HOOK_PHASE_DIRECTORIES_BY_PHASE:
        raise ValueError("Runtime hook phase must be one controlled value")
    if source not in RUNTIME_HOOK_SOURCE_NAMES:
        raise ValueError("Runtime hook source must be one controlled value")
    _require_hook_filename(filename)


def _require_hook_filename(value: object) -> None:
    if type(value) is not str:
        raise ValueError("Runtime hook filename must be a string")
    try:
        validate_hook_relative_path(value)
    except ValueError as error:
        raise ValueError("Runtime hook filename must be one safe hook leaf") from error
    if PurePosixPath(value).name != value:
        raise ValueError("Runtime hook filename must be one safe hook leaf")


def _require_runtime_download_identity(
    index: object,
    total: object,
    target: object,
    mode: object,
    max_attempts: object,
) -> None:
    _require_position(index, total, "Runtime download")
    _require_canonical_target(target)
    if mode not in _RUNTIME_DOWNLOAD_MODES:
        raise ValueError("Runtime download mode must be one controlled value")
    _require_positive_integer(max_attempts, "Runtime download attempt limit")


def _require_runtime_download_item(
    index: object,
    total: object,
    target: object,
    mode: object,
    attempt: object,
    max_attempts: object,
) -> None:
    _require_runtime_download_identity(index, total, target, mode, max_attempts)
    _require_attempt(attempt, max_attempts)


def _require_runtime_download_outcome(
    index: object,
    total: object,
    target: object,
    mode: object,
    attempts: object,
    max_attempts: object,
) -> None:
    _require_runtime_download_identity(index, total, target, mode, max_attempts)
    _require_attempts_used(attempts, max_attempts)


def _require_attempts_used(attempts: object, max_attempts: object) -> None:
    _require_non_negative_integer(attempts, "Runtime download attempt count")
    _require_positive_integer(max_attempts, "Runtime download attempt limit")
    if attempts > max_attempts:
        raise ValueError("Runtime download attempts must not exceed their limit")


def _require_attempt(attempt: object, max_attempts: object) -> None:
    _require_positive_integer(attempt, "Runtime download attempt")
    _require_positive_integer(max_attempts, "Runtime download attempt limit")
    if attempt > max_attempts:
        raise ValueError("Runtime download attempt must not exceed its limit")


def _require_canonical_target(target: object) -> None:
    if type(target) is not str or not target:
        raise ValueError("Runtime download target must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in target):
        raise ValueError("Runtime download target must not contain control characters")
    if "\\" in target:
        raise ValueError("Runtime download target must use canonical POSIX separators")
    path = PurePosixPath(target)
    if path.is_absolute() or path.as_posix() != target:
        raise ValueError("Runtime download target must be canonical and relative")
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Runtime download target must be a strict relative path")


def _require_position(index: object, total: object, label: str) -> None:
    _require_positive_integer(index, f"{label} index")
    _require_positive_integer(total, f"{label} total")
    if index > total:
        raise ValueError(f"{label} index must not exceed its total")


def _require_positive_integer(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _require_non_negative_integer(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
