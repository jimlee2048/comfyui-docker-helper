"""Semantic contracts for Host phase presentation and teardown."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from io import StringIO
from typing import ClassVar

import pytest
from rich.console import Console

from comfyui_docker_helper.cli_output.policy import (
    CliOutputSettings,
    OutputContextKind,
    OutputDetail,
    OutputPolicy,
    StreamCapabilities,
)
from comfyui_docker_helper.host import workflow_display as display_module
from comfyui_docker_helper.host.events import (
    HostPhase,
    HostPhaseCompleted,
    HostPhaseFailed,
    HostPhaseInterrupted,
    HostPhaseStarted,
    HostSubphase,
    HostSubphaseCompleted,
    HostSubphaseStarted,
    HostWorkflowSucceeded,
)
from comfyui_docker_helper.host.workflow_display import HostWorkflowDisplay


class _FlushTrackingStream(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def _capabilities(*, terminal: bool) -> StreamCapabilities:
    return StreamCapabilities.from_facts(
        is_terminal=terminal,
        no_color=False,
        term="xterm-256color",
        encoding="utf-8",
    )


def _display(
    *,
    title: str = "Preparing host workflow",
    detail: OutputDetail = OutputDetail.NORMAL,
    stdout_terminal: bool = False,
    stderr_terminal: bool = False,
    clock: Callable[[], float] | None = None,
) -> tuple[HostWorkflowDisplay, StringIO]:
    stderr = _FlushTrackingStream()
    console = Console(
        file=stderr,
        force_terminal=stderr_terminal,
        color_system="standard" if stderr_terminal else None,
        highlight=False,
        markup=False,
        width=100,
    )
    policy = OutputPolicy(
        settings=CliOutputSettings(detail=detail),
        stdout=_capabilities(terminal=stdout_terminal),
        stderr=_capabilities(terminal=stderr_terminal),
        context=OutputContextKind.ONE_SHOT,
    )
    selected_clock = clock if clock is not None else time.monotonic
    return (
        HostWorkflowDisplay(
            title=title,
            stderr=console,
            policy=policy,
            clock=selected_clock,
        ),
        stderr,
    )


class _FakeLive:
    instances: ClassVar[list[_FakeLive]] = []
    start_error: ClassVar[BaseException | None] = None
    update_error: ClassVar[BaseException | None] = None
    stop_error: ClassVar[BaseException | None] = None

    def __init__(self, renderable: object, **options: object) -> None:
        self.renderable = renderable
        self.options = options
        self.started = 0
        self.stopped = 0
        self.updates: list[object] = []
        type(self).instances.append(self)

    def start(self, *, refresh: bool) -> None:
        assert refresh is True
        self.started += 1
        if type(self).start_error is not None:
            raise type(self).start_error

    def update(self, renderable: object, *, refresh: bool) -> None:
        assert refresh is True
        self.renderable = renderable
        self.updates.append(renderable)
        if type(self).update_error is not None:
            raise type(self).update_error

    def stop(self) -> None:
        self.stopped += 1
        if type(self).stop_error is not None:
            raise type(self).stop_error


@pytest.fixture(autouse=True)
def _reset_fake_live() -> None:
    _FakeLive.instances = []
    _FakeLive.start_error = None
    _FakeLive.update_error = None
    _FakeLive.stop_error = None


def test_host_phase_events_are_immutable() -> None:
    event = HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION)

    with pytest.raises(FrozenInstanceError):
        event.phase = HostPhase.BUILD_INPUT_RESOLUTION  # type: ignore[misc]


def test_verbose_subphase_is_safe_bounded_and_summary_is_immutable() -> None:
    times = iter((10.0, 10.5, 11.0, 12.0))
    display, stderr = _display(
        detail=OutputDetail.VERBOSE,
        clock=lambda: next(times),
    )

    display.emit(HostPhaseStarted(HostPhase.BUILD_INPUT_RESOLUTION))
    display.emit(HostSubphaseStarted(HostSubphase.CANONICAL_WHEEL_PREPARATION))
    display.emit(HostSubphaseCompleted(HostSubphase.CANONICAL_WHEEL_PREPARATION))
    display.emit(HostPhaseCompleted(HostPhase.BUILD_INPUT_RESOLUTION))

    output = stderr.getvalue()
    assert output.count("Preparing the canonical cdh wheel") == 2
    assert "In progress" in output and "Completed" in output
    summary = display.completed_summary
    assert summary.phases[0].phase is HostPhase.BUILD_INPUT_RESOLUTION
    with pytest.raises(FrozenInstanceError):
        summary.phases = ()  # type: ignore[misc]


def test_plain_output_is_append_only_control_free_and_avoids_duplicate_history() -> (
    None
):
    display, stderr = _display(stdout_terminal=True, stderr_terminal=False)

    display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))
    display.emit(HostPhaseCompleted(HostPhase.CONFIGURATION_VALIDATION))
    display.emit(HostPhaseStarted(HostPhase.BUILD_INPUT_RESOLUTION))
    before_failure = stderr.getvalue()
    display.finish(HostPhaseFailed(HostPhase.BUILD_INPUT_RESOLUTION))

    output = stderr.getvalue()
    assert output.startswith("In progress: Validating configuration\n")
    assert "Completed: Validating configuration\n" not in output
    assert "In progress: Resolving build inputs\n" in output
    assert output[len(before_failure) :] == "Failed: Resolving build inputs\n"
    assert stderr.flushes == len(output.splitlines())
    assert "\x1b" not in output
    assert "\r" not in output


def test_only_stderr_capability_enables_live_and_live_redirects_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(display_module, "Live", _FakeLive)
    plain, plain_stderr = _display(stdout_terminal=True, stderr_terminal=False)

    plain.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))
    plain.finish(HostWorkflowSucceeded(), primary_error=ValueError("irrelevant"))

    assert _FakeLive.instances == []
    assert "In progress: Validating configuration" in plain_stderr.getvalue()

    interactive, stderr = _display(stdout_terminal=False, stderr_terminal=True)
    interactive.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))
    interactive.emit(HostPhaseCompleted(HostPhase.CONFIGURATION_VALIDATION))
    interactive.finish(HostWorkflowSucceeded())
    interactive.finish(HostWorkflowSucceeded())

    assert stderr.getvalue() == ""
    assert len(_FakeLive.instances) == 1
    live = _FakeLive.instances[0]
    assert live.options["transient"] is True
    assert live.options["redirect_stdout"] is False
    assert live.options["redirect_stderr"] is False
    assert live.started == 1
    assert live.stopped == 1


def test_external_stream_seal_stops_live_and_rejects_later_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(display_module, "Live", _FakeLive)
    display, stderr = _display(stderr_terminal=True)

    display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))
    display.emit(HostPhaseCompleted(HostPhase.CONFIGURATION_VALIDATION))
    display.seal_for_external_stream()
    display.seal_for_external_stream()

    assert stderr.getvalue() == ""
    assert _FakeLive.instances[0].stopped == 1
    with pytest.raises(RuntimeError, match="after display teardown"):
        display.emit(HostPhaseStarted(HostPhase.BUILD_INPUT_RESOLUTION))


def test_tty_failure_clears_live_then_renders_completed_and_current_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(display_module, "Live", _FakeLive)
    display, stderr = _display(stderr_terminal=True)

    display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))
    display.emit(HostPhaseCompleted(HostPhase.CONFIGURATION_VALIDATION))
    display.emit(HostPhaseStarted(HostPhase.BUILD_INPUT_RESOLUTION))
    display.finish(HostPhaseFailed(HostPhase.BUILD_INPUT_RESOLUTION))

    output = _strip_ansi(stderr.getvalue())
    assert output.count("Validating configuration") == 1
    assert output.count("Resolving build inputs") == 1
    assert "Completed" in output
    assert "Failed" in output
    assert _FakeLive.instances[0].stopped == 1


@pytest.mark.parametrize(
    ("terminal_event", "expected"),
    [
        (HostPhaseFailed(HostPhase.BUILD_INPUT_RESOLUTION), "Failed"),
        (HostPhaseInterrupted(HostPhase.BUILD_INPUT_RESOLUTION), "Interrupted"),
    ],
)
def test_quiet_tty_terminal_output_contains_only_the_current_phase(
    monkeypatch: pytest.MonkeyPatch,
    terminal_event: HostPhaseFailed | HostPhaseInterrupted,
    expected: str,
) -> None:
    monkeypatch.setattr(display_module, "Live", _FakeLive)
    display, stderr = _display(
        detail=OutputDetail.QUIET,
        stderr_terminal=True,
    )

    display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))
    display.emit(HostPhaseCompleted(HostPhase.CONFIGURATION_VALIDATION))
    display.emit(HostPhaseStarted(HostPhase.BUILD_INPUT_RESOLUTION))
    display.finish(terminal_event)

    output = _strip_ansi(stderr.getvalue())
    assert expected in output
    assert "Resolving build inputs" in output
    assert "Validating configuration" not in output
    assert _FakeLive.instances == []


def test_verbose_plain_completion_includes_major_phase_duration() -> None:
    times = iter((10.0, 12.5))
    display, stderr = _display(
        detail=OutputDetail.VERBOSE,
        clock=lambda: next(times),
    )

    display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))
    display.emit(HostPhaseCompleted(HostPhase.CONFIGURATION_VALIDATION))
    display.finish(HostWorkflowSucceeded())

    output = stderr.getvalue()
    assert "Completed:" in output
    assert "configuration" in output.lower()
    assert "s)" in output


def test_live_teardown_failure_does_not_replace_the_primary_workflow_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(display_module, "Live", _FakeLive)
    _FakeLive.update_error = RuntimeError("terminal update failed")
    _FakeLive.stop_error = OSError("terminal teardown failed")
    display, stderr = _display(stderr_terminal=True)
    primary_error = ValueError("workflow failed")
    display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))

    display.finish(
        HostPhaseFailed(HostPhase.CONFIGURATION_VALIDATION),
        primary_error=primary_error,
    )
    display.finish(
        HostPhaseFailed(HostPhase.CONFIGURATION_VALIDATION),
        primary_error=primary_error,
    )

    assert _FakeLive.instances[0].stopped == 1
    assert "Failed: Validating configuration" in stderr.getvalue()


def test_live_start_failure_stops_and_preserves_the_start_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(display_module, "Live", _FakeLive)
    start_error = KeyboardInterrupt("terminal start interrupted")
    _FakeLive.start_error = start_error
    _FakeLive.stop_error = OSError("terminal teardown failed")
    display, _ = _display(stderr_terminal=True)

    with pytest.raises(KeyboardInterrupt) as raised:
        display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))

    assert raised.value is start_error
    assert _FakeLive.instances[0].stopped == 1
    assert display._live is None
    display.terminate_for_error(ValueError("workflow failed"))
    assert _FakeLive.instances[0].stopped == 1


def test_terminal_update_failure_still_stops_and_remains_the_first_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(display_module, "Live", _FakeLive)
    update_error = RuntimeError("terminal update failed")
    _FakeLive.update_error = update_error
    _FakeLive.stop_error = OSError("terminal teardown failed")
    display, stderr = _display(stderr_terminal=True)
    display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))

    with pytest.raises(RuntimeError) as raised:
        display.finish(HostPhaseInterrupted(HostPhase.CONFIGURATION_VALIDATION))

    assert raised.value is update_error
    assert _FakeLive.instances[0].stopped == 1
    assert display._live is None
    assert "Interrupted: Validating configuration" in stderr.getvalue()


def test_live_teardown_failure_remains_observable_without_a_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(display_module, "Live", _FakeLive)
    _FakeLive.stop_error = OSError("terminal teardown failed")
    display, stderr = _display(stderr_terminal=True)
    display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))

    with pytest.raises(OSError, match="terminal teardown failed"):
        display.finish(HostPhaseInterrupted(HostPhase.CONFIGURATION_VALIDATION))

    assert "Interrupted: Validating configuration" in stderr.getvalue()


def test_interactive_title_is_control_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display_module, "Live", _FakeLive)
    display, stderr = _display(
        title="Preparing\\path\nnext\x1b",
        stderr_terminal=True,
    )
    display.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))
    display.finish(HostPhaseFailed(HostPhase.CONFIGURATION_VALIDATION))

    output = _strip_ansi(stderr.getvalue())
    assert r"Preparing\\path\nnext\x1b" in output
    assert "\x1b" not in output


def _strip_ansi(value: str) -> str:
    result = value
    while "\x1b[" in result:
        prefix, suffix = result.split("\x1b[", maxsplit=1)
        index = 0
        while index < len(suffix) and not suffix[index].isalpha():
            index += 1
        result = prefix + suffix[index + 1 :]
    return result
