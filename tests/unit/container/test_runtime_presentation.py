"""Typed facts and always-plain presentation for the durable Runtime."""

from __future__ import annotations

import threading
import time
from dataclasses import FrozenInstanceError
from io import StringIO

import pytest

from comfyui_docker_helper.cli_output.policy import CliOutputSettings, OutputDetail
from comfyui_docker_helper.container.download_events import (
    DownloadBackendName,
    DownloadRetryReason,
    DownloadRetryScheduled,
    DownloadTransferProgress,
)
from comfyui_docker_helper.container.runtime_event_delivery import (
    safe_runtime_event_sink,
)
from comfyui_docker_helper.container.runtime_events import (
    RuntimeDownloadAttemptStarted,
    RuntimeDownloadFailed,
    RuntimeDownloadItemCompleted,
    RuntimeDownloadItemProgress,
    RuntimeDownloadItemRetryScheduled,
    RuntimeDownloadQueue,
    RuntimeDownloadQueueState,
    RuntimeDownloadQueueSummary,
    RuntimeDownloadQueueWarning,
    RuntimeDownloadQueueWarningKind,
    RuntimeDownloadReconciled,
    RuntimeGenerationAdmitted,
    RuntimeGenerationOperation,
    RuntimeGenerationReady,
    RuntimeGenerationStopCause,
    RuntimeGenerationStopped,
    RuntimeGenerationStopping,
    RuntimeHookCompleted,
    RuntimeHookStarted,
    RuntimeHookWarning,
    RuntimeHookWarningKind,
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
)
from comfyui_docker_helper.container.runtime_presentation import (
    RuntimeDisplay,
    default_runtime_display,
)


class _TerminalStream(StringIO):
    encoding = "ascii"

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def isatty(self) -> bool:
        return True

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class _YieldingStream:
    encoding = "utf-8"

    def __init__(self) -> None:
        self.characters: list[str] = []
        self.flushes = 0

    def write(self, value: str) -> int:
        for character in value:
            self.characters.append(character)
            time.sleep(0)
        return len(value)

    def flush(self) -> None:
        self.flushes += 1

    def value(self) -> str:
        return "".join(self.characters)


class _Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        value = self.now
        self.now += 2.0
        return value


def _render(detail: OutputDetail) -> str:
    stream = StringIO()
    display = RuntimeDisplay(
        stderr=stream,
        settings=CliOutputSettings(detail=detail),
        clock=_Clock(),
    )
    events = (
        RuntimeGenerationAdmitted(
            generation="gen-1",
            operation=RuntimeGenerationOperation.INITIAL_START,
        ),
        RuntimePhaseStarted(RuntimePhase.RUNTIME_FILES_PREPARATION),
        RuntimeDownloadReconciled(
            desired_count=5,
            scheduled_sync_count=1,
            scheduled_async_count=1,
            already_present_count=3,
            stale_count=2,
            cleanup_pending_count=1,
        ),
        RuntimePhaseCompleted(RuntimePhase.RUNTIME_FILES_PREPARATION),
        RuntimeHookStarted(1, 1, "pre-start", "baked", "10-prepare.sh"),
        RuntimeHookCompleted(1, 1, "pre-start", "baked", "10-prepare.sh"),
        RuntimeSshOutcome(RuntimeSshStatus.ENABLED_WITHOUT_CREDENTIALS),
        RuntimeDownloadQueueSummary(
            queue=RuntimeDownloadQueue.ASYNCHRONOUS,
            state=RuntimeDownloadQueueState.ACCEPTED,
            item_count=1,
        ),
        RuntimeDownloadAttemptStarted(
            1,
            1,
            "models/model.bin",
            "async",
            DownloadBackendName.HTTPX,
            1,
            3,
        ),
        RuntimeDownloadItemProgress(
            1,
            1,
            "models/model.bin",
            "async",
            1,
            3,
            DownloadTransferProgress(512, 1024, 600, 256),
        ),
        RuntimeDownloadItemRetryScheduled(
            1,
            1,
            "models/model.bin",
            "async",
            3,
            DownloadRetryScheduled(1, 2, 0, DownloadRetryReason.NETWORK),
        ),
        RuntimeDownloadItemCompleted(1, 1, "models/model.bin", "async", 2, 3),
        RuntimeGenerationReady("gen-1"),
        RuntimeGenerationStopping("gen-1"),
        RuntimeGenerationStopped("gen-1", RuntimeGenerationStopCause.STARTUP_FAILURE),
        RuntimePresentationSaturated(omitted_transition_count=2),
        RuntimeDownloadQueueWarning(
            RuntimeDownloadQueueWarningKind.STOPPED_AFTER_FAILURE
        ),
    )
    for event in events:
        display.emit(event)
    return stream.getvalue()


def test_runtime_display_owns_the_four_detail_levels_once() -> None:
    quiet = _render(OutputDetail.QUIET)
    normal = _render(OutputDetail.NORMAL)
    verbose = _render(OutputDetail.VERBOSE)
    debug = _render(OutputDetail.DEBUG)

    assert "saturated" in quiet
    assert "queue stopped after a failure" in quiet
    assert "Starting a runtime generation" not in quiet
    assert "Preparing runtime files" in normal
    assert "[1/1]" in normal
    assert "baked" in normal
    assert "models/model.bin" in normal
    assert "backend=" not in normal
    assert "[1/3] Downloading runtime file" in normal
    assert "50%" in normal
    assert "ETA" in normal
    assert "Retrying runtime file" in normal
    assert "immediately" in normal
    assert "Runtime file ready" in normal
    assert "gen-1" not in normal
    assert "queue accepted" in normal
    assert "5 desired" not in normal
    assert "gen-1" in verbose
    assert "5 desired" in verbose
    assert "600 B stored" in verbose
    assert "network error" in verbose
    assert "Phase complete" in verbose
    assert "operation=initial-start" in debug
    assert "mode=async backend=httpx" in debug
    assert "cause=startup-failure" in debug
    assert "code=presentation-saturated" in debug


def test_runtime_display_ignores_terminal_capability_and_preserves_unknown_totals() -> (
    None
):
    stream = _TerminalStream()
    display = default_runtime_display(CliOutputSettings(), stderr=stream)

    display.emit(
        RuntimeDownloadItemProgress(
            1,
            1,
            "models/unknown.bin",
            "sync",
            1,
            1,
            DownloadTransferProgress(512, None, None, 64),
        )
    )

    output = stream.getvalue()
    output.encode("ascii")
    assert "512 B transferred" in output
    assert "%" not in output
    assert "ETA" not in output
    assert "\x1b" not in output
    assert "\r" not in output
    assert stream.flushes == 1


def test_runtime_warnings_keep_safe_context_even_when_quiet() -> None:
    stream = StringIO()
    display = RuntimeDisplay(
        stderr=stream,
        settings=CliOutputSettings(detail=OutputDetail.QUIET),
    )
    sync_failure = RuntimeDownloadFailed(
        "models/required.bin",
        "sync",
        "fail",
        DownloadRetryReason.TIMEOUT,
        1,
        1,
    )

    for warning in (
        RuntimeStaleCleanupPending("models/stale.bin"),
        RuntimeDownloadFailed(
            "models/failed.bin",
            "async",
            "continue",
            DownloadRetryReason.NETWORK,
            2,
            3,
        ),
        sync_failure,
        RuntimeSshWarning(RuntimeSshWarningKind.EXITED_UNEXPECTEDLY, returncode=7),
        RuntimeHookWarning(
            RuntimeHookWarningKind.TERMINATION_FAILED,
            "stop",
            "mounted",
            "90-cleanup.sh",
        ),
    ):
        display.emit(warning)

    output = stream.getvalue()
    assert "models/stale.bin" in output
    assert "models/failed.bin" in output
    assert "Synchronous" in output
    assert "models/required.bin" in output
    assert "exit code 7" in output
    assert "mounted/stop/90-cleanup.sh" in output

    debug_stream = StringIO()
    RuntimeDisplay(
        stderr=debug_stream,
        settings=CliOutputSettings(detail=OutputDetail.DEBUG),
    ).emit(sync_failure)
    assert "code=runtime-download-failed" in debug_stream.getvalue()
    assert "code=async-download-failed" not in debug_stream.getvalue()


def test_runtime_events_reject_uncontrolled_dynamic_payloads() -> None:
    with pytest.raises(ValueError, match="controller-owned identity"):
        RuntimeGenerationReady("https://user:secret@example.invalid\n")
    with pytest.raises(ValueError, match="safe hook leaf"):
        RuntimeHookStarted(
            1,
            1,
            "pre-start",
            "mounted",
            "https://user:secret@example.invalid/hook.sh\n",
        )
    with pytest.raises(ValueError, match="controlled value"):
        RuntimeHookStarted(1, 1, "startup", "mounted", "10-hook.sh")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="relative path"):
        RuntimeStaleCleanupPending("../secret")
    with pytest.raises(ValueError, match="must equal desired"):
        RuntimeDownloadReconciled(2, 1, 1, 1, 0, 0)
    with pytest.raises(ValueError, match="must not exceed stale"):
        RuntimeDownloadReconciled(2, 1, 0, 1, 0, 1)
    with pytest.raises(ValueError, match="controlled value"):
        RuntimeDownloadFailed(
            "models/file.bin",
            "sync",
            "ignore",  # type: ignore[arg-type]
            DownloadRetryReason.UNKNOWN,
            1,
            1,
        )
    with pytest.raises(ValueError, match="integer"):
        RuntimeSshWarning(RuntimeSshWarningKind.MONITOR_FAILED, returncode=True)
    with pytest.raises(ValueError, match="unexpected SSH exit"):
        RuntimeSshWarning(RuntimeSshWarningKind.MONITOR_FAILED, returncode=7)


def test_runtime_phase_pairing_and_event_immutability_are_local() -> None:
    event = RuntimeGenerationReady("gen-3")
    with pytest.raises(FrozenInstanceError):
        event.generation = "gen-4"  # type: ignore[misc]

    stream = StringIO()
    display = RuntimeDisplay(stderr=stream, settings=CliOutputSettings())
    display.emit(event)
    with pytest.raises(ValueError, match="phase"):
        display.emit(RuntimePhaseCompleted(RuntimePhase.COMFYUI_STARTUP))

    display.emit(RuntimePhaseStarted(RuntimePhase.COMFYUI_STARTUP))
    with pytest.raises(ValueError, match="while another is active"):
        display.emit(RuntimePhaseStarted(RuntimePhase.COMFYUI_READINESS))
    with pytest.raises(ValueError, match="does not match"):
        display.emit(RuntimePhaseCompleted(RuntimePhase.COMFYUI_READINESS))
    display.emit(RuntimePhaseFailed(RuntimePhase.COMFYUI_STARTUP))
    display.emit(RuntimePhaseStarted(RuntimePhase.COMFYUI_READINESS))

    quiet_stream = StringIO()
    quiet_display = RuntimeDisplay(
        stderr=quiet_stream,
        settings=CliOutputSettings(detail=OutputDetail.QUIET),
    )
    quiet_display.emit(RuntimePhaseStarted(RuntimePhase.COMFYUI_STARTUP))
    quiet_display.emit(RuntimePhaseFailed(RuntimePhase.COMFYUI_STARTUP))
    assert "Warning:" in quiet_stream.getvalue()
    assert "did not complete" in quiet_stream.getvalue()


def test_runtime_display_serializes_complete_lines_between_threads() -> None:
    stream = _YieldingStream()
    display = RuntimeDisplay(stderr=stream, settings=CliOutputSettings())  # type: ignore[arg-type]
    barrier = threading.Barrier(3)

    def emit(event: RuntimeHookStarted) -> None:
        barrier.wait()
        display.emit(event)

    threads = (
        threading.Thread(
            target=emit,
            args=(RuntimeHookStarted(1, 2, "pre-start", "baked", "10-one.sh"),),
        ),
        threading.Thread(
            target=emit,
            args=(RuntimeHookStarted(2, 2, "pre-start", "mounted", "20-two.py"),),
        ),
    )
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    lines = stream.value().splitlines()
    assert len(lines) == 2
    assert any(
        "[1/2]" in line and "baked" in line and "10-one.sh" in line for line in lines
    )
    assert any(
        "[2/2]" in line and "mounted" in line and "20-two.py" in line for line in lines
    )
    assert all(not ("10-one.sh" in line and "20-two.py" in line) for line in lines)
    assert stream.flushes == 2


def test_safe_runtime_event_sink_latches_exceptions_but_preserves_interrupts() -> None:
    calls: list[object] = []

    class FailingSink:
        def emit(self, event: object) -> None:
            calls.append(event)
            raise OSError("ordinary presentation failure")

    safe_sink = safe_runtime_event_sink(FailingSink())  # type: ignore[arg-type]
    assert safe_sink is not None
    safe_sink.emit(RuntimeGenerationReady("gen-1"))
    safe_sink.emit(RuntimeGenerationReady("gen-2"))
    assert calls == [RuntimeGenerationReady("gen-1")]

    class InterruptingSink:
        def emit(self, _event: object) -> None:
            raise KeyboardInterrupt

    interrupting = safe_runtime_event_sink(InterruptingSink())  # type: ignore[arg-type]
    assert interrupting is not None
    with pytest.raises(KeyboardInterrupt):
        interrupting.emit(RuntimeGenerationReady("gen-3"))
