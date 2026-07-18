"""Narrow process spawn, signaling, escalation, and reap operations."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import Protocol

type Monotonic = Callable[[], float]
type Sleep = Callable[[float], object]
type ProcessGroupSignaler = Callable[[int, signal.Signals], object]


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

    def __init__(self, sig: signal.Signals, error: OSError) -> None:
        self.sig = sig
        self.error = error
        super().__init__(f"{sig.name}: {error}")


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
        raise ProcessStartError(
            f"{description} executable not found: {argv[0]}"
        ) from error
    except OSError as error:
        raise ProcessStartError(f"{description} failed to start: {error}") from error


def reap_process_if_exited(process: WaitableProcess) -> int | None:
    """Reap an exited child and return its result, or return None if running."""
    if process.poll() is None:
        return None
    return process.wait()


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


def signal_direct_process(
    process: DirectProcess,
    sig: signal.Signals,
    *,
    signal_timeout: float,
    kill_timeout: float,
    poll_interval: float,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
) -> int:
    """Forward one signal, then kill/reap an unresponsive direct child."""
    if process.poll() is None:
        process.send_signal(sig)
    if wait_for_process_reap(
        process,
        timeout=signal_timeout,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    ):
        assert process.returncode is not None
        return process.returncode
    with suppress(OSError):
        process.kill()
    if wait_for_process_reap(
        process,
        timeout=kill_timeout,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    ):
        assert process.returncode is not None
        return process.returncode
    return -int(signal.SIGKILL)


def terminate_process_group(
    process: SessionLeaderProcess,
    *,
    termination_grace: float,
    kill_grace: float,
    poll_interval: float,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
    signaler: ProcessGroupSignaler | None = None,
) -> bool:
    """Terminate the session leader's group, escalating and reaping its child."""
    if reap_process_if_exited(process) is not None:
        return True
    send = signal_process_group if signaler is None else signaler
    if not _send_process_group_signal(process, signal.SIGTERM, send):
        return reap_process_if_exited(process) is not None
    if _wait_for_process_group_reap(
        process,
        timeout=termination_grace,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    ):
        return True
    _send_process_group_signal(process, signal.SIGKILL, send)
    return _wait_for_process_group_reap(
        process,
        timeout=kill_grace,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    )


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
        raise ProcessGroupSignalError(sig, error) from error
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


def _wait_for_process_group_reap(
    process: SessionLeaderProcess,
    *,
    timeout: float,
    poll_interval: float,
    monotonic: Monotonic,
    sleep: Sleep,
) -> bool:
    deadline = monotonic() + timeout
    while True:
        if reap_process_if_exited(process) is not None:
            return True
        now = monotonic()
        if now >= deadline:
            return False
        sleep(min(poll_interval, deadline - now))
