"""Failure-isolated direct delivery for Runtime presentation facts."""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from comfyui_docker_helper.cli_output import EventSink
from comfyui_docker_helper.container.download_cadence import (
    DownloadCadenceDecision,
    PlainDownloadCadence,
)
from comfyui_docker_helper.container.runtime_events import (
    RuntimeDownloadAttemptStarted,
    RuntimeDownloadFailed,
    RuntimeDownloadItemCompleted,
    RuntimeDownloadItemProgress,
    RuntimeDownloadItemRetryScheduled,
    RuntimeDownloadProgressState,
    RuntimeDownloadQueueSummary,
    RuntimeDownloadQueueWarning,
    RuntimeDownloadReconciled,
    RuntimeEvent,
    RuntimePresentationSaturated,
    RuntimeSshWarning,
    RuntimeStaleCleanupPending,
    RuntimeWarningCategory,
    RuntimeWarningsAggregated,
)

type RuntimeBackgroundEvent = (
    RuntimeDownloadReconciled
    | RuntimeDownloadQueueSummary
    | RuntimeDownloadAttemptStarted
    | RuntimeDownloadItemProgress
    | RuntimeDownloadItemRetryScheduled
    | RuntimeDownloadItemCompleted
    | RuntimeDownloadQueueWarning
    | RuntimeStaleCleanupPending
    | RuntimeDownloadFailed
    | RuntimeSshWarning
)


class _FailureLatchingRuntimeEventSink(EventSink[RuntimeEvent]):
    """Deliver directly until presentation fails, then keep execution authoritative."""

    def __init__(self, sink: EventSink[RuntimeEvent]) -> None:
        self._sink = sink
        self._failed = False

    def emit(self, event: RuntimeEvent, /) -> None:
        if self._failed:
            return
        try:
            self._sink.emit(event)
        except Exception:
            # The Runtime logging broker remains the authority for primary stream
            # failure. Presentation must not block or replace lifecycle cleanup.
            self._failed = True


def safe_runtime_event_sink(
    sink: EventSink[RuntimeEvent] | None,
) -> EventSink[RuntimeEvent] | None:
    """Return one idempotently wrapped, failure-latching direct event sink."""
    if sink is None:
        return None
    if isinstance(sink, _FailureLatchingRuntimeEventSink):
        return sink
    return _FailureLatchingRuntimeEventSink(sink)


class RuntimeDeliveryWorker(Protocol):
    def wake(self) -> None: ...

    def close(self) -> None: ...


class RuntimeBackgroundEventSink(EventSink[RuntimeBackgroundEvent], Protocol):
    """Background-only delivery surface with private transfer scopes."""

    def emit_progress(
        self,
        scope: object,
        event: RuntimeDownloadItemProgress,
    ) -> None: ...

    def close_progress(self, scope: object) -> None: ...


type RuntimeDeliveryWorkerFactory = Callable[
    [Callable[[], float | None]], RuntimeDeliveryWorker
]


@dataclass(slots=True)
class _ProgressSlot:
    latest: RuntimeDownloadItemProgress
    cadence: PlainDownloadCadence
    pending: DownloadCadenceDecision = DownloadCadenceDecision.SUPPRESS


@dataclass(frozen=True, slots=True)
class _QueuedEvent:
    sequence: int
    event: RuntimeBackgroundEvent


class RuntimeEventDelivery(EventSink[RuntimeBackgroundEvent]):
    """Bound and cadence-limit facts offered by genuine background producers."""

    def __init__(
        self,
        event_sink: EventSink[RuntimeEvent],
        *,
        transition_capacity: int = 64,
        warning_capacity: int = 16,
        progress_capacity: int = 8,
        clock: Callable[[], float],
        information_enabled: bool = True,
        progress_enabled: bool = True,
        worker_factory: RuntimeDeliveryWorkerFactory | None = None,
    ) -> None:
        for value, label in (
            (transition_capacity, "transition capacity"),
            (warning_capacity, "warning capacity"),
            (progress_capacity, "progress capacity"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"Runtime delivery {label} must be positive")
        self._event_sink = safe_runtime_event_sink(event_sink)
        assert self._event_sink is not None
        self._transition_capacity = transition_capacity
        self._warning_capacity = warning_capacity
        self._progress_capacity = progress_capacity
        self._clock = clock
        self._information_enabled = information_enabled
        self._progress_enabled = progress_enabled
        self._lock = threading.Lock()
        self._transitions: deque[_QueuedEvent] = deque()
        self._warnings: deque[_QueuedEvent] = deque()
        self._warning_counts = {category: 0 for category in RuntimeWarningCategory}
        self._progress: OrderedDict[object, _ProgressSlot] = OrderedDict()
        self._omitted_transitions = 0
        self._next_sequence = 0
        self._closed = False
        factory = worker_factory or _ConditionRuntimeDeliveryWorker
        self._worker = factory(self._drain_once)

    def emit(self, event: RuntimeBackgroundEvent, /) -> None:
        """Offer one transition or warning without rendering or waiting."""
        if isinstance(event, RuntimeDownloadItemProgress):
            raise TypeError("Runtime progress requires one private operation scope")
        with self._lock:
            if self._closed:
                return
            category = _warning_category(event)
            if category is not None:
                if len(self._warnings) < self._warning_capacity:
                    self._warnings.append(self._queued(event))
                else:
                    self._warning_counts[category] += 1
            elif not self._information_enabled:
                return
            elif len(self._transitions) < self._transition_capacity:
                self._transitions.append(self._queued(event))
            else:
                self._omitted_transitions += 1
        self._worker.wake()

    def emit_progress(
        self,
        scope: object,
        event: RuntimeDownloadItemProgress,
    ) -> None:
        """Replace the latest progress for one private producer-owned scope."""
        with self._lock:
            if self._closed or not self._progress_enabled:
                return
            slot = self._progress.get(scope)
            if slot is None:
                if len(self._progress) >= self._progress_capacity:
                    self._progress.popitem(last=False)
                    self._omitted_transitions += 1
                slot = _ProgressSlot(
                    latest=event,
                    cadence=PlainDownloadCadence(clock=self._clock),
                )
                self._progress[scope] = slot
            else:
                slot.latest = event
                self._progress.move_to_end(scope)
            decision = slot.cadence.observe(event.progress.transferred_bytes)
            if decision is not DownloadCadenceDecision.SUPPRESS:
                slot.pending = decision
        self._worker.wake()

    def close_progress(self, scope: object) -> None:
        """Forget one completed or abandoned transfer scope without output."""
        with self._lock:
            self._progress.pop(scope, None)
        self._worker.wake()

    def close(self) -> None:
        """Stop cadence polling and drain retained facts before broker teardown."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._worker.close()
        self._drain_once(final=True)

    def __enter__(self) -> RuntimeEventDelivery:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _queued(self, event: RuntimeBackgroundEvent) -> _QueuedEvent:
        queued = _QueuedEvent(self._next_sequence, event)
        self._next_sequence += 1
        return queued

    def _drain_once(self, *, final: bool = False) -> float | None:
        outgoing: list[RuntimeEvent] = []
        next_delays: list[float] = []
        with self._lock:
            retained = [*self._warnings, *self._transitions]
            retained.sort(key=lambda item: item.sequence)
            outgoing.extend(item.event for item in retained)
            self._warnings.clear()
            self._transitions.clear()
            self._next_sequence = 0
            for category, count in self._warning_counts.items():
                if count:
                    outgoing.append(RuntimeWarningsAggregated(category, count))
                    self._warning_counts[category] = 0
            if self._omitted_transitions:
                outgoing.append(RuntimePresentationSaturated(self._omitted_transitions))
                self._omitted_transitions = 0
            for slot in self._progress.values():
                decision = slot.pending
                slot.pending = DownloadCadenceDecision.SUPPRESS
                if decision is DownloadCadenceDecision.SUPPRESS and not final:
                    decision = slot.cadence.poll()
                state = _progress_state(decision)
                if state is not None:
                    outgoing.append(replace(slot.latest, state=state))
                if not final:
                    next_delays.append(slot.cadence.next_poll_delay())
            if final:
                self._progress.clear()
        for event in outgoing:
            self._event_sink.emit(event)
        if final or not next_delays:
            return None
        return min(next_delays)


class _ConditionRuntimeDeliveryWorker:
    """One deadline-aware worker shared by all controller Runtime producers."""

    def __init__(self, callback: Callable[[], float | None]) -> None:
        self._callback = callback
        self._condition = threading.Condition()
        self._closed = False
        self._revision = 0
        self._thread = threading.Thread(
            target=self._run,
            name="cdh-runtime-event-delivery",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        with self._condition:
            self._revision += 1
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify()
        self._thread.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                revision = self._revision
            delay = self._callback()
            with self._condition:
                self._condition.wait_for(
                    lambda observed=revision: (
                        self._closed or self._revision != observed
                    ),
                    timeout=delay,
                )


def _warning_category(
    event: RuntimeBackgroundEvent,
) -> RuntimeWarningCategory | None:
    if isinstance(event, RuntimeStaleCleanupPending):
        return RuntimeWarningCategory.STALE_CLEANUP
    if isinstance(event, RuntimeDownloadFailed):
        return RuntimeWarningCategory.DOWNLOAD_FAILURE
    if isinstance(event, RuntimeDownloadQueueWarning):
        return RuntimeWarningCategory.DOWNLOAD_FAILURE
    if isinstance(event, RuntimeSshWarning):
        return RuntimeWarningCategory.SSH
    return None


def _progress_state(
    decision: DownloadCadenceDecision,
) -> RuntimeDownloadProgressState | None:
    return {
        DownloadCadenceDecision.SUPPRESS: None,
        DownloadCadenceDecision.ACTIVE: RuntimeDownloadProgressState.ACTIVE,
        DownloadCadenceDecision.STALLED: RuntimeDownloadProgressState.STALLED,
        DownloadCadenceDecision.RECOVERED: RuntimeDownloadProgressState.RECOVERED,
    }[decision]
