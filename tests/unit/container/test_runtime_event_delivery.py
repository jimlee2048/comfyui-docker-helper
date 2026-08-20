from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from comfyui_docker_helper.container.download_events import (
    DownloadBackendName,
    DownloadRetryReason,
    DownloadTransferProgress,
)
from comfyui_docker_helper.container.runtime_event_delivery import (
    RuntimeEventDelivery,
)
from comfyui_docker_helper.container.runtime_events import (
    RuntimeDownloadAttemptStarted,
    RuntimeDownloadFailed,
    RuntimeDownloadItemProgress,
    RuntimeDownloadProgressState,
    RuntimeDownloadQueueWarning,
    RuntimeDownloadQueueWarningKind,
    RuntimePresentationSaturated,
    RuntimeWarningCategory,
    RuntimeWarningsAggregated,
)


@dataclass
class _Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


class _ManualWorker:
    def __init__(self, callback: Callable[[], float | None]) -> None:
        self.callback = callback
        self.wakes = 0
        self.closed = False

    def wake(self) -> None:
        self.wakes += 1

    def close(self) -> None:
        self.closed = True

    def run(self) -> float | None:
        return self.callback()


class _ManualWorkerFactory:
    def __init__(self) -> None:
        self.worker: _ManualWorker | None = None

    def __call__(self, callback: Callable[[], float | None]) -> _ManualWorker:
        self.worker = _ManualWorker(callback)
        return self.worker


class _Recorder:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object, /) -> None:
        self.events.append(event)


def _attempt(target: str = "models/a.bin") -> RuntimeDownloadAttemptStarted:
    return RuntimeDownloadAttemptStarted(
        1,
        2,
        target,
        "async",
        DownloadBackendName.HTTPX,
        1,
        3,
    )


def _progress(
    target: str,
    transferred: int,
) -> RuntimeDownloadItemProgress:
    return RuntimeDownloadItemProgress(
        1,
        2,
        target,
        "async",
        1,
        3,
        DownloadTransferProgress(transferred, 100, transferred, 10),
    )


def _failure(target: str) -> RuntimeDownloadFailed:
    return RuntimeDownloadFailed(
        target,
        "async",
        "continue",
        DownloadRetryReason.NETWORK,
        3,
        3,
    )


def test_bounded_delivery_preserves_fifo_and_aggregates_saturation() -> None:
    recorder = _Recorder()
    factory = _ManualWorkerFactory()
    delivery = RuntimeEventDelivery(
        recorder,
        transition_capacity=1,
        warning_capacity=1,
        progress_capacity=1,
        clock=_Clock(),
        worker_factory=factory,
    )
    assert factory.worker is not None

    delivery.emit(_attempt())
    delivery.emit(_attempt("models/b.bin"))
    delivery.emit(_failure("models/a.bin"))
    delivery.emit(
        RuntimeDownloadQueueWarning(
            RuntimeDownloadQueueWarningKind.STOPPED_AFTER_FAILURE
        )
    )
    factory.worker.run()

    assert isinstance(recorder.events[0], RuntimeDownloadAttemptStarted)
    assert isinstance(recorder.events[1], RuntimeDownloadFailed)
    assert RuntimeWarningsAggregated(RuntimeWarningCategory.DOWNLOAD_FAILURE, 1) in (
        recorder.events
    )
    assert RuntimePresentationSaturated(1) in recorder.events
    recorder.events.clear()
    factory.worker.run()
    assert recorder.events == []

    delivery.close()
    assert factory.worker.closed is True


def test_delivery_coalesces_two_scopes_and_reports_stall_and_recovery() -> None:
    clock = _Clock()
    recorder = _Recorder()
    factory = _ManualWorkerFactory()
    delivery = RuntimeEventDelivery(
        recorder,
        progress_capacity=2,
        clock=clock,
        worker_factory=factory,
    )
    assert factory.worker is not None
    first_scope = object()
    second_scope = object()

    delivery.emit_progress(first_scope, _progress("models/a.bin", 1))
    delivery.emit_progress(second_scope, _progress("models/b.bin", 2))
    factory.worker.run()
    assert recorder.events == []

    clock.now = 10.0
    delivery.emit_progress(first_scope, _progress("models/a.bin", 11))
    delivery.emit_progress(second_scope, _progress("models/b.bin", 22))
    factory.worker.run()
    active = [
        event
        for event in recorder.events
        if isinstance(event, RuntimeDownloadItemProgress)
    ]
    assert [event.progress.transferred_bytes for event in active] == [11, 22]
    assert {event.state for event in active} == {RuntimeDownloadProgressState.ACTIVE}

    recorder.events.clear()
    clock.now = 40.0
    factory.worker.run()
    stalled = [
        event
        for event in recorder.events
        if isinstance(event, RuntimeDownloadItemProgress)
    ]
    assert {event.state for event in stalled} == {RuntimeDownloadProgressState.STALLED}

    recorder.events.clear()
    delivery.emit_progress(first_scope, _progress("models/a.bin", 12))
    factory.worker.run()
    assert recorder.events == [
        replace(
            _progress("models/a.bin", 12),
            state=RuntimeDownloadProgressState.RECOVERED,
        )
    ]


def test_progress_capacity_one_keeps_one_of_two_scopes_and_reports_omission() -> None:
    clock = _Clock()
    recorder = _Recorder()
    factory = _ManualWorkerFactory()
    delivery = RuntimeEventDelivery(
        recorder,
        progress_capacity=1,
        clock=clock,
        worker_factory=factory,
    )
    assert factory.worker is not None
    first_scope = object()
    second_scope = object()

    delivery.emit_progress(first_scope, _progress("models/a.bin", 1))
    delivery.emit_progress(second_scope, _progress("models/b.bin", 2))
    clock.now = 10.0
    delivery.emit_progress(second_scope, _progress("models/b.bin", 22))
    factory.worker.run()

    surviving = [
        event
        for event in recorder.events
        if isinstance(event, RuntimeDownloadItemProgress)
    ]
    assert [event.target for event in surviving] == ["models/b.bin"]
    assert recorder.events.count(RuntimePresentationSaturated(1)) == 1

    recorder.events.clear()
    factory.worker.run()
    assert recorder.events == []


def test_quiet_admission_discards_information_without_false_saturation() -> None:
    recorder = _Recorder()
    factory = _ManualWorkerFactory()
    delivery = RuntimeEventDelivery(
        recorder,
        transition_capacity=1,
        information_enabled=False,
        progress_enabled=False,
        clock=_Clock(),
        worker_factory=factory,
    )
    assert factory.worker is not None

    delivery.emit(_attempt())
    delivery.emit(_attempt("models/b.bin"))
    delivery.emit_progress(object(), _progress("models/a.bin", 10))
    delivery.emit(_failure("models/a.bin"))
    factory.worker.run()

    assert recorder.events == [_failure("models/a.bin")]


def test_real_delivery_worker_never_blocks_background_offer_on_rendering() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingRecorder(_Recorder):
        def emit(self, event: object, /) -> None:
            entered.set()
            release.wait(timeout=1)
            super().emit(event)

    recorder = BlockingRecorder()
    delivery = RuntimeEventDelivery(
        recorder,
        transition_capacity=1,
        clock=time.monotonic,
    )
    delivery.emit(_attempt())
    assert entered.wait(timeout=1)

    def offer_while_renderer_is_blocked() -> None:
        delivery.emit(_attempt("models/b.bin"))
        delivery.emit(_attempt("models/c.bin"))

    producer = threading.Thread(target=offer_while_renderer_is_blocked)
    producer.start()
    producer.join(timeout=0.5)
    assert producer.is_alive() is False

    release.set()
    delivery.close()
    assert (
        sum(
            isinstance(event, RuntimePresentationSaturated) for event in recorder.events
        )
        == 1
    )
