"""Plain Container event presentation and interactive download progress."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from types import TracebackType
from typing import Literal, Protocol, TextIO

from rich.console import Console, Group
from rich.progress import BarColumn, Progress, ProgressColumn, Task
from rich.table import Table
from rich.text import Text

from comfyui_docker_helper.cli_output.events import EventSink
from comfyui_docker_helper.cli_output.policy import (
    CliOutputSettings,
    OutputContextKind,
    OutputDetail,
    OutputPolicy,
    OutputStream,
    detect_stream_capabilities,
)
from comfyui_docker_helper.cli_output.text import control_safe_text
from comfyui_docker_helper.container.download_events import (
    DownloadAttemptStarted,
    DownloadBackendName,
    DownloadBatchCompleted,
    DownloadEvent,
    DownloadFinalVerificationCompleted,
    DownloadFinalVerificationStarted,
    DownloadItemCompleted,
    DownloadItemStarted,
    DownloadItemStatus,
    DownloadPlacementCompleted,
    DownloadPlacementStarted,
    DownloadRetryReason,
    DownloadRetryScheduled,
    DownloadTransferProgress,
    DownloadVerificationCompleted,
    DownloadVerificationStarted,
)
from comfyui_docker_helper.container.helper_events import (
    ComfyUIInstallCompleted,
    ContainerHelperEvent,
    ContainerHelperPhase,
    ContainerHelperPhaseCompleted,
    ContainerHelperPhaseStarted,
    CustomNodeCompleted,
    CustomNodesInstallCompleted,
    FinalManifestCompleted,
    GitCustomNodeStarted,
    RegistryCustomNodeStarted,
)

_ACTIVE_PROGRESS_INTERVAL_SECONDS = 10.0
_STALLED_PROGRESS_INTERVAL_SECONDS = 30.0

_RETRY_REASON_LABELS = {
    DownloadRetryReason.TIMEOUT: "the transfer timed out",
    DownloadRetryReason.NETWORK: "the network connection failed",
    DownloadRetryReason.TEMPORARY_SERVER: "the server is temporarily unavailable",
    DownloadRetryReason.RATE_LIMITED: "the server asked cdh to slow down",
    DownloadRetryReason.RESUME_REJECTED: "the server rejected the resumed transfer",
    DownloadRetryReason.CHECKSUM_MISMATCH: "download verification failed",
    DownloadRetryReason.UNKNOWN: "a temporary transfer failure occurred",
}

_HELPER_PHASE_LABELS = {
    ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT: "Checking out ComfyUI source",
    ContainerHelperPhase.COMFYUI_SOURCE_VERIFICATION: "Verifying ComfyUI source",
    ContainerHelperPhase.PYTORCH_INSTALLATION: "Installing PyTorch packages",
    ContainerHelperPhase.PYTHON_EXTRAS_INSTALLATION: "Installing Python extras",
    ContainerHelperPhase.COMFYUI_REQUIREMENTS_INSTALLATION: (
        "Installing ComfyUI requirements"
    ),
    ContainerHelperPhase.MANAGER_INSTALLATION: "Installing ComfyUI-Manager",
    ContainerHelperPhase.COMFYUI_FINAL_VERIFICATION: (
        "Verifying the ComfyUI installation"
    ),
    ContainerHelperPhase.CUSTOM_NODES_PREPARATION: "Preparing custom-node installation",
    ContainerHelperPhase.CUSTOM_NODE_PRE_INSTALL: "Running pre-install hooks",
    ContainerHelperPhase.CUSTOM_NODE_INSTALLATION: "Installing the custom node",
    ContainerHelperPhase.CUSTOM_NODE_POST_INSTALL: "Running post-install hooks",
    ContainerHelperPhase.CUSTOM_NODES_FINAL_VERIFICATION: (
        "Verifying custom-node installation"
    ),
    ContainerHelperPhase.FINAL_STATE_VERIFICATION: "Verifying final image state",
    ContainerHelperPhase.FINAL_MANIFEST_WRITE: "Writing the final manifest",
}


class _DetailFilter(Protocol):
    def includes(self, minimum: OutputDetail) -> bool: ...


class _PlainEventWriter:
    """Write one control-safe, flushed plain event line."""

    def __init__(
        self,
        *,
        stderr: TextIO,
        detail: _DetailFilter,
    ) -> None:
        self._stderr = stderr
        self._detail = detail

    def write(
        self,
        value: str,
        *,
        minimum: OutputDetail = OutputDetail.NORMAL,
    ) -> None:
        if not self._detail.includes(minimum):
            return
        self._stderr.write(f"{control_safe_text(value)}\n")
        self._stderr.flush()


class _CadenceDecision(Enum):
    SUPPRESS = auto()
    ACTIVE = auto()
    STALLED = auto()
    RECOVERED = auto()


class _PlainDownloadCadence:
    """Select active, stalled, and recovered snapshots using a fakeable clock."""

    def __init__(self, *, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._transferred_bytes = 0
        self._last_change_at = clock()
        self._last_active_at = self._last_change_at
        self._last_stalled_at: float | None = None
        self._stalled = False

    def observe(self, transferred_bytes: int) -> _CadenceDecision:
        """Observe current transfer bytes and select any immediately due line."""
        now = self._clock()
        if transferred_bytes != self._transferred_bytes:
            self._transferred_bytes = transferred_bytes
            self._last_change_at = now
            if self._stalled:
                self._stalled = False
                self._last_stalled_at = None
                self._last_active_at = now
                return _CadenceDecision.RECOVERED
            if now - self._last_active_at >= _ACTIVE_PROGRESS_INTERVAL_SECONDS:
                self._last_active_at = now
                return _CadenceDecision.ACTIVE
            return _CadenceDecision.SUPPRESS
        return self.poll()

    def poll(self) -> _CadenceDecision:
        """Select a due stalled heartbeat without requiring a new byte event."""
        now = self._clock()
        stalled_at = self._last_stalled_at
        if not self._stalled:
            if now - self._last_change_at < _STALLED_PROGRESS_INTERVAL_SECONDS:
                return _CadenceDecision.SUPPRESS
            self._stalled = True
            self._last_stalled_at = now
            return _CadenceDecision.STALLED
        if (
            stalled_at is not None
            and now - stalled_at >= _STALLED_PROGRESS_INTERVAL_SECONDS
        ):
            self._last_stalled_at = now
            return _CadenceDecision.STALLED
        return _CadenceDecision.SUPPRESS

    def next_poll_delay(self) -> float:
        """Return the delay until the next stall decision can become due."""
        now = self._clock()
        if self._stalled and self._last_stalled_at is not None:
            deadline = self._last_stalled_at + _STALLED_PROGRESS_INTERVAL_SECONDS
        else:
            deadline = self._last_change_at + _STALLED_PROGRESS_INTERVAL_SECONDS
        return max(0.0, deadline - now)


class _DownloadWatchdog(Protocol):
    def wake(self) -> None: ...

    def close(self) -> None: ...


class _DownloadWatchdogFactory(Protocol):
    def __call__(
        self,
        callback: Callable[[], float | None],
    ) -> _DownloadWatchdog: ...


class _ConditionDownloadWatchdog:
    """One deadline-aware daemon worker for a plain download invocation."""

    def __init__(self, callback: Callable[[], float | None]) -> None:
        self._callback = callback
        self._condition = threading.Condition()
        self._closed = False
        self._revision = 0
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="cdh-download-watchdog",
        )
        self._thread.start()

    def wake(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._revision += 1
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify()
        self._thread.join()

    def _run(self) -> None:
        observed_revision = -1
        delay: float | None = 0.0
        while True:
            with self._condition:
                if self._closed:
                    return
                if observed_revision == self._revision:
                    self._condition.wait(timeout=delay)
                    if self._closed:
                        return
                observed_revision = self._revision
            delay = self._callback()


@dataclass(slots=True)
class _CurrentDownload:
    index: int
    total: int
    target: str
    backend: DownloadBackendName
    max_attempts: int
    checksum_expected: bool
    attempt: int | None = None
    progress: DownloadTransferProgress | None = None
    cadence: _PlainDownloadCadence | None = None


class ContainerDownloadDisplay(EventSink[DownloadEvent]):
    """Render one serial download batch as flushed, append-only stderr lines."""

    def __init__(
        self,
        *,
        stderr: TextIO,
        policy: OutputPolicy,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy
        self._writer = _PlainEventWriter(stderr=stderr, detail=policy)
        self._clock = clock
        self._current: _CurrentDownload | None = None
        self._final_verification: tuple[int, int] | None = None
        self._final_verification_completed: tuple[int, int] | None = None
        self._batch_completed = False

    def emit(self, event: DownloadEvent, /) -> None:
        """Consume one typed download event in serial orchestration order."""
        if self._batch_completed:
            raise RuntimeError("Cannot emit a download event after batch completion.")
        if isinstance(event, DownloadItemStarted):
            self._start_item(event)
        elif isinstance(event, DownloadAttemptStarted):
            self._start_attempt(event)
        elif isinstance(event, DownloadTransferProgress):
            self._progress(event)
        elif isinstance(event, DownloadRetryScheduled):
            self._retry(event)
        elif isinstance(event, DownloadVerificationStarted):
            self._transition("Verifying downloaded bytes")
        elif isinstance(event, DownloadVerificationCompleted):
            self._transition(
                "Downloaded-byte verification complete",
                minimum=OutputDetail.VERBOSE,
            )
        elif isinstance(event, DownloadPlacementStarted):
            self._transition("Placing required file")
        elif isinstance(event, DownloadPlacementCompleted):
            self._transition(
                "Required file placed",
                minimum=OutputDetail.VERBOSE,
            )
        elif isinstance(event, DownloadFinalVerificationStarted):
            self._start_final_verification(event)
        elif isinstance(event, DownloadFinalVerificationCompleted):
            self._complete_final_verification()
        elif isinstance(event, DownloadItemCompleted):
            self._complete_item(event)
        else:
            self._complete_batch(event)

    def poll(self) -> None:
        """Emit a due stalled heartbeat through the invocation polling seam."""
        current = self._current
        if (
            current is None
            or current.cadence is None
            or not self._policy.includes(OutputDetail.NORMAL)
        ):
            return
        if current.cadence.poll() is _CadenceDecision.STALLED:
            if current.progress is None:
                self._write(
                    f"{self._progress_scope(current)} Transfer stalled: "
                    "no new transfer bytes"
                )
            else:
                self._write_progress(
                    current,
                    current.progress,
                    prefix="Transfer stalled",
                    include_rate=False,
                )

    def _next_poll_delay(self) -> float | None:
        current = self._current
        if (
            current is None
            or current.cadence is None
            or not self._policy.includes(OutputDetail.NORMAL)
        ):
            return None
        return current.cadence.next_poll_delay()

    def _start_item(self, event: DownloadItemStarted) -> None:
        if self._current is not None:
            raise ValueError(
                "A download item cannot start before the current item ends."
            )
        if (
            self._final_verification is not None
            or self._final_verification_completed is not None
        ):
            raise ValueError("A download item cannot start after the batch recheck.")
        self._current = _CurrentDownload(
            index=event.index,
            total=event.total,
            target=event.target,
            backend=event.backend,
            max_attempts=event.max_attempts,
            checksum_expected=event.checksum_expected,
        )
        suffix = ""
        if self._policy.includes(OutputDetail.DEBUG):
            suffix = f" (backend={event.backend.value})"
        self._write(
            f"[{event.index}/{event.total}] Required file: {event.target}{suffix}"
        )

    def _start_attempt(self, event: DownloadAttemptStarted) -> None:
        current = self._require_current()
        if event.attempt > current.max_attempts:
            raise ValueError("Download attempt exceeds the admitted attempt budget.")
        current.attempt = event.attempt
        current.progress = None
        current.cadence = _PlainDownloadCadence(clock=self._clock)
        self._write(f"  Attempt [{event.attempt}/{current.max_attempts}] started")

    def _progress(self, event: DownloadTransferProgress) -> None:
        current = self._require_current()
        if current.attempt is None or current.cadence is None:
            raise ValueError("Download progress requires an active attempt.")
        current.progress = event
        decision = current.cadence.observe(event.transferred_bytes)
        if decision is _CadenceDecision.ACTIVE:
            self._write_progress(current, event, prefix="Transferring")
        elif decision is _CadenceDecision.STALLED:
            self._write_progress(
                current,
                event,
                prefix="Transfer stalled",
                include_rate=False,
            )
        elif decision is _CadenceDecision.RECOVERED:
            self._write_progress(current, event, prefix="Transfer resumed")

    def _retry(self, event: DownloadRetryScheduled) -> None:
        current = self._require_current()
        if current.attempt != event.failed_attempt:
            raise ValueError("Retry event does not match the current download attempt.")
        if event.next_attempt > current.max_attempts:
            raise ValueError("Retry event exceeds the admitted attempt budget.")
        delay = (
            "immediately"
            if event.delay_seconds == 0
            else f"in {_format_duration(event.delay_seconds)}"
        )
        value = (
            f"  Attempt [{event.failed_attempt}/{current.max_attempts}] failed; "
            f"retrying as [{event.next_attempt}/{current.max_attempts}] {delay}"
        )
        if self._policy.includes(OutputDetail.VERBOSE):
            value += f" because {_RETRY_REASON_LABELS[event.reason]}"
        if self._policy.includes(OutputDetail.DEBUG):
            facts = [f"backend={current.backend.value}"]
            if event.http_status is not None:
                facts.append(f"http_status={event.http_status}")
            value += f" ({', '.join(facts)})"
        self._write(value)
        current.attempt = None
        current.progress = None
        current.cadence = None

    def _transition(
        self,
        label: str,
        *,
        minimum: OutputDetail = OutputDetail.NORMAL,
    ) -> None:
        current = self._require_current()
        current.cadence = None
        self._write(f"  {label}", minimum=minimum)

    def _complete_item(self, event: DownloadItemCompleted) -> None:
        self._require_current()
        self._write(_item_completion_line(event, self._policy))
        self._current = None

    def _start_final_verification(
        self,
        event: DownloadFinalVerificationStarted,
    ) -> None:
        if self._current is not None:
            raise ValueError("The batch recheck cannot begin during an active item.")
        if (
            self._final_verification is not None
            or self._final_verification_completed is not None
        ):
            raise ValueError("The batch recheck cannot begin more than once.")
        self._final_verification = (event.item_count, event.checksum_count)
        file_noun = "file" if event.item_count == 1 else "files"
        checksum_noun = "checksum" if event.checksum_count == 1 else "checksums"
        value = (
            f"Rechecking {event.item_count} required {file_noun} "
            f"({event.checksum_count} {checksum_noun})"
        )
        if event.item_count and event.checksum_count:
            self._write(value)
        else:
            self._write(value, minimum=OutputDetail.VERBOSE)

    def _complete_final_verification(self) -> None:
        if self._current is not None or self._final_verification is None:
            raise ValueError("Batch recheck completion requires an active recheck.")
        self._write(
            "Required-file batch recheck complete", minimum=OutputDetail.VERBOSE
        )
        self._final_verification_completed = self._final_verification
        self._final_verification = None

    def _complete_batch(self, event: DownloadBatchCompleted) -> None:
        if self._current is not None:
            raise ValueError("A download batch cannot complete during an active item.")
        if (
            self._final_verification is not None
            or self._final_verification_completed is None
        ):
            raise ValueError("A download batch requires a completed final recheck.")
        if self._final_verification_completed != (
            event.item_count,
            event.checksum_verified_count,
        ):
            raise ValueError("Download batch counts do not match its final recheck.")
        self._write(_batch_completion_line(event, self._policy))
        self._batch_completed = True

    def _write_progress(
        self,
        current: _CurrentDownload,
        event: DownloadTransferProgress,
        *,
        prefix: str,
        include_rate: bool = True,
    ) -> None:
        value = (
            f"{self._progress_scope(current)} {prefix}: "
            f"{_format_bytes(event.transferred_bytes)}"
        )
        rate = event.reported_rate
        if event.total_bytes is not None:
            percentage = _percentage(
                event.transferred_bytes,
                event.total_bytes,
            )
            value += (
                f" / {_format_bytes(event.total_bytes)} "
                f"({_format_percentage(percentage)})"
            )
        if rate is not None and include_rate:
            value += f", {_format_bytes_per_second(rate)}"
            if event.total_bytes is not None and rate > 0:
                remaining = max(0, event.total_bytes - event.transferred_bytes)
                value += f", ETA {_format_duration(remaining / rate)}"
        if event.stored_bytes is not None and self._policy.includes(
            OutputDetail.VERBOSE
        ):
            value += f"; stored {_format_bytes(event.stored_bytes)}"
        self._write(value)

    @staticmethod
    def _progress_scope(current: _CurrentDownload) -> str:
        if current.attempt is None:
            raise ValueError("A progress snapshot requires an active attempt.")
        return (
            f"[{current.index}/{current.total}] {current.target} "
            f"attempt [{current.attempt}/{current.max_attempts}]:"
        )

    def _require_current(self) -> _CurrentDownload:
        if self._current is None:
            raise ValueError("Download event requires an active item.")
        return self._current

    def _write(
        self,
        value: str,
        *,
        minimum: OutputDetail = OutputDetail.NORMAL,
    ) -> None:
        self._writer.write(value, minimum=minimum)


class ContainerHelperDisplay(EventSink[ContainerHelperEvent]):
    """Render serial one-shot helper events as durable plain stderr lines."""

    def __init__(
        self,
        *,
        stderr: TextIO,
        settings: CliOutputSettings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._writer = _PlainEventWriter(stderr=stderr, detail=settings)
        self._clock = clock
        self._started_at = clock()
        self._active_phase: ContainerHelperPhase | None = None
        self._phase_started_at: float | None = None

    def emit(self, event: ContainerHelperEvent, /) -> None:
        """Render one helper event and validate only serial phase pairing."""
        if isinstance(event, ContainerHelperPhaseStarted):
            self._start_phase(event)
        elif isinstance(event, ContainerHelperPhaseCompleted):
            self._complete_phase(event)
        elif isinstance(event, RegistryCustomNodeStarted):
            self._registry_node(event)
        elif isinstance(event, GitCustomNodeStarted):
            self._git_node(event)
        elif isinstance(event, CustomNodeCompleted):
            self._writer.write(f"[{event.index}/{event.total}] Custom node complete")
        elif isinstance(event, ComfyUIInstallCompleted):
            self._write_terminal("ComfyUI installation complete")
        elif isinstance(event, CustomNodesInstallCompleted):
            value = "Custom-node installation complete"
            if event.node_count == 0 or self._settings.includes(OutputDetail.VERBOSE):
                noun = "node" if event.node_count == 1 else "nodes"
                value += f": {event.node_count} {noun}"
            self._write_terminal(value)
        elif isinstance(event, FinalManifestCompleted):
            self._write_terminal("Final manifest complete")
        else:
            raise TypeError("unsupported Container helper event")

    def _start_phase(self, event: ContainerHelperPhaseStarted) -> None:
        if self._active_phase is not None:
            raise ValueError("A helper phase cannot start while another is active.")
        self._active_phase = event.phase
        self._phase_started_at = self._clock()
        self._writer.write(_HELPER_PHASE_LABELS[event.phase])

    def _complete_phase(self, event: ContainerHelperPhaseCompleted) -> None:
        if self._active_phase is not event.phase:
            raise ValueError("Helper phase completion does not match its start.")
        phase_started_at = self._phase_started_at
        if phase_started_at is None:
            raise RuntimeError("An active helper phase must retain its start time.")
        duration = max(0.0, self._clock() - phase_started_at)
        self._active_phase = None
        self._phase_started_at = None
        self._writer.write(
            f"  Phase complete: {_HELPER_PHASE_LABELS[event.phase]} "
            f"in {_format_duration(duration)}",
            minimum=OutputDetail.VERBOSE,
        )

    def _registry_node(self, event: RegistryCustomNodeStarted) -> None:
        value = f"[{event.index}/{event.total}] Custom node: {event.id} {event.version}"
        self._writer.write(self._node_detail(value, event, source="registry"))

    def _git_node(self, event: GitCustomNodeStarted) -> None:
        value = f"[{event.index}/{event.total}] Custom node: {event.target_name}"
        self._writer.write(self._node_detail(value, event, source="git"))

    def _node_detail(
        self,
        value: str,
        event: RegistryCustomNodeStarted | GitCustomNodeStarted,
        *,
        source: Literal["registry", "git"],
    ) -> str:
        if self._settings.includes(OutputDetail.VERBOSE):
            value += (
                f" (pre-install hooks={event.pre_hook_count}, "
                f"post-install hooks={event.post_hook_count}"
            )
            if self._settings.includes(OutputDetail.DEBUG):
                value += f", source={source}"
            value += ")"
        return value

    def _write_terminal(self, value: str) -> None:
        if self._settings.includes(OutputDetail.VERBOSE):
            duration = max(0.0, self._clock() - self._started_at)
            value += f" in {_format_duration(duration)}"
        self._writer.write(value)


class _RichTransferColumn(ProgressColumn):
    """Render transfer facts without crossing byte domains."""

    def __init__(self, *, detail: OutputDetail) -> None:
        super().__init__()
        self._detail = detail

    def render(self, task: Task) -> Text:
        if task.fields.get("phase") != "transfer":
            return Text("")
        transferred = int(task.completed)
        total = None if task.total is None else int(task.total)
        value = _format_bytes(transferred)
        if total is not None:
            value += (
                f" / {_format_bytes(total)} "
                f"({_format_percentage(_percentage(transferred, total))})"
            )
        rate = task.fields.get("reported_rate")
        if isinstance(rate, (int, float)):
            value += f", {_format_bytes_per_second(rate)}"
            if total is not None and rate > 0:
                remaining = max(0, total - transferred)
                value += f", ETA {_format_duration(remaining / rate)}"
        stored = task.fields.get("stored_bytes")
        if isinstance(stored, int) and self._detail >= OutputDetail.VERBOSE:
            value += f"; stored {_format_bytes(stored)}"
        return Text(value)


class _RichDownloadColumn(ProgressColumn):
    """Keep the complete safe target above compact transfer facts."""

    def __init__(self, *, detail: OutputDetail, supports_unicode: bool) -> None:
        super().__init__()
        self._bar: ProgressColumn = (
            BarColumn(bar_width=16)
            if supports_unicode
            else _AsciiProgressBarColumn(width=16)
        )
        self._transfer = _RichTransferColumn(detail=detail)

    def render(self, task: Task) -> Group:
        status = Table.grid(padding=(0, 1))
        status.add_column(width=16)
        status.add_column()
        status.add_row(self._bar(task), self._transfer(task))
        return Group(Text(control_safe_text(task.description)), status)


class _AsciiProgressBarColumn(ProgressColumn):
    """Provide a live bar for capable terminals without Unicode encoding."""

    def __init__(self, *, width: int) -> None:
        super().__init__()
        self._width = width

    def render(self, task: Task) -> Text:
        if task.total is None:
            position = int(task.get_time() * 4) % self._width
            cells = ["-"] * self._width
            cells[position] = "#"
            return Text("".join(cells))
        if task.total <= 0:
            completed = 0
        else:
            ratio = min(1.0, max(0.0, task.completed / task.total))
            completed = int(ratio * self._width)
        return Text("#" * completed + "-" * (self._width - completed))


class _RichContainerDownloadDisplay(EventSink[DownloadEvent]):
    """Render one directly interactive batch with one transient Progress."""

    def __init__(
        self,
        *,
        stderr: TextIO,
        policy: OutputPolicy,
    ) -> None:
        capabilities = policy.capabilities(OutputStream.STDERR)
        console = Console(
            file=stderr,
            force_terminal=capabilities.is_terminal,
            color_system="auto" if capabilities.supports_color else None,
            no_color=not capabilities.supports_color,
        )
        self._policy = policy
        self._state = ContainerDownloadDisplay(
            stderr=stderr,
            policy=OutputPolicy(
                settings=CliOutputSettings(detail=OutputDetail.QUIET),
                stdout=policy.stdout,
                stderr=policy.stderr,
                context=policy.context,
            ),
        )
        self._progress = Progress(
            _RichDownloadColumn(
                detail=policy.settings.detail,
                supports_unicode=capabilities.supports_unicode,
            ),
            console=console,
            transient=True,
            auto_refresh=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._task_id: int | None = None
        self._started = False
        self._closed = False

    def emit(self, event: DownloadEvent, /) -> None:
        """Validate and render one typed download event."""
        if self._closed:
            raise RuntimeError("Cannot emit a download event after display close.")
        current_before = self._state._current
        self._state.emit(event)
        if isinstance(event, DownloadItemStarted):
            self._start_item(event)
        elif isinstance(event, DownloadAttemptStarted):
            self._update(
                self._attempt_label(event.attempt),
                total=None,
                completed=0,
                phase="status",
            )
        elif isinstance(event, DownloadTransferProgress):
            self._update(
                self._attempt_label(self._current_attempt()),
                total=event.total_bytes,
                completed=event.transferred_bytes,
                phase="transfer",
                reported_rate=event.reported_rate,
                stored_bytes=event.stored_bytes,
            )
        elif isinstance(event, DownloadRetryScheduled):
            self._print(self._retry_line(event, current_before))
            self._update(
                self._item_label("Waiting to retry"),
                total=None,
                completed=0,
                phase="status",
            )
        elif isinstance(event, DownloadVerificationStarted):
            self._update(
                self._item_label("Verifying downloaded bytes"),
                total=None,
                completed=0,
                phase="status",
            )
        elif isinstance(event, DownloadVerificationCompleted):
            if self._policy.includes(OutputDetail.VERBOSE):
                self._update(
                    self._item_label("Downloaded-byte verification complete"),
                    total=None,
                    completed=0,
                    phase="status",
                )
        elif isinstance(event, DownloadPlacementStarted):
            self._update(
                self._item_label("Placing required file"),
                total=None,
                completed=0,
                phase="status",
            )
        elif isinstance(event, DownloadPlacementCompleted):
            if self._policy.includes(OutputDetail.VERBOSE):
                self._update(
                    self._item_label("Required file placed"),
                    total=None,
                    completed=0,
                    phase="status",
                )
        elif isinstance(event, DownloadItemCompleted):
            self._remove_task()
            if current_before is None:
                raise ValueError("Interactive completion requires an active item.")
            self._print(
                f"[{current_before.index}/{current_before.total}] "
                f"{current_before.target}: "
                f"{_item_completion_line(event, self._policy).strip()}"
            )
        elif isinstance(event, DownloadFinalVerificationStarted):
            self._start_final_verification(event)
        elif isinstance(event, DownloadFinalVerificationCompleted):
            if self._task_id is not None and self._policy.includes(
                OutputDetail.VERBOSE
            ):
                self._update(
                    "Required-file batch recheck complete",
                    total=None,
                    completed=0,
                    phase="status",
                )
            self._remove_task()
        else:
            self._remove_task()
            self._print(_batch_completion_line(event, self._policy))

    def close(self) -> None:
        """Stop and clear the transient Progress once."""
        if self._closed:
            return
        self._closed = True
        if self._started:
            self._started = False
            self._progress.stop()

    def _start_item(self, event: DownloadItemStarted) -> None:
        self._ensure_started()
        suffix = ""
        if self._policy.includes(OutputDetail.DEBUG):
            suffix = f" (backend={event.backend.value})"
        self._task_id = self._progress.add_task(
            f"[{event.index}/{event.total}] {event.target}{suffix}",
            total=None,
            phase="status",
        )

    def _start_final_verification(
        self,
        event: DownloadFinalVerificationStarted,
    ) -> None:
        should_show = bool(event.item_count and event.checksum_count)
        if not should_show and not self._policy.includes(OutputDetail.VERBOSE):
            return
        self._ensure_started()
        file_noun = "file" if event.item_count == 1 else "files"
        checksum_noun = "checksum" if event.checksum_count == 1 else "checksums"
        self._task_id = self._progress.add_task(
            f"Rechecking {event.item_count} required {file_noun} "
            f"({event.checksum_count} {checksum_noun})",
            total=None,
            phase="status",
        )

    def _update(
        self,
        description: str,
        *,
        total: int | None,
        completed: int,
        phase: str,
        reported_rate: int | float | None = None,
        stored_bytes: int | None = None,
    ) -> None:
        if self._task_id is None:
            raise ValueError("Interactive download state requires an active task.")
        self._progress.update(
            self._task_id,
            description=control_safe_text(description),
            total=total,
            completed=completed,
            phase=phase,
            reported_rate=reported_rate,
            stored_bytes=stored_bytes,
            refresh=True,
        )

    def _remove_task(self) -> None:
        if self._task_id is None:
            return
        self._progress.remove_task(self._task_id)
        self._task_id = None

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._progress.start()
        except BaseException:
            with suppress(BaseException):
                self._progress.stop()
            self._started = False
            raise

    def _print(self, value: str) -> None:
        self._progress.console.print(Text(control_safe_text(value)))

    def _current_attempt(self) -> int:
        current = self._state._current
        if current is None or current.attempt is None:
            raise ValueError("Interactive progress requires an active attempt.")
        return current.attempt

    def _item_label(self, state: str) -> str:
        current = self._state._current
        if current is None:
            raise ValueError("Interactive transition requires an active item.")
        return f"[{current.index}/{current.total}] {current.target} - {state}"

    def _attempt_label(self, attempt: int) -> str:
        current = self._state._current
        if current is None:
            raise ValueError("Interactive attempt requires an active item.")
        return (
            f"[{current.index}/{current.total}] {current.target} - "
            f"attempt [{attempt}/{current.max_attempts}]"
        )

    def _retry_line(
        self,
        event: DownloadRetryScheduled,
        current: _CurrentDownload | None,
    ) -> str:
        if current is None:
            raise ValueError("Interactive retry requires an active item.")
        delay = (
            "immediately"
            if event.delay_seconds == 0
            else f"in {_format_duration(event.delay_seconds)}"
        )
        value = (
            f"[{current.index}/{current.total}] {current.target}: attempt "
            f"[{event.failed_attempt}/{current.max_attempts}] failed; retrying as "
            f"[{event.next_attempt}/{current.max_attempts}] {delay}"
        )
        if self._policy.includes(OutputDetail.VERBOSE):
            value += f" because {_RETRY_REASON_LABELS[event.reason]}"
        if self._policy.includes(OutputDetail.DEBUG):
            facts = [f"backend={current.backend.value}"]
            if event.http_status is not None:
                facts.append(f"http_status={event.http_status}")
            value += f" ({', '.join(facts)})"
        return value


class ContainerDownloadInvocation(EventSink[DownloadEvent]):
    """Own one download display and its complete invocation lifecycle."""

    def __init__(
        self,
        *,
        stderr: TextIO,
        policy: OutputPolicy,
        clock: Callable[[], float] = time.monotonic,
        watchdog_factory: _DownloadWatchdogFactory = _ConditionDownloadWatchdog,
    ) -> None:
        if policy.includes(OutputDetail.NORMAL) and policy.allows_live(
            OutputStream.STDERR
        ):
            self._display: ContainerDownloadDisplay | _RichContainerDownloadDisplay
            self._display = _RichContainerDownloadDisplay(
                stderr=stderr,
                policy=policy,
            )
        else:
            self._display = ContainerDownloadDisplay(
                stderr=stderr,
                policy=policy,
                clock=clock,
            )
        self._lock = threading.Lock()
        self._closed = False
        self._background_error: BaseException | None = None
        self._watchdog: _DownloadWatchdog | None = None
        if policy.includes(OutputDetail.NORMAL) and not policy.allows_live(
            OutputStream.STDERR
        ):
            self._watchdog = watchdog_factory(self._watchdog_poll)

    def __enter__(self) -> ContainerDownloadInvocation:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, traceback
        self._shutdown(primary=exc_value)
        return False

    def emit(self, event: DownloadEvent, /) -> None:
        """Serialize an event with watchdog polling and surface latched failure."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Cannot emit a download event after display close.")
            self._raise_background_error()
            self._display.emit(event)
            watchdog = self._watchdog
        if watchdog is not None:
            watchdog.wake()

    def close(self) -> None:
        """Stop the watchdog once and surface an unmasked background failure."""
        self._shutdown(primary=None)

    def _watchdog_poll(self) -> float | None:
        with self._lock:
            if self._closed or self._background_error is not None:
                return None
            try:
                self._display.poll()
                return self._display._next_poll_delay()
            except BaseException as error:
                self._background_error = error
                return None

    def _shutdown(self, *, primary: BaseException | None) -> None:
        with self._lock:
            if self._closed:
                watchdog = None
            else:
                self._closed = True
                watchdog = self._watchdog
                self._watchdog = None

        close_error: BaseException | None = None
        if watchdog is not None:
            try:
                watchdog.close()
            except BaseException as error:
                close_error = error
        if isinstance(self._display, _RichContainerDownloadDisplay):
            try:
                self._display.close()
            except BaseException as error:
                if close_error is None:
                    close_error = error

        with self._lock:
            background_error = self._background_error
            self._background_error = None
        if primary is not None:
            return
        if background_error is not None:
            raise background_error.with_traceback(background_error.__traceback__)
        if close_error is not None:
            raise close_error.with_traceback(close_error.__traceback__)

    def _raise_background_error(self) -> None:
        error = self._background_error
        if error is None:
            return
        self._background_error = None
        raise error.with_traceback(error.__traceback__)


def default_container_download_invocation(
    settings: CliOutputSettings,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> ContainerDownloadInvocation:
    """Create one download invocation from the concrete call-time streams."""
    stdout_stream = sys.stdout if stdout is None else stdout
    stderr_stream = sys.stderr if stderr is None else stderr
    policy = OutputPolicy(
        settings=settings,
        stdout=detect_stream_capabilities(stdout_stream),
        stderr=detect_stream_capabilities(stderr_stream),
        context=OutputContextKind.ONE_SHOT,
    )
    return ContainerDownloadInvocation(stderr=stderr_stream, policy=policy)


def default_container_helper_display(
    settings: CliOutputSettings,
    *,
    stderr: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ContainerHelperDisplay:
    """Create one always-plain helper display for the call-time stderr."""
    stderr_stream = sys.stderr if stderr is None else stderr
    return ContainerHelperDisplay(
        stderr=stderr_stream,
        settings=settings,
        clock=clock,
    )


def _format_bytes(value: int | float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} {unit}"
    return f"{amount:.1f} {unit}"


def _format_bytes_per_second(value: int | float) -> str:
    return f"{_format_bytes(value)}/s"


def _item_completion_line(
    event: DownloadItemCompleted,
    policy: OutputPolicy,
) -> str:
    result = (
        "Downloaded"
        if event.status is DownloadItemStatus.DOWNLOADED
        else "Already present"
    )
    value = f"  {result}: {_format_bytes(event.observed_bytes)}"
    if policy.includes(OutputDetail.VERBOSE):
        checksum = "verified" if event.checksum_verified else "not requested"
        value += f" (checksum {checksum})"
    return value


def _batch_completion_line(
    event: DownloadBatchCompleted,
    policy: OutputPolicy,
) -> str:
    noun = "file" if event.item_count == 1 else "files"
    value = f"Downloads complete: {event.item_count} required {noun}"
    if policy.includes(OutputDetail.VERBOSE):
        checksum_noun = (
            "checksum" if event.checksum_verified_count == 1 else "checksums"
        )
        value += f", {event.checksum_verified_count} {checksum_noun} verified"
    return value


def _format_percentage(value: float) -> str:
    return f"{value:.0f}%"


def _percentage(transferred_bytes: int, total_bytes: int) -> float:
    if total_bytes == 0:
        return 0.0
    if transferred_bytes == total_bytes:
        return 100.0
    return min(transferred_bytes / total_bytes * 100.0, 99.0)


def _format_duration(value: int | float) -> str:
    return f"{value:.0f}s"
