"""Process-control spawn, escalation, and reap contract tests."""

from __future__ import annotations

import signal
from collections.abc import Mapping, Sequence

import pytest

from comfyui_docker_helper.container.process_control import (
    ProcessGroupSignalError,
    ProcessStartError,
    reap_process_if_exited,
    signal_direct_process,
    start_direct_process,
    terminate_direct_process,
    terminate_process_group,
    wait_for_process_reap,
)


class FakeClock:
    """Deterministic monotonic clock for bounded process waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProcess:
    """Controllable direct/session-leader child with explicit reap evidence."""

    def __init__(self, *, pid: int = 4242, terminate_exits: bool = False) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminate_exits = terminate_exits
        self.signals: list[signal.Signals] = []
        self.terminates = 0
        self.kills = 0
        self.waits = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        self.waits += 1
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.terminates += 1
        if self.terminate_exits:
            self.returncode = -int(signal.SIGTERM)

    def kill(self) -> None:
        self.kills += 1
        self.returncode = -int(signal.SIGKILL)


# Spawn admission keeps argv/cwd/env exact and converts only expected OS start
# failures into concise process-boundary diagnostics.
def test_start_direct_process_preserves_inputs_and_maps_expected_errors() -> None:
    calls: list[tuple[tuple[str, ...], str, Mapping[str, str], bool]] = []
    process = FakeProcess()

    def starter(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeProcess:
        calls.append((tuple(argv), cwd, env, shell))
        return process

    assert (
        start_direct_process(
            ["/opt/venv/bin/python", "main.py"],
            cwd="/workspace/ComfyUI",
            env={"PATH": "/opt/venv/bin"},
            description="ComfyUI",
            starter=starter,
        )
        is process
    )
    assert calls == [
        (
            ("/opt/venv/bin/python", "main.py"),
            "/workspace/ComfyUI",
            {"PATH": "/opt/venv/bin"},
            False,
        )
    ]

    def missing(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        raise FileNotFoundError

    with pytest.raises(
        ProcessStartError,
        match=r"^ComfyUI executable not found: /missing/python$",
    ):
        start_direct_process(
            ["/missing/python"],
            cwd="/workspace/ComfyUI",
            env={},
            description="ComfyUI",
            starter=missing,
        )

    def denied(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        raise PermissionError("denied")

    with pytest.raises(
        ProcessStartError,
        match=r"^ComfyUI failed to start: denied$",
    ):
        start_direct_process(
            ["/opt/venv/bin/python"],
            cwd="/workspace/ComfyUI",
            env={},
            description="ComfyUI",
            starter=denied,
        )


# Direct children are reaped after cooperative termination and escalate exactly
# once when a forwarded signal is ignored.
def test_direct_process_wait_and_escalation_are_bounded_and_reaped() -> None:
    cooperative = FakeProcess(terminate_exits=True)
    assert (
        terminate_direct_process(
            cooperative,
            terminate_timeout=0.2,
            kill_timeout=0.2,
            poll_interval=0.1,
        )
        is True
    )
    assert cooperative.terminates == 1
    assert cooperative.kills == 0
    assert cooperative.waits == 1

    clock = FakeClock()
    ignored = FakeProcess()
    assert signal_direct_process(
        ignored,
        signal.SIGINT,
        signal_timeout=0.2,
        kill_timeout=0.2,
        poll_interval=0.1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ) == -int(signal.SIGKILL)
    assert ignored.signals == [signal.SIGINT]
    assert ignored.kills == 1
    assert ignored.waits == 1
    assert clock.now == pytest.approx(0.2)


# Session-leader ownership targets the recorded group once, then reaps the
# direct child after TERM-to-KILL escalation and preserves signal errors.
def test_process_group_escalation_and_signal_failure_are_typed() -> None:
    clock = FakeClock()
    process = FakeProcess(pid=5151)
    signals: list[tuple[int, signal.Signals]] = []

    def signaler(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            process.returncode = -int(signal.SIGKILL)

    assert (
        terminate_process_group(
            process,
            termination_grace=0.2,
            kill_grace=0.2,
            poll_interval=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            signaler=signaler,
        )
        is True
    )
    assert signals == [(5151, signal.SIGTERM), (5151, signal.SIGKILL)]
    assert process.waits == 1

    cooperative_clock = FakeClock()
    cooperative = FakeProcess(pid=5252)
    cooperative_signals: list[tuple[int, signal.Signals]] = []

    def cooperative_signaler(pid: int, sig: signal.Signals) -> None:
        cooperative_signals.append((pid, sig))

    def cooperative_sleep(seconds: float) -> None:
        cooperative_clock.sleep(seconds)
        cooperative.returncode = -int(signal.SIGTERM)

    assert (
        terminate_process_group(
            cooperative,
            termination_grace=0.2,
            kill_grace=0.2,
            poll_interval=0.1,
            monotonic=cooperative_clock.monotonic,
            sleep=cooperative_sleep,
            signaler=cooperative_signaler,
        )
        is True
    )
    assert cooperative_signals == [(5252, signal.SIGTERM)]
    assert cooperative.waits == 1

    failed = FakeProcess(pid=6161)

    def fail_signal(pid: int, sig: signal.Signals) -> None:
        del pid, sig
        raise PermissionError("not permitted")

    with pytest.raises(ProcessGroupSignalError) as error:
        terminate_process_group(
            failed,
            termination_grace=0.2,
            kill_grace=0.2,
            poll_interval=0.1,
            signaler=fail_signal,
        )
    assert error.value.sig == signal.SIGTERM
    assert isinstance(error.value.error, PermissionError)


# A caller-owned force request interrupts the TERM grace and applies SIGKILL
# without consuming either the remaining grace or a newly created wait budget.
def test_process_group_repeated_signal_interrupts_termination_grace() -> None:
    clock = FakeClock()
    process = FakeProcess(pid=5353)
    signals: list[tuple[int, signal.Signals]] = []
    force = False

    def signaler(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            process.returncode = -int(signal.SIGKILL)

    def sleep(seconds: float) -> None:
        nonlocal force
        clock.sleep(seconds)
        force = True

    assert (
        terminate_process_group(
            process,
            termination_grace=30.0,
            kill_grace=30.0,
            poll_interval=0.1,
            monotonic=clock.monotonic,
            sleep=sleep,
            signaler=signaler,
            force_requested=lambda: force,
        )
        is True
    )

    assert signals == [(5353, signal.SIGTERM), (5353, signal.SIGKILL)]
    assert clock.now == pytest.approx(0.1)
    assert process.waits == 1

    kill_clock = FakeClock()
    kill_wait_process = FakeProcess(pid=5454)
    kill_wait_signals: list[tuple[int, signal.Signals]] = []
    sleep_calls = 0

    def kill_wait_signaler(pid: int, sig: signal.Signals) -> None:
        kill_wait_signals.append((pid, sig))

    def kill_wait_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        kill_clock.sleep(seconds)
        sleep_calls += 1

    assert (
        terminate_process_group(
            kill_wait_process,
            termination_grace=0.1,
            kill_grace=30.0,
            poll_interval=0.1,
            monotonic=kill_clock.monotonic,
            sleep=kill_wait_sleep,
            signaler=kill_wait_signaler,
            force_requested=lambda: sleep_calls >= 2,
        )
        is False
    )
    assert kill_wait_signals == [
        (5454, signal.SIGTERM),
        (5454, signal.SIGKILL),
    ]
    assert kill_clock.now == pytest.approx(0.2)


# A terminal child is reaped once; an active child remains owned when its
# caller-supplied wait budget expires.
def test_wait_for_process_reap_distinguishes_exit_from_timeout() -> None:
    exited = FakeProcess()
    exited.returncode = 7
    assert wait_for_process_reap(exited, timeout=1.0, poll_interval=0.1) is True
    assert exited.waits == 1

    clock = FakeClock()
    running = FakeProcess()
    assert (
        wait_for_process_reap(
            running,
            timeout=0.2,
            poll_interval=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        is False
    )
    assert running.waits == 0
    assert clock.now == pytest.approx(0.2)

    class WaitErrorProcess(FakeProcess):
        def wait(self) -> int:
            raise OSError("wait failed")

    wait_error = WaitErrorProcess()
    wait_error.returncode = 9
    with pytest.raises(OSError, match="wait failed"):
        reap_process_if_exited(wait_error)
    assert (
        wait_for_process_reap(
            wait_error,
            timeout=0.0,
            poll_interval=0.1,
        )
        is True
    )
