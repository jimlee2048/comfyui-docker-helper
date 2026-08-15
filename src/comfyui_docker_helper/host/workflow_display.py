"""Live or durable presentation of operator-facing Host workflow phases."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text
from rich.tree import Tree

from comfyui_docker_helper.cli_output.events import EventSink
from comfyui_docker_helper.cli_output.policy import (
    OutputDetail,
    OutputPolicy,
    OutputStream,
)
from comfyui_docker_helper.cli_output.text import control_safe_text
from comfyui_docker_helper.host.events import (
    HostPhase,
    HostPhaseCompleted,
    HostPhaseFailed,
    HostPhaseInterrupted,
    HostPhaseStarted,
    HostSubphase,
    HostSubphaseCompleted,
    HostSubphaseStarted,
    HostWorkflowEvent,
    HostWorkflowSucceeded,
    HostWorkflowTerminalEvent,
)

_PHASE_LABELS = {
    HostPhase.CONFIGURATION_VALIDATION: "Validating configuration",
    HostPhase.BUILD_INPUT_RESOLUTION: "Resolving build inputs",
    HostPhase.LOCK_RECONCILIATION: "Reconciling canonical lock",
    HostPhase.BUILD_PLAN_PREPARATION: "Preparing BuildPlan",
    HostPhase.CONTEXT_RENDER_CHECK: "Rendering or checking build context",
}

_SUBPHASE_LABELS = {
    HostSubphase.CANONICAL_WHEEL_PREPARATION: "Preparing the canonical cdh wheel",
    HostSubphase.CANONICAL_IDENTITY_RECONCILIATION: (
        "Reconciling canonical identities"
    ),
}


def host_phase_label(phase: HostPhase) -> str:
    """Return the presentation-owned label for one semantic major phase."""
    return _PHASE_LABELS[phase]


class _PhaseStatus(Enum):
    ACTIVE = "In progress"
    COMPLETED = "Completed"
    FAILED = "Failed"
    INTERRUPTED = "Interrupted"


@dataclass(slots=True)
class _PhaseRecord:
    phase: HostPhase
    status: _PhaseStatus
    started_at: float
    duration: float | None = None
    subphases: list[_SubphaseRecord] = field(default_factory=list)


@dataclass(slots=True)
class _SubphaseRecord:
    subphase: HostSubphase
    status: _PhaseStatus
    started_at: float
    duration: float | None = None


@dataclass(frozen=True, slots=True)
class HostCompletedPhase:
    """One immutable successfully completed major phase."""

    phase: HostPhase
    duration: float


@dataclass(frozen=True, slots=True)
class HostWorkflowSummary:
    """Immutable major-phase facts for the command's final result."""

    phases: tuple[HostCompletedPhase, ...]


class HostWorkflowDisplay(EventSink[HostWorkflowEvent]):
    """Render one serial Host workflow without owning its business control flow."""

    def __init__(
        self,
        *,
        title: str,
        stderr: Console,
        policy: OutputPolicy,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._title = control_safe_text(title)
        self._stderr = stderr
        self._policy = policy
        self._clock = clock
        self._records: list[_PhaseRecord] = []
        self._current: _PhaseRecord | None = None
        self._current_subphase: _SubphaseRecord | None = None
        self._live: Live | None = None
        self._closed = False
        self._sealed_for_external_stream = False

    def emit(self, event: HostWorkflowEvent, /) -> None:
        """Consume one phase event and immediately make its state observable."""
        if isinstance(
            event, (HostWorkflowSucceeded, HostPhaseFailed, HostPhaseInterrupted)
        ):
            self.finish(event)
            return
        if self._closed:
            raise RuntimeError(
                "Cannot emit a Host workflow phase after display teardown."
            )
        if isinstance(event, HostPhaseStarted):
            self._start_phase(event.phase)
        elif isinstance(event, HostPhaseCompleted):
            self._complete_phase(event.phase)
        elif isinstance(event, HostSubphaseStarted):
            self._start_subphase(event.subphase)
        else:
            self._complete_subphase(event.subphase)
        self._render_transition(event)

    @property
    def completed_summary(self) -> HostWorkflowSummary:
        """Return an immutable snapshot for a durable final result tree."""
        return HostWorkflowSummary(
            tuple(
                HostCompletedPhase(record.phase, record.duration)
                for record in self._records
                if record.status is _PhaseStatus.COMPLETED
                and record.duration is not None
            )
        )

    def terminate_for_error(
        self,
        primary_error: BaseException,
        /,
        *,
        interrupted: bool = False,
    ) -> None:
        """Stop safely and terminate the real current phase, when one exists."""
        if self._closed:
            return
        if self._current is not None:
            event: HostWorkflowTerminalEvent = (
                HostPhaseInterrupted(self._current.phase)
                if interrupted
                else HostPhaseFailed(self._current.phase)
            )
            self.finish(event, primary_error=primary_error)
            return
        self._close_without_current(primary_error)

    def finish(
        self,
        event: HostWorkflowTerminalEvent,
        /,
        *,
        primary_error: BaseException | None = None,
    ) -> None:
        """Close once without allowing display failure to mask workflow failure."""
        if self._closed:
            return
        self._closed = True
        try:
            self._record_terminal(event)
            self._render_terminal(event)
        except BaseException:
            self._attempt_plain_fallback(event)
            if primary_error is None:
                raise

    def seal_for_external_stream(self) -> None:
        """Synchronously stop presentation before an externally owned stream."""
        if self._sealed_for_external_stream:
            return
        if self._closed:
            raise RuntimeError(
                "A closed Host workflow cannot be sealed for external output."
            )
        self.finish(HostWorkflowSucceeded())
        self._stderr.file.flush()
        self._sealed_for_external_stream = True

    def _start_phase(self, phase: HostPhase) -> None:
        if self._current is not None:
            raise ValueError(
                "A Host workflow phase cannot begin before the current phase ends."
            )
        record = _PhaseRecord(
            phase=phase,
            status=_PhaseStatus.ACTIVE,
            started_at=self._clock(),
        )
        self._records.append(record)
        self._current = record

    def _complete_phase(self, phase: HostPhase) -> None:
        if self._current_subphase is not None:
            raise ValueError(
                "A Host workflow phase cannot end while a subphase remains active."
            )
        current = self._require_current(phase)
        current.status = _PhaseStatus.COMPLETED
        current.duration = max(0.0, self._clock() - current.started_at)
        self._current = None

    def _start_subphase(self, subphase: HostSubphase) -> None:
        if self._current is None:
            raise ValueError("A Host workflow subphase requires an active major phase.")
        if self._current_subphase is not None:
            raise ValueError(
                "A Host workflow subphase cannot begin before the current "
                "subphase ends."
            )
        record = _SubphaseRecord(
            subphase=subphase,
            status=_PhaseStatus.ACTIVE,
            started_at=self._clock(),
        )
        self._current.subphases.append(record)
        self._current_subphase = record

    def _complete_subphase(self, subphase: HostSubphase) -> None:
        current = self._current_subphase
        if current is None or current.subphase is not subphase:
            raise ValueError(
                "The Host workflow event does not match the current subphase."
            )
        current.status = _PhaseStatus.COMPLETED
        current.duration = max(0.0, self._clock() - current.started_at)
        self._current_subphase = None

    def _record_terminal(self, event: HostWorkflowTerminalEvent) -> None:
        if isinstance(event, HostWorkflowSucceeded):
            if self._current is not None:
                raise ValueError(
                    "A Host workflow cannot succeed while a phase remains active."
                )
            return
        current = self._require_current(event.phase)
        current.duration = max(0.0, self._clock() - current.started_at)
        current.status = (
            _PhaseStatus.FAILED
            if isinstance(event, HostPhaseFailed)
            else _PhaseStatus.INTERRUPTED
        )
        if self._current_subphase is not None:
            self._current_subphase.duration = max(
                0.0, self._clock() - self._current_subphase.started_at
            )
            self._current_subphase.status = current.status
            self._current_subphase = None
        self._current = None

    def _require_current(self, phase: HostPhase) -> _PhaseRecord:
        if self._current is None or self._current.phase is not phase:
            raise ValueError(
                "The Host workflow event does not match the current phase."
            )
        return self._current

    def _render_transition(
        self,
        event: (
            HostPhaseStarted
            | HostPhaseCompleted
            | HostSubphaseStarted
            | HostSubphaseCompleted
        ),
    ) -> None:
        if not self._policy.includes(OutputDetail.NORMAL):
            return
        if isinstance(event, (HostSubphaseStarted, HostSubphaseCompleted)):
            if not self._policy.includes(OutputDetail.VERBOSE):
                return
            if self._policy.allows_live(OutputStream.STDERR):
                if self._live is not None:
                    self._live.update(self._interactive_tree(), refresh=True)
                return
            record = self._records[-1].subphases[-1]
            status = (
                _PhaseStatus.ACTIVE
                if isinstance(event, HostSubphaseStarted)
                else _PhaseStatus.COMPLETED
            )
            self._write_plain_subphase_line(record, status=status)
            return
        if self._policy.allows_live(OutputStream.STDERR):
            renderable = self._interactive_tree()
            if self._live is None:
                self._live = Live(
                    renderable,
                    console=self._stderr,
                    transient=True,
                    redirect_stdout=False,
                    redirect_stderr=False,
                )
                self._live.start(refresh=True)
            else:
                self._live.update(renderable, refresh=True)
            return
        record = self._records[-1]
        status = (
            _PhaseStatus.ACTIVE
            if isinstance(event, HostPhaseStarted)
            else _PhaseStatus.COMPLETED
        )
        if status is _PhaseStatus.COMPLETED and not self._policy.includes(
            OutputDetail.VERBOSE
        ):
            return
        self._write_plain_line(record, status=status)

    def _render_terminal(self, event: HostWorkflowTerminalEvent) -> None:
        live = self._live
        if live is not None:
            if not isinstance(event, HostWorkflowSucceeded):
                live.update(self._interactive_tree(), refresh=True)
            live.stop()
            self._live = None

        if isinstance(event, HostWorkflowSucceeded):
            return
        if self._policy.allows_live(OutputStream.STDERR):
            self._stderr.print(self._interactive_tree(terminal=True), soft_wrap=True)
            return
        terminal_record = self._records[-1]
        self._write_plain_line(terminal_record, status=terminal_record.status)

    def _attempt_plain_fallback(self, event: HostWorkflowTerminalEvent) -> None:
        if isinstance(event, HostWorkflowSucceeded):
            return
        try:
            label = _PHASE_LABELS[event.phase]
            status = (
                _PhaseStatus.FAILED
                if isinstance(event, HostPhaseFailed)
                else _PhaseStatus.INTERRUPTED
            )
            self._write_plain_text(f"{status.value}: {label}")
        except BaseException:
            pass

    def _interactive_tree(self, *, terminal: bool = False) -> RenderableType:
        records = self._terminal_records() if terminal else tuple(self._records)
        capabilities = self._policy.capabilities(OutputStream.STDERR)
        if not capabilities.supports_unicode:
            lines: list[RenderableType] = [self._styled_text(self._title, "bold")]
            for index, record in enumerate(records):
                branch = "`--" if index == len(records) - 1 else "|--"
                lines.append(
                    self._styled_text(
                        f"{branch} {self._record_text(record)}",
                        self._status_style(record.status),
                    )
                )
                if self._policy.includes(OutputDetail.VERBOSE):
                    for subphase_index, subphase in enumerate(record.subphases):
                        subphase_branch = (
                            "`--"
                            if subphase_index == len(record.subphases) - 1
                            else "|--"
                        )
                        lines.append(
                            self._styled_text(
                                f"    {subphase_branch} "
                                f"{self._subphase_text(subphase)}",
                                self._status_style(subphase.status),
                            )
                        )
            return Group(*lines)

        tree = Tree(self._styled_text(self._title, "bold cyan"))
        for record in records:
            if record.status is _PhaseStatus.ACTIVE:
                branch = tree.add(
                    Spinner(
                        "dots",
                        self._styled_text(self._record_text(record), "cyan"),
                    )
                )
            else:
                branch = tree.add(
                    self._styled_text(
                        self._record_text(record),
                        self._status_style(record.status),
                    )
                )
            if self._policy.includes(OutputDetail.VERBOSE):
                for subphase in record.subphases:
                    if subphase.status is _PhaseStatus.ACTIVE:
                        branch.add(
                            Spinner(
                                "dots",
                                self._styled_text(
                                    self._subphase_text(subphase), "cyan"
                                ),
                            )
                        )
                    else:
                        branch.add(
                            self._styled_text(
                                self._subphase_text(subphase),
                                self._status_style(subphase.status),
                            )
                        )
        return tree

    def _terminal_records(self) -> tuple[_PhaseRecord, ...]:
        if self._policy.settings.detail is OutputDetail.QUIET:
            return (self._records[-1],)
        return tuple(self._records)

    def _record_text(self, record: _PhaseRecord) -> str:
        value = f"{record.status.value}: {_PHASE_LABELS[record.phase]}"
        if record.duration is not None and self._policy.includes(OutputDetail.VERBOSE):
            value += f" ({record.duration:.2f}s)"
        return value

    def _subphase_text(self, record: _SubphaseRecord) -> str:
        value = f"{record.status.value}: {_SUBPHASE_LABELS[record.subphase]}"
        if record.duration is not None and self._policy.includes(OutputDetail.DEBUG):
            value += f" ({record.duration:.2f}s)"
        return value

    def _write_plain_line(
        self,
        record: _PhaseRecord,
        *,
        status: _PhaseStatus,
    ) -> None:
        value = f"{status.value}: {_PHASE_LABELS[record.phase]}"
        if (
            status is not _PhaseStatus.ACTIVE
            and record.duration is not None
            and self._policy.includes(OutputDetail.VERBOSE)
        ):
            value += f" ({record.duration:.2f}s)"
        self._write_plain_text(value)

    def _write_plain_subphase_line(
        self,
        record: _SubphaseRecord,
        *,
        status: _PhaseStatus,
    ) -> None:
        value = f"  {status.value}: {_SUBPHASE_LABELS[record.subphase]}"
        if (
            status is not _PhaseStatus.ACTIVE
            and record.duration is not None
            and self._policy.includes(OutputDetail.DEBUG)
        ):
            value += f" ({record.duration:.2f}s)"
        self._write_plain_text(value)

    def _close_without_current(self, primary_error: BaseException) -> None:
        self._closed = True
        try:
            live = self._live
            if live is not None:
                live.stop()
                self._live = None
                if self._policy.includes(OutputDetail.NORMAL):
                    self._stderr.print(
                        self._interactive_tree(terminal=True), soft_wrap=True
                    )
        except BaseException:
            pass

    def _write_plain_text(self, value: str) -> None:
        stream = self._stderr.file
        stream.write(f"{control_safe_text(value)}\n")
        stream.flush()

    def _styled_text(self, value: str, style: str) -> Text:
        capabilities = self._policy.capabilities(OutputStream.STDERR)
        return Text(value, style=style if capabilities.supports_color else None)

    @staticmethod
    def _status_style(status: _PhaseStatus) -> str:
        return {
            _PhaseStatus.ACTIVE: "cyan",
            _PhaseStatus.COMPLETED: "green",
            _PhaseStatus.FAILED: "bold red",
            _PhaseStatus.INTERRUPTED: "bold yellow",
        }[status]
