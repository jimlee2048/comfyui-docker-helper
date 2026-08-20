"""Always-plain, serialized presentation for durable Runtime facts."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

from comfyui_docker_helper.cli_output.events import EventSink
from comfyui_docker_helper.cli_output.policy import CliOutputSettings, OutputDetail
from comfyui_docker_helper.cli_output.text import control_safe_text
from comfyui_docker_helper.container.download_events import DownloadRetryReason
from comfyui_docker_helper.container.runtime_events import (
    RuntimeDownloadAttemptStarted,
    RuntimeDownloadFailed,
    RuntimeDownloadItemCompleted,
    RuntimeDownloadItemProgress,
    RuntimeDownloadItemRetryScheduled,
    RuntimeDownloadItemVerificationStarted,
    RuntimeDownloadProgressState,
    RuntimeDownloadQueue,
    RuntimeDownloadQueueState,
    RuntimeDownloadQueueSummary,
    RuntimeDownloadQueueWarning,
    RuntimeDownloadQueueWarningKind,
    RuntimeDownloadReconciled,
    RuntimeEvent,
    RuntimeGenerationAdmitted,
    RuntimeGenerationReady,
    RuntimeGenerationStopped,
    RuntimeGenerationStopping,
    RuntimeHookCompleted,
    RuntimeHookStarted,
    RuntimeHookWarning,
    RuntimePhase,
    RuntimePhaseCompleted,
    RuntimePhaseFailed,
    RuntimePhaseStarted,
    RuntimePresentationSaturated,
    RuntimeSshOutcome,
    RuntimeSshStatus,
    RuntimeSshWarning,
    RuntimeSshWarningKind,
    RuntimeStaleCleanupPending,
    RuntimeWarningCategory,
    RuntimeWarningsAggregated,
)

_RUNTIME_PHASE_LABELS = {
    RuntimePhase.RUNTIME_FILES_PREPARATION: "Preparing runtime files",
    RuntimePhase.PRE_START_HOOKS: "Running pre-start hooks",
    RuntimePhase.SSH_STARTUP: "Starting the SSH service",
    RuntimePhase.COMFYUI_STARTUP: "Starting ComfyUI",
    RuntimePhase.COMFYUI_READINESS: "Waiting for ComfyUI readiness",
    RuntimePhase.POST_START_HOOKS: "Running post-start hooks",
    RuntimePhase.STOP_HOOKS: "Running stop hooks",
    RuntimePhase.GENERATION_CLEANUP: "Cleaning up the runtime generation",
}

_SSH_STATUS_LABELS = {
    RuntimeSshStatus.DISABLED: "SSH service is disabled",
    RuntimeSshStatus.ENABLED_WITHOUT_CREDENTIALS: (
        "SSH service is enabled without configured credentials"
    ),
    RuntimeSshStatus.READY: "SSH service is ready",
}

_SSH_WARNING_LABELS = {
    RuntimeSshWarningKind.STARTUP_TERMINATION_FAILED: (
        "SSH startup termination failed"
    ),
    RuntimeSshWarningKind.SERVICE_TERMINATION_FAILED: (
        "SSH service termination failed"
    ),
    RuntimeSshWarningKind.SERVICE_REAP_FAILED: "SSH service reap failed",
    RuntimeSshWarningKind.STARTUP_PROCESS_SIGNAL_FAILED: (
        "SSH startup process signal failed"
    ),
    RuntimeSshWarningKind.MONITOR_FAILED: "SSH service monitoring failed",
    RuntimeSshWarningKind.EXITED_UNEXPECTEDLY: "SSH service exited unexpectedly",
    RuntimeSshWarningKind.SERVICE_SHUTDOWN_FAILED: "SSH service shutdown failed",
    RuntimeSshWarningKind.DIRECTORY_MODE_NONSTANDARD: (
        "Root SSH directory mode is nonstandard; preserving its safe mode"
    ),
    RuntimeSshWarningKind.AUTHORIZED_KEYS_MODE_NONSTANDARD: (
        "Root SSH authorized keys mode is nonstandard; replacing it safely"
    ),
    RuntimeSshWarningKind.FORCE_TERMINATION_REQUIRED: (
        "SSH service required force termination"
    ),
}

_RETRY_REASON_LABELS = {
    DownloadRetryReason.TIMEOUT: "the transfer timed out",
    DownloadRetryReason.NETWORK: "a network error occurred",
    DownloadRetryReason.TEMPORARY_SERVER: "the server reported a temporary error",
    DownloadRetryReason.RATE_LIMITED: "the server rate-limited the transfer",
    DownloadRetryReason.RESUME_REJECTED: "the server rejected the resumed transfer",
    DownloadRetryReason.CHECKSUM_MISMATCH: "verification did not match",
    DownloadRetryReason.UNKNOWN: "the transfer failed",
}

_WARNING_CATEGORY_LABELS = {
    RuntimeWarningCategory.STALE_CLEANUP: "stale-file cleanup",
    RuntimeWarningCategory.DOWNLOAD_FAILURE: "download",
    RuntimeWarningCategory.SSH: "SSH",
}


class RuntimeDisplay(EventSink[RuntimeEvent]):
    """Render Runtime facts as lock-serialized, flushed stderr lines."""

    def __init__(
        self,
        *,
        stderr: TextIO,
        settings: CliOutputSettings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stderr = stderr
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._active_phase: RuntimePhase | None = None
        self._phase_started_at: float | None = None

    def emit(self, event: RuntimeEvent, /) -> None:
        """Render one fact while validating only local phase pairing."""
        with self._lock:
            if isinstance(event, RuntimePhaseStarted):
                self._start_phase(event)
            elif isinstance(event, RuntimePhaseCompleted):
                self._complete_phase(event)
            elif isinstance(event, RuntimePhaseFailed):
                self._fail_phase(event)
            elif isinstance(event, RuntimeGenerationAdmitted):
                value = "Starting a runtime generation"
                value += self._generation_context(event.generation)
                if self._settings.includes(OutputDetail.DEBUG):
                    value += f" operation={event.operation.value}"
                self._write(value)
            elif isinstance(event, RuntimeGenerationReady):
                self._write(
                    "Runtime generation ready"
                    + self._generation_context(event.generation)
                )
            elif isinstance(event, RuntimeGenerationStopping):
                value = "Stopping the runtime generation"
                value += self._generation_context(event.generation)
                self._write(value)
            elif isinstance(event, RuntimeGenerationStopped):
                value = "Runtime generation stopped"
                value += self._generation_context(event.generation)
                if self._settings.includes(OutputDetail.DEBUG):
                    value += f" cause={event.cause.value}"
                self._write(value)
            elif isinstance(event, RuntimeHookStarted):
                self._write(
                    f"[{event.index}/{event.total}] Running {event.phase} hook "
                    f"from {event.source}: {event.filename}"
                )
            elif isinstance(event, RuntimeHookCompleted):
                self._write(
                    f"[{event.index}/{event.total}] Runtime hook complete from "
                    f"{event.source}: {event.filename}",
                    minimum=OutputDetail.VERBOSE,
                )
            elif isinstance(event, RuntimeSshOutcome):
                if event.status is RuntimeSshStatus.ENABLED_WITHOUT_CREDENTIALS:
                    self._write(
                        f"Warning: {_SSH_STATUS_LABELS[event.status]}",
                        warning=True,
                    )
                else:
                    minimum = (
                        OutputDetail.VERBOSE
                        if event.status is RuntimeSshStatus.DISABLED
                        else OutputDetail.NORMAL
                    )
                    self._write(_SSH_STATUS_LABELS[event.status], minimum=minimum)
            elif isinstance(event, RuntimeDownloadReconciled):
                self._download_reconciled(event)
            elif isinstance(event, RuntimeDownloadQueueSummary):
                self._download_queue(event)
            elif isinstance(event, RuntimeDownloadAttemptStarted):
                self._download_attempt_started(event)
            elif isinstance(event, RuntimeDownloadItemProgress):
                self._download_progress(event)
            elif isinstance(event, RuntimeDownloadItemVerificationStarted):
                self._download_verification_started(event)
            elif isinstance(event, RuntimeDownloadItemRetryScheduled):
                self._download_retry(event)
            elif isinstance(event, RuntimeDownloadItemCompleted):
                self._download_completed(event)
            elif isinstance(
                event,
                (
                    RuntimePresentationSaturated,
                    RuntimeWarningsAggregated,
                    RuntimeDownloadQueueWarning,
                    RuntimeStaleCleanupPending,
                    RuntimeDownloadFailed,
                    RuntimeSshWarning,
                    RuntimeHookWarning,
                ),
            ):
                self._warning(event)
            else:
                raise TypeError("unsupported Runtime event")

    def _start_phase(self, event: RuntimePhaseStarted) -> None:
        if self._active_phase is not None:
            raise ValueError("A Runtime phase cannot start while another is active.")
        self._active_phase = event.phase
        self._phase_started_at = self._clock()
        self._write(_RUNTIME_PHASE_LABELS[event.phase])

    def _complete_phase(self, event: RuntimePhaseCompleted) -> None:
        if self._active_phase is not event.phase:
            raise ValueError("Runtime phase completion does not match its start.")
        started_at = self._phase_started_at
        if started_at is None:
            raise RuntimeError("An active Runtime phase must retain its start time.")
        duration = max(0.0, self._clock() - started_at)
        self._active_phase = None
        self._phase_started_at = None
        self._write(
            f"  Phase complete: {_RUNTIME_PHASE_LABELS[event.phase]} "
            f"in {_format_duration(duration)}",
            minimum=OutputDetail.VERBOSE,
        )

    def _fail_phase(self, event: RuntimePhaseFailed) -> None:
        if self._active_phase is not event.phase:
            raise ValueError("Runtime phase failure does not match its start.")
        self._active_phase = None
        self._phase_started_at = None
        self._write(
            f"Warning: Phase did not complete: {_RUNTIME_PHASE_LABELS[event.phase]}",
            warning=True,
        )

    def _download_reconciled(self, event: RuntimeDownloadReconciled) -> None:
        value = "Runtime files reconciled"
        if self._settings.includes(OutputDetail.VERBOSE):
            value += (
                f": {event.desired_count} desired, "
                f"{event.scheduled_sync_count} synchronous scheduled, "
                f"{event.scheduled_async_count} asynchronous scheduled, "
                f"{event.already_present_count} already present, "
                f"{event.stale_count} stale, "
                f"{event.cleanup_pending_count} cleanup pending"
            )
        self._write(value)

    def _download_queue(self, event: RuntimeDownloadQueueSummary) -> None:
        queue = (
            "Synchronous"
            if event.queue is RuntimeDownloadQueue.SYNCHRONOUS
            else "Asynchronous"
        )
        state = {
            RuntimeDownloadQueueState.ACCEPTED: "accepted",
            RuntimeDownloadQueueState.COMPLETED: "complete",
        }[event.state]
        value = f"{queue} runtime download queue {state}"
        if self._settings.includes(OutputDetail.VERBOSE):
            noun = "item" if event.item_count == 1 else "items"
            value += f": {event.item_count} {noun}"
        if self._settings.includes(OutputDetail.DEBUG):
            value += f" queue={event.queue.value} state={event.state.value}"
        self._write(value)

    def _download_attempt_started(self, event: RuntimeDownloadAttemptStarted) -> None:
        value = (
            f"[{event.index}/{event.total}] [{event.attempt}/{event.max_attempts}] "
            f"Downloading runtime file: {event.target}"
        )
        if self._settings.includes(OutputDetail.VERBOSE):
            value += f" mode={event.mode}"
        if self._settings.includes(OutputDetail.DEBUG):
            value += f" backend={event.backend.value}"
        self._write(value)

    def _download_progress(self, event: RuntimeDownloadItemProgress) -> None:
        progress = event.progress
        transferred = _format_bytes(progress.transferred_bytes)
        if progress.total_bytes is None:
            status = f"{transferred} transferred"
        else:
            percentage = _transfer_percentage(
                progress.transferred_bytes,
                progress.total_bytes,
            )
            status = (
                f"{transferred} / {_format_bytes(progress.total_bytes)} "
                f"({percentage:.0f}%)"
            )
        if progress.reported_rate is not None and progress.reported_rate > 0:
            status += f" at {_format_bytes(progress.reported_rate)}/s"
            if progress.total_bytes is not None:
                remaining = max(0, progress.total_bytes - progress.transferred_bytes)
                status += (
                    f", ETA {_format_duration(remaining / progress.reported_rate)}"
                )
        state = {
            RuntimeDownloadProgressState.ACTIVE: "Runtime file transfer",
            RuntimeDownloadProgressState.STALLED: "Runtime file transfer stalled",
            RuntimeDownloadProgressState.RECOVERED: "Runtime file transfer resumed",
        }[event.state]
        value = f"[{event.index}/{event.total}] {state}: {event.target}: {status}"
        if (
            self._settings.includes(OutputDetail.VERBOSE)
            and progress.stored_bytes is not None
        ):
            value += f", {_format_bytes(progress.stored_bytes)} stored"
        if self._settings.includes(OutputDetail.DEBUG):
            value += f" mode={event.mode} attempt={event.attempt}/{event.max_attempts}"
        self._write(value)

    def _download_verification_started(
        self,
        event: RuntimeDownloadItemVerificationStarted,
    ) -> None:
        self._write(
            f"[{event.index}/{event.total}] Verifying runtime file: {event.target}"
        )

    def _download_retry(self, event: RuntimeDownloadItemRetryScheduled) -> None:
        retry = event.retry
        delay = (
            "immediately"
            if retry.delay_seconds == 0
            else f"in {_format_duration(retry.delay_seconds)}"
        )
        value = (
            f"[{event.index}/{event.total}] Retrying runtime file: {event.target} "
            f"[{retry.next_attempt}/{event.max_attempts}] {delay}"
        )
        if self._settings.includes(OutputDetail.VERBOSE):
            value += f" because {_RETRY_REASON_LABELS[retry.reason]}"
        if self._settings.includes(OutputDetail.DEBUG):
            value += f" mode={event.mode} reason={retry.reason.value}"
        self._write(value)

    def _download_completed(self, event: RuntimeDownloadItemCompleted) -> None:
        value = f"[{event.index}/{event.total}] Runtime file ready: {event.target}"
        if self._settings.includes(OutputDetail.VERBOSE):
            if event.attempts == 0:
                value += " without a transfer attempt"
            else:
                value += f" after {event.attempts}/{event.max_attempts} attempt(s)"
        if self._settings.includes(OutputDetail.DEBUG):
            value += f" mode={event.mode}"
        self._write(value)

    def _warning(
        self,
        event: RuntimePresentationSaturated
        | RuntimeWarningsAggregated
        | RuntimeDownloadQueueWarning
        | RuntimeStaleCleanupPending
        | RuntimeDownloadFailed
        | RuntimeSshWarning
        | RuntimeHookWarning,
    ) -> None:
        if isinstance(event, RuntimePresentationSaturated):
            count = event.omitted_transition_count
            noun = "update" if count == 1 else "updates"
            verb = "was" if count == 1 else "were"
            value = (
                "Warning: Runtime output was busy; "
                f"{count} informational {noun} {verb} omitted"
            )
            code = "presentation-saturated"
        elif isinstance(event, RuntimeWarningsAggregated):
            count = event.count
            noun = "warning" if count == 1 else "warnings"
            value = (
                f"Warning: {count} additional runtime "
                f"{_WARNING_CATEGORY_LABELS[event.category]} {noun} occurred "
                "while output was busy"
            )
            code = f"warnings-aggregated-{event.category.value}"
        elif isinstance(event, RuntimeDownloadQueueWarning):
            if event.kind is RuntimeDownloadQueueWarningKind.STOPPED_AFTER_FAILURE:
                value = (
                    "Warning: Asynchronous runtime download queue stopped "
                    "after a failure"
                )
            else:
                value = (
                    "Warning: Asynchronous runtime download queue required "
                    "force termination"
                )
            code = f"download-queue-{event.kind.value}"
        elif isinstance(event, RuntimeStaleCleanupPending):
            value = (
                f"Warning: Stale runtime file cleanup remains pending: {event.target}"
            )
            code = "stale-cleanup-pending"
        elif isinstance(event, RuntimeDownloadFailed):
            mode = "Asynchronous" if event.mode == "async" else "Synchronous"
            value = f"Warning: {mode} runtime file failed: {event.target} "
            if event.attempts == 0:
                value += "before any transfer attempt"
            else:
                value += f"after {event.attempts}/{event.max_attempts} attempt(s)"
            if self._settings.includes(OutputDetail.VERBOSE):
                value += (
                    f"; policy={event.policy}; "
                    f"reason={_RETRY_REASON_LABELS[event.reason]}"
                )
            code = "runtime-download-failed"
        elif isinstance(event, RuntimeSshWarning):
            value = f"Warning: {_SSH_WARNING_LABELS[event.kind]}"
            if event.returncode is not None:
                value += f" with exit code {event.returncode}"
            code = f"ssh-{event.kind.value}"
        else:
            value = (
                f"Warning: Runtime hook termination failed: "
                f"{event.source}/{event.phase}/{event.filename}"
            )
            code = f"hook-{event.kind.value}"
        if self._settings.includes(OutputDetail.DEBUG):
            value += f" code={code}"
        self._write(value, warning=True)

    def _generation_context(self, generation: str) -> str:
        if not self._settings.includes(OutputDetail.VERBOSE):
            return ""
        return f" ({generation})"

    def _write(
        self,
        value: str,
        *,
        minimum: OutputDetail = OutputDetail.NORMAL,
        warning: bool = False,
    ) -> None:
        if not warning and not self._settings.includes(minimum):
            return
        self._stderr.write(f"{control_safe_text(value)}\n")
        self._stderr.flush()


def default_runtime_display(
    settings: CliOutputSettings,
    *,
    stderr: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RuntimeDisplay:
    """Create one always-plain Runtime display for call-time stderr."""
    stderr_stream = sys.stderr if stderr is None else stderr
    return RuntimeDisplay(stderr=stderr_stream, settings=settings, clock=clock)


def _format_bytes(value: int | float) -> str:
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units[:-1]:
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {units[-1]}"


def _transfer_percentage(transferred_bytes: int, total_bytes: int) -> float:
    if total_bytes == 0:
        return 0.0
    if transferred_bytes == total_bytes:
        return 100.0
    return min(transferred_bytes / total_bytes * 100.0, 99.0)


def _format_duration(seconds: float) -> str:
    return f"{seconds:.0f}s"
