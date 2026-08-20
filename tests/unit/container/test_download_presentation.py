"""Semantic contracts for durable Container download presentation."""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError
from io import StringIO

import pytest

from comfyui_docker_helper.cli_output.policy import (
    CliOutputSettings,
    OutputContextKind,
    OutputDetail,
    OutputPolicy,
    StreamCapabilities,
)
from comfyui_docker_helper.container import presentation as presentation_module
from comfyui_docker_helper.container.download_events import (
    DownloadAttemptStarted,
    DownloadBackendName,
    DownloadBatchCompleted,
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
from comfyui_docker_helper.container.presentation import (
    ContainerDownloadDisplay,
    ContainerDownloadInvocation,
    default_container_download_invocation,
)
from comfyui_docker_helper.container.transfer_core import DownloadCancelled


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _RecordingStream(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class _TerminalStream(_RecordingStream):
    def isatty(self) -> bool:
        return True


class _AsciiTerminalStream(_TerminalStream):
    @property
    def encoding(self) -> str:
        return "ascii"

    def write(self, value: str) -> int:
        value.encode(self.encoding)
        return super().write(value)


class _FailingStream(_RecordingStream):
    def __init__(self) -> None:
        super().__init__()
        self.failure: BaseException | None = None

    def write(self, value: str) -> int:
        if self.failure is not None:
            raise self.failure
        return super().write(value)


class _FailingTerminalStream(_TerminalStream):
    def __init__(self) -> None:
        super().__init__()
        self.failure: BaseException | None = None

    def write(self, value: str) -> int:
        if self.failure is not None:
            raise self.failure
        return super().write(value)


class _InterruptingTerminalStream(_TerminalStream):
    def __init__(self, *, fail_on_write: int, failure: BaseException) -> None:
        super().__init__()
        self._fail_on_write = fail_on_write
        self._failure = failure
        self._write_count = 0

    def write(self, value: str) -> int:
        self._write_count += 1
        if self._write_count == self._fail_on_write:
            raise self._failure
        return super().write(value)


class _ManualWatchdog:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.close_count = 0
        self.wake_count = 0
        self.fire_count = 0

    def wake(self) -> None:
        self.wake_count += 1

    def close(self) -> None:
        self.close_count += 1

    def fire(self) -> float | None:
        self.fire_count += 1
        return self.callback()


class _ManualWatchdogFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.watchdog: _ManualWatchdog | None = None

    def __call__(self, callback) -> _ManualWatchdog:
        self.calls += 1
        self.watchdog = _ManualWatchdog(callback)
        return self.watchdog


def test_condition_watchdog_wakes_recomputes_deadline_and_closes() -> None:
    immediate = threading.Event()
    woken = threading.Event()
    deadline = threading.Event()
    calls_lock = threading.Lock()
    call_count = 0

    def callback() -> float | None:
        nonlocal call_count
        with calls_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            immediate.set()
            return None
        if current_call == 2:
            woken.set()
            return 0.0
        deadline.set()
        return None

    watchdog = presentation_module._ConditionDownloadWatchdog(callback)
    try:
        assert immediate.wait(timeout=1)
        with calls_lock:
            assert call_count == 1

        watchdog.wake()
        assert woken.wait(timeout=1)
        assert deadline.wait(timeout=1)

        watchdog.wake()
    finally:
        watchdog.close()
    watchdog.close()

    with calls_lock:
        calls_after_close = call_count
    watchdog.wake()
    with calls_lock:
        assert call_count == calls_after_close


def _policy(
    detail: OutputDetail = OutputDetail.NORMAL,
    *,
    live_stderr: bool = False,
) -> OutputPolicy:
    plain = StreamCapabilities.from_facts(
        is_terminal=False,
        no_color=False,
        term=None,
        encoding="utf-8",
    )
    stderr = (
        StreamCapabilities.from_facts(
            is_terminal=True,
            no_color=False,
            term="xterm-256color",
            encoding="utf-8",
        )
        if live_stderr
        else plain
    )
    return OutputPolicy(
        settings=CliOutputSettings(detail=detail),
        stdout=plain,
        stderr=stderr,
        context=OutputContextKind.ONE_SHOT,
    )


def _display(
    *,
    detail: OutputDetail = OutputDetail.NORMAL,
    clock: _Clock | None = None,
) -> tuple[ContainerDownloadDisplay, _RecordingStream, _Clock]:
    selected_clock = clock or _Clock()
    stream = _RecordingStream()
    return (
        ContainerDownloadDisplay(
            stderr=stream,
            policy=_policy(detail),
            clock=selected_clock,
        ),
        stream,
        selected_clock,
    )


def _invocation(
    *,
    detail: OutputDetail = OutputDetail.NORMAL,
    clock: _Clock | None = None,
    stream: _RecordingStream | None = None,
) -> tuple[
    ContainerDownloadInvocation,
    _RecordingStream,
    _Clock,
    _ManualWatchdogFactory,
]:
    selected_clock = clock or _Clock()
    selected_stream = stream or _RecordingStream()
    factory = _ManualWatchdogFactory()
    return (
        ContainerDownloadInvocation(
            stderr=selected_stream,
            policy=_policy(detail),
            clock=selected_clock,
            watchdog_factory=factory,
        ),
        selected_stream,
        selected_clock,
        factory,
    )


def _start_item(display: ContainerDownloadDisplay, *, total: int = 1) -> None:
    display.emit(
        DownloadItemStarted(
            index=1,
            total=total,
            target="models/checkpoints/model.bin",
            backend=DownloadBackendName.HTTPX,
            max_attempts=3,
            checksum_expected=True,
        )
    )


def test_download_events_are_immutable_and_reject_unsafe_scope() -> None:
    event = DownloadItemStarted(
        index=1,
        total=1,
        target="models/model.bin",
        backend=DownloadBackendName.HTTPX,
        max_attempts=3,
        checksum_expected=True,
    )

    with pytest.raises(FrozenInstanceError):
        event.target = "other.bin"  # type: ignore[misc]
    for target in ("/absolute/model.bin", "../model.bin", "models\\model.bin"):
        with pytest.raises(ValueError):
            DownloadItemStarted(
                index=1,
                total=1,
                target=target,
                backend=DownloadBackendName.HTTPX,
                max_attempts=3,
                checksum_expected=False,
            )
    unsafe_sentinel = "https://user:secret@example.test/model.bin"
    with pytest.raises(ValueError):
        DownloadItemStarted(
            index=1,
            total=1,
            target=unsafe_sentinel,
            backend=DownloadBackendName.HTTPX,
            max_attempts=3,
            checksum_expected=False,
        )

    display, stream, _ = _display()
    display.emit(event)
    assert unsafe_sentinel not in stream.getvalue()


def test_known_total_progress_can_show_percentage_rate_and_eta() -> None:
    assert presentation_module._percentage(0, 0) == 0
    display, stream, clock = _display()
    _start_item(display)
    display.emit(DownloadAttemptStarted(1))
    clock.now = 10
    display.emit(
        DownloadTransferProgress(
            transferred_bytes=999,
            total_bytes=1000,
            stored_bytes=768,
            reported_rate=128,
        )
    )

    output = stream.getvalue()
    assert "Transferring" in output
    assert "%" in output
    assert "/s" in output
    assert "ETA" in output
    assert "stored" not in output
    assert "backend=" not in output
    assert "100%" not in output
    assert "[1/1]" in output
    assert "models/checkpoints/model.bin" in output
    assert "attempt [1/3]" in output
    assert stream.flushes >= 3

    clock.now = 20
    display.emit(DownloadTransferProgress(1000, 1000, 768, 128))
    display.emit(DownloadVerificationStarted())
    display.emit(DownloadVerificationCompleted())
    output = stream.getvalue()
    assert "100%" in output
    assert "Verifying downloaded bytes" in output
    assert "verification complete" not in output


def test_unknown_total_progress_never_invents_percentage_or_eta() -> None:
    display, stream, clock = _display()
    _start_item(display)
    display.emit(DownloadAttemptStarted(1))
    clock.now = 10
    display.emit(
        DownloadTransferProgress(
            transferred_bytes=512,
            total_bytes=None,
            stored_bytes=None,
            reported_rate=128,
        )
    )

    progress = next(
        line for line in stream.getvalue().splitlines() if "Transferring" in line
    )
    assert "/s" in progress
    assert "%" not in progress
    assert "ETA" not in progress


def test_fake_watchdog_cadence_emits_stalled_recovered_active_sequence() -> None:
    invocation, stream, clock, factory = _invocation()
    assert factory.watchdog is not None
    with invocation as display:
        _start_item(display)
        display.emit(DownloadAttemptStarted(1))

        clock.now = 29
        factory.watchdog.fire()
        assert "Transfer stalled" not in stream.getvalue()
        clock.now = 30
        factory.watchdog.fire()
        assert stream.getvalue().count("Transfer stalled") == 1
        clock.now = 59
        factory.watchdog.fire()
        assert stream.getvalue().count("Transfer stalled") == 1
        clock.now = 60
        factory.watchdog.fire()
        assert stream.getvalue().count("Transfer stalled") == 2

        clock.now = 61
        display.emit(DownloadTransferProgress(10, 100, 10))
        clock.now = 70
        display.emit(DownloadTransferProgress(20, 100, 20))
        clock.now = 71
        display.emit(DownloadTransferProgress(30, 100, 30))

    output = stream.getvalue()
    stalled = output.index("Transfer stalled")
    recovered = output.index("Transfer resumed")
    active = output.index("Transferring")
    assert stalled < recovered < active
    assert output.count("Transfer stalled") == 2
    assert output.count("Transferring") == 1
    assert "models/checkpoints/model.bin" in output
    assert "attempt [1/3]" in output
    assert factory.watchdog.close_count == 1


def test_transitions_and_retry_are_immediate_with_bounded_detail() -> None:
    display, stream, _ = _display(detail=OutputDetail.DEBUG)
    _start_item(display)
    display.emit(DownloadAttemptStarted(1))
    display.emit(
        DownloadRetryScheduled(
            failed_attempt=1,
            next_attempt=2,
            delay_seconds=1,
            reason=DownloadRetryReason.TEMPORARY_SERVER,
            http_status=503,
        )
    )
    display.emit(DownloadAttemptStarted(2))
    transitions = (
        DownloadVerificationStarted(),
        DownloadVerificationCompleted(),
        DownloadPlacementStarted(),
        DownloadPlacementCompleted(),
    )
    for event in transitions:
        display.emit(event)
    display.emit(
        DownloadItemCompleted(
            status=DownloadItemStatus.DOWNLOADED,
            observed_bytes=1,
            checksum_verified=True,
        )
    )
    display.emit(DownloadFinalVerificationStarted(item_count=1, checksum_count=1))
    display.emit(DownloadFinalVerificationCompleted())

    output = stream.getvalue()
    assert "[2/3]" in output
    assert "temporarily unavailable" in output
    assert "backend=httpx" in output
    assert "http_status=503" in output
    positions = [
        output.index(marker)
        for marker in ("Verifying", "verification complete", "Placing", "placed")
    ]
    assert positions == sorted(positions)


def test_quiet_suppresses_all_download_events() -> None:
    display, stream, clock = _display(detail=OutputDetail.QUIET)
    _start_item(display)
    display.emit(DownloadAttemptStarted(1))
    clock.now = 40
    display.emit(DownloadTransferProgress(1, None, 1))
    display.poll()
    display.emit(DownloadVerificationStarted())
    display.emit(DownloadVerificationCompleted())
    display.emit(DownloadPlacementStarted())
    display.emit(DownloadPlacementCompleted())
    display.emit(
        DownloadItemCompleted(
            status=DownloadItemStatus.DOWNLOADED,
            observed_bytes=1,
            checksum_verified=True,
        )
    )
    display.emit(DownloadFinalVerificationStarted(item_count=1, checksum_count=1))
    display.emit(DownloadFinalVerificationCompleted())
    display.emit(DownloadBatchCompleted(item_count=1, checksum_verified_count=1))

    assert stream.getvalue() == ""
    assert stream.flushes == 0


@pytest.mark.parametrize(
    ("detail", "live_stderr"),
    [
        (OutputDetail.QUIET, False),
        (OutputDetail.NORMAL, True),
    ],
)
def test_quiet_or_live_invocation_never_constructs_watchdog(
    detail: OutputDetail,
    live_stderr: bool,
) -> None:
    stream = _RecordingStream()

    def fail_factory(_callback):
        raise AssertionError("this output mode must not construct a watchdog")

    invocation = ContainerDownloadInvocation(
        stderr=stream,
        policy=_policy(detail, live_stderr=live_stderr),
        clock=_Clock(),
        watchdog_factory=fail_factory,
    )
    with invocation as display:
        _start_item(display)
        display.emit(DownloadAttemptStarted(1))
        display.emit(DownloadTransferProgress(1, None, 1))

    if detail is OutputDetail.QUIET:
        assert stream.getvalue() == ""
    else:
        assert "models/checkpoints/model.bin" in stream.getvalue()


def test_invocation_close_is_idempotent_and_fire_after_close_cannot_write() -> None:
    invocation, stream, _, factory = _invocation()
    assert factory.watchdog is not None
    _start_item(invocation)
    invocation.emit(DownloadAttemptStarted(1))

    invocation.close()
    output = stream.getvalue()
    invocation.close()
    factory.watchdog.fire()

    assert factory.calls == 1
    assert factory.watchdog.close_count == 1
    assert stream.getvalue() == output


def test_background_stderr_failure_is_surfaced_on_close() -> None:
    stream = _FailingStream()
    invocation, _, clock, factory = _invocation(stream=stream)
    assert factory.watchdog is not None
    _start_item(invocation)
    invocation.emit(DownloadAttemptStarted(1))
    failure = OSError("stderr-write-sentinel")
    stream.failure = failure
    clock.now = 30

    factory.watchdog.fire()
    factory.watchdog.fire()
    with pytest.raises(OSError) as raised:
        invocation.close()

    assert raised.value is failure
    assert factory.watchdog.close_count == 1


@pytest.mark.parametrize(
    "primary",
    [
        RuntimeError("primary ordinary failure"),
        DownloadCancelled("primary cancellation"),
        KeyboardInterrupt("primary interruption"),
    ],
)
def test_primary_failure_wins_over_background_watchdog_failure(
    primary: BaseException,
) -> None:
    stream = _FailingStream()
    invocation, _, clock, factory = _invocation(stream=stream)
    assert factory.watchdog is not None
    failure = OSError("background-write-sentinel")

    with pytest.raises(type(primary)) as raised, invocation as display:
        _start_item(display)
        display.emit(DownloadAttemptStarted(1))
        stream.failure = failure
        clock.now = 30
        factory.watchdog.fire()
        raise primary

    assert raised.value is primary
    assert factory.watchdog.close_count == 1


def test_plain_output_is_line_oriented_and_control_free() -> None:
    display, stream, _ = _display(detail=OutputDetail.VERBOSE)
    _start_item(display)
    display.emit(
        DownloadItemCompleted(
            status=DownloadItemStatus.SKIPPED,
            observed_bytes=0,
            checksum_verified=True,
        )
    )
    display.emit(DownloadFinalVerificationStarted(item_count=1, checksum_count=1))
    display.emit(DownloadFinalVerificationCompleted())
    display.emit(DownloadBatchCompleted(item_count=1, checksum_verified_count=1))

    output = stream.getvalue()
    assert output.endswith("\n")
    assert "1 required file" in output
    assert "1 required files" not in output
    assert "\x1b" not in output
    assert "\r" not in output


def test_rich_progress_preserves_transfer_domains_and_durable_results() -> None:
    stream = _TerminalStream()
    policy = _policy(OutputDetail.VERBOSE, live_stderr=True)

    with ContainerDownloadInvocation(stderr=stream, policy=policy) as display:
        _start_item(display)
        display.emit(DownloadAttemptStarted(1))
        display.emit(
            DownloadTransferProgress(
                transferred_bytes=999,
                total_bytes=1000,
                stored_bytes=768,
                reported_rate=128,
            )
        )
        display.emit(DownloadVerificationStarted())
        display.emit(DownloadVerificationCompleted())
        display.emit(DownloadPlacementStarted())
        display.emit(DownloadPlacementCompleted())
        display.emit(
            DownloadItemCompleted(
                status=DownloadItemStatus.DOWNLOADED,
                observed_bytes=768,
                checksum_verified=True,
            )
        )
        display.emit(DownloadFinalVerificationStarted(item_count=1, checksum_count=1))
        display.emit(DownloadFinalVerificationCompleted())
        display.emit(DownloadBatchCompleted(item_count=1, checksum_verified_count=1))

    output = stream.getvalue()
    assert "999 B / 1000 B" in output
    assert "%" in output
    assert "128 B/s" in output
    assert "ETA" in output
    assert "stored 768 B" in output
    assert "Verifying downloaded bytes" in output
    assert "Placing required file" in output
    assert "models/checkpoints/model.bin: Downloaded: 768 B" in output
    assert output.count("Downloaded: 768 B") == 1
    assert output.count("Downloads complete: 1 required file") == 1
    assert "backend=" not in output


def test_rich_unknown_total_never_invents_percentage_or_eta() -> None:
    stream = _TerminalStream()

    with ContainerDownloadInvocation(
        stderr=stream,
        policy=_policy(live_stderr=True),
    ) as display:
        _start_item(display)
        display.emit(DownloadAttemptStarted(1))
        display.emit(
            DownloadTransferProgress(
                transferred_bytes=512,
                total_bytes=None,
                stored_bytes=256,
                reported_rate=128,
            )
        )

    output = stream.getvalue()
    assert "512 B" in output
    assert "128 B/s" in output
    assert "%" not in output
    assert "ETA" not in output
    assert "stored" not in output


def test_rich_progress_uses_ascii_bar_when_terminal_encoding_requires_it() -> None:
    stream = _AsciiTerminalStream()
    plain = StreamCapabilities.from_facts(
        is_terminal=False,
        no_color=False,
        term=None,
        encoding="utf-8",
    )
    ascii_terminal = StreamCapabilities.from_facts(
        is_terminal=True,
        no_color=False,
        term="xterm",
        encoding="ascii",
    )
    policy = OutputPolicy(
        settings=CliOutputSettings(),
        stdout=plain,
        stderr=ascii_terminal,
        context=OutputContextKind.ONE_SHOT,
    )

    with ContainerDownloadInvocation(stderr=stream, policy=policy) as display:
        _start_item(display)
        display.emit(DownloadAttemptStarted(1))
        display.emit(DownloadTransferProgress(0, 0, 0, 0))

    output = stream.getvalue()
    assert output
    output.encode("ascii")
    assert "0 B / 0 B (0%)" in output
    bar = presentation_module._AsciiProgressBarColumn(width=4)
    progress = presentation_module.Progress(bar, auto_refresh=False)
    progress.add_task("zero", total=0, completed=0)
    progress.add_task("empty", total=1, completed=0)
    zero_total, empty = progress.tasks
    assert bar.render(zero_total).plain == bar.render(empty).plain


def test_rich_start_interrupt_clears_live_registration() -> None:
    failure = KeyboardInterrupt("start-write-interrupted")
    stream = _InterruptingTerminalStream(
        fail_on_write=2,
        failure=failure,
    )
    display = presentation_module._RichContainerDownloadDisplay(
        stderr=stream,
        policy=_policy(live_stderr=True),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _start_item(display)

    assert raised.value is failure
    assert display._progress.live.is_started is False
    display.close()
    display.close()


def test_rich_teardown_preserves_primary_failure() -> None:
    stream = _FailingTerminalStream()
    invocation = ContainerDownloadInvocation(
        stderr=stream,
        policy=_policy(live_stderr=True),
    )
    primary = KeyboardInterrupt("primary interruption")

    with pytest.raises(KeyboardInterrupt) as raised, invocation as display:
        _start_item(display)
        display.emit(DownloadAttemptStarted(1))
        stream.failure = OSError("rich-stop-write-sentinel")
        raise primary

    invocation.close()
    assert raised.value is primary


def test_rich_close_surfaces_stop_failure_without_primary() -> None:
    stream = _FailingTerminalStream()
    invocation = ContainerDownloadInvocation(
        stderr=stream,
        policy=_policy(live_stderr=True),
    )
    _start_item(invocation)
    failure = OSError("rich-stop-write-sentinel")
    stream.failure = failure

    with pytest.raises(OSError) as raised:
        invocation.close()

    assert raised.value is failure
    invocation.close()


def test_default_download_invocation_detects_call_time_streams_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _RecordingStream()
    stderr = _TerminalStream()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(presentation_module.sys, "stdout", stdout)
    monkeypatch.setattr(presentation_module.sys, "stderr", stderr)

    invocation = default_container_download_invocation(CliOutputSettings())
    with invocation as display:
        _start_item(display)
        display.emit(DownloadAttemptStarted(1))

    assert "models/checkpoints/model.bin" in stderr.getvalue()
    assert stdout.getvalue() == ""
