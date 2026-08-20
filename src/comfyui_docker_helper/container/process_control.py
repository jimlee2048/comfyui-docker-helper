"""Narrow process spawn, signaling, escalation, and reap operations."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

type Monotonic = Callable[[], float]
type Sleep = Callable[[float], object]
type ProcessGroupSignaler = Callable[[int, signal.Signals], object]
type ForceRequested = Callable[[], bool]


class WaitableProcess(Protocol):
    """A child process whose terminal result can be polled and reaped."""

    returncode: int | None

    def wait(self) -> int: ...

    def poll(self) -> int | None: ...


class DirectProcess(WaitableProcess, Protocol):
    """A directly controlled child process."""

    def send_signal(self, sig: signal.Signals) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class SessionLeaderProcess(WaitableProcess, Protocol):
    """A child that owns the process group created for its session."""

    pid: int


class DirectProcessStarter(Protocol):
    """Subprocess-compatible starter for one directly controlled child."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> DirectProcess: ...


class ProcessStartError(RuntimeError):
    """A child process could not be started."""


class ProcessGroupSignalError(RuntimeError):
    """A cdh-owned process group could not be signaled."""

    def __init__(self, sig: signal.Signals) -> None:
        self.sig = sig
        super().__init__(f"owned process group could not be signaled with {sig.name}")


@dataclass(frozen=True, slots=True)
class ProcessTerminalResult:
    """Observed child terminal status after a successful direct reap."""

    returncode: int


@dataclass(frozen=True, slots=True)
class ProcessTerminationResult:
    """Terminal evidence plus whether force escalation was required."""

    terminal: ProcessTerminalResult
    forced: bool


def start_direct_process(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    description: str,
    starter: DirectProcessStarter = subprocess.Popen,
) -> DirectProcess:
    """Start one direct child without shell expansion."""
    try:
        return starter(argv, cwd=cwd, env=env, shell=False)
    except FileNotFoundError as error:
        raise ProcessStartError(f"{description} executable was not found") from error
    except OSError as error:
        raise ProcessStartError(f"{description} failed to start") from error


def reap_process_if_exited(process: WaitableProcess) -> int | None:
    """Reap an exited child and return its result, or return None if running."""
    terminal = reap_process_terminal(process)
    return None if terminal is None else terminal.returncode


def reap_process_terminal(process: WaitableProcess) -> ProcessTerminalResult | None:
    """Return strict terminal/reap evidence, or None while the child is live."""
    if process.poll() is None:
        return None
    return ProcessTerminalResult(returncode=process.wait())


def wait_for_process_reap(
    process: WaitableProcess,
    *,
    timeout: float,
    poll_interval: float,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
) -> bool:
    """Wait at most one caller-owned interval for a child to exit and be reaped."""
    deadline = monotonic() + timeout
    while True:
        if _reap_process_if_exited_best_effort(process) is not None:
            return True
        now = monotonic()
        if now >= deadline:
            return False
        sleep(min(poll_interval, deadline - now))


def terminate_direct_process(
    process: DirectProcess,
    *,
    terminate_timeout: float,
    kill_timeout: float,
    poll_interval: float,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
) -> bool:
    """Terminate a direct child, then kill and reap it if it remains alive."""
    if _reap_process_if_exited_best_effort(process) is not None:
        return True
    with suppress(OSError):
        process.terminate()
    if wait_for_process_reap(
        process,
        timeout=terminate_timeout,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    ):
        return True
    with suppress(OSError):
        process.kill()
    return wait_for_process_reap(
        process,
        timeout=kill_timeout,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    )


def request_terminate_direct_process(process: DirectProcess) -> bool:
    """Send the cooperative termination signal to one running direct child."""
    if process.poll() is not None:
        return False
    try:
        process.terminate()
    except ProcessLookupError:
        return False
    return True


def send_direct_process_signal(
    process: DirectProcess,
    sig: signal.Signals,
) -> bool:
    """Signal one still-running direct child without waiting for it."""
    if process.poll() is not None:
        return False
    try:
        process.send_signal(sig)
    except ProcessLookupError:
        return False
    return True


def request_force_direct_process(process: DirectProcess) -> bool:
    """Send SIGKILL to one running direct child without waiting."""
    if process.poll() is not None:
        return False
    try:
        process.kill()
    except ProcessLookupError:
        return False
    return True


def terminate_direct_process_until(
    process: DirectProcess,
    *,
    deadline: float,
    poll_interval: float,
    force_requested: ForceRequested = lambda: False,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
) -> ProcessTerminationResult:
    """Terminate one child against one caller-owned absolute boundary."""
    terminal = reap_process_terminal(process)
    if terminal is not None:
        return ProcessTerminationResult(terminal=terminal, forced=False)
    forced = force_requested()
    if forced:
        request_force_direct_process(process)
    else:
        request_terminate_direct_process(process)
    while True:
        terminal = reap_process_terminal(process)
        if terminal is not None:
            return ProcessTerminationResult(terminal=terminal, forced=forced)
        now = monotonic()
        if not forced and (force_requested() or now >= deadline):
            request_force_direct_process(process)
            forced = True
            terminal = reap_process_terminal(process)
            if terminal is not None:
                return ProcessTerminationResult(terminal=terminal, forced=True)
        delay = poll_interval if forced else min(poll_interval, deadline - now)
        sleep(max(0.0, delay))


def request_force_process_group(
    process: SessionLeaderProcess,
    *,
    signaler: ProcessGroupSignaler | None = None,
) -> bool:
    """Send SIGKILL to one recorded owned process group without waiting."""
    send = signal_process_group if signaler is None else signaler
    return _send_process_group_signal(process, signal.SIGKILL, send)


def terminate_process_group_until(
    process: SessionLeaderProcess,
    *,
    deadline: float,
    poll_interval: float,
    signaler: ProcessGroupSignaler | None = None,
    force_requested: ForceRequested = lambda: False,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
) -> ProcessTerminationResult:
    """Terminate one recorded owned group after its caller fixes cancellation."""
    send = signal_process_group if signaler is None else signaler
    forced = force_requested()
    _send_process_group_signal(
        process,
        signal.SIGKILL if forced else signal.SIGTERM,
        send,
    )
    while True:
        terminal = reap_process_terminal(process)
        if terminal is not None:
            return ProcessTerminationResult(terminal=terminal, forced=forced)
        now = monotonic()
        if not forced and (force_requested() or now >= deadline):
            _send_process_group_signal(process, signal.SIGKILL, send)
            forced = True
            terminal = reap_process_terminal(process)
            if terminal is not None:
                return ProcessTerminationResult(terminal=terminal, forced=True)
        delay = poll_interval if forced else min(poll_interval, deadline - now)
        sleep(max(0.0, delay))


def signal_process_group(group_leader_pid: int, sig: signal.Signals) -> None:
    """Signal the process group owned by one new-session leader."""
    os.killpg(group_leader_pid, sig)


def _send_process_group_signal(
    process: SessionLeaderProcess,
    sig: signal.Signals,
    signaler: ProcessGroupSignaler,
) -> bool:
    try:
        signaler(process.pid, sig)
    except ProcessLookupError:
        return False
    except OSError as error:
        raise ProcessGroupSignalError(sig) from error
    return True


def _reap_process_if_exited_best_effort(
    process: WaitableProcess,
) -> int | None:
    observed = process.poll()
    if observed is None:
        return None
    try:
        return process.wait()
    except OSError:
        return observed
