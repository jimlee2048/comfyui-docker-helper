"""Ordered container runtime lifecycle orchestration."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import Protocol

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container.process_control import (
    DirectProcess,
    DirectProcessStarter,
    ProcessStartError,
    SessionLeaderProcess,
    reap_process_if_exited,
    signal_process_group,
    start_direct_process,
    terminate_direct_process,
)
from comfyui_docker_helper.container.readiness import (
    ReadinessError,
    wait_for_comfyui_readiness,
)
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_diagnostics import (
    format_runtime_diagnostics,
    render_runtime_diagnostics,
)
from comfyui_docker_helper.container.runtime_downloads import (
    RuntimeAsyncQueueStartupError,
    RuntimeDownloads,
)
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeFileDownloadError,
    RuntimeFilePlanError,
)
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    RuntimeHookPlan,
    RuntimeHookResult,
    run_runtime_startup_hooks,
    run_runtime_stop_hooks,
)
from comfyui_docker_helper.container.runtime_ssh_service import (
    RuntimeSshService,
    RuntimeSshServiceError,
)
from comfyui_docker_helper.container.runtime_state import RuntimeStateError
from comfyui_docker_helper.errors import ApplicationError

CHILD_TERMINATION_REAP_GRACE_SECONDS = 2.0
CHILD_REAP_POLL_INTERVAL_SECONDS = 0.1
COMFYUI_SHUTDOWN_RESERVE_SECONDS = 2.0
AUXILIARY_SHUTDOWN_BOUND_SECONDS = 5.0


class EntrypointError(ApplicationError):
    """A user-facing container entrypoint failure."""


class RuntimeHookRunner(Protocol):
    """Run one startup hook phase."""

    def __call__(
        self,
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]: ...


class RuntimeStopHookRunner(Protocol):
    """Run stop hooks with cooperative cancellation."""

    def __call__(
        self,
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
        deadline: float | None,
        monotonic: Callable[[], float],
        sleep: Callable[[float], object],
    ) -> tuple[RuntimeHookResult, ...]: ...


class ReadinessWaiter(Protocol):
    """Wait for ComfyUI before post-start hooks."""

    def __call__(self, port: int, *, child: DirectProcess) -> object: ...


def run_runtime_lifecycle(
    config: RuntimeConfig,
    hook_plan: RuntimeHookPlan,
    *,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    runner: DirectProcessStarter,
    runtime_hook_runner: RuntimeHookRunner = run_runtime_startup_hooks,
    runtime_stop_hook_runner: RuntimeStopHookRunner = run_runtime_stop_hooks,
    readiness_waiter: ReadinessWaiter = wait_for_comfyui_readiness,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> int:
    """Run the fixed cdh startup, child, and cleanup phase order."""
    startup_shutdown = _StartupShutdownState(
        shutdown_timeout=config.cdh.shutdown_timeout,
        monotonic=monotonic,
    )
    with _startup_shutdown_signal_handlers(startup_shutdown):

        def stop_startup_auxiliary_services() -> None:
            previous_raise_on_signal = startup_shutdown.raise_on_signal
            startup_shutdown.raise_on_signal = False
            try:
                downloads.stop(
                    cancel_requested=startup_shutdown.repeated_signal_requested
                )
                ssh_service.stop(
                    cancel_requested=startup_shutdown.repeated_signal_requested
                )
            finally:
                startup_shutdown.raise_on_signal = previous_raise_on_signal

        def finish_startup_signal_shutdown(
            child: DirectProcess | None = None,
        ) -> int:
            previous_raise_on_signal = startup_shutdown.raise_on_signal
            startup_shutdown.raise_on_signal = False
            try:
                return _finish_startup_signal_shutdown(
                    startup_shutdown,
                    child=child,
                    hook_plan=hook_plan,
                    runtime=runtime,
                    source_env=source_env,
                    runtime_stop_hook_runner=runtime_stop_hook_runner,
                    downloads=downloads,
                    ssh_service=ssh_service,
                    monotonic=monotonic,
                    sleep=sleep,
                )
            finally:
                startup_shutdown.raise_on_signal = previous_raise_on_signal

        try:
            try:
                downloads.activate()
            except RuntimeFilePlanError as error:
                raise EntrypointError(
                    format_runtime_diagnostics(
                        "runtime file configuration is invalid", error.diagnostics
                    )
                ) from error
            except RuntimeStateError as error:
                raise EntrypointError(f"runtime state failed: {error}") from error
            except RuntimeFileDownloadError as error:
                raise EntrypointError(
                    format_runtime_diagnostics(
                        "runtime download failed", error.diagnostics
                    )
                ) from error
            except ApplicationError as error:
                raise EntrypointError(f"runtime download failed: {error}") from error

            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown()
            _run_pre_start_hooks(
                hook_plan,
                runtime=runtime,
                source_env=source_env,
                runtime_hook_runner=runtime_hook_runner,
                startup_shutdown=startup_shutdown,
            )
            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown()
            previous_raise_on_signal = startup_shutdown.raise_on_signal
            startup_shutdown.raise_on_signal = False
            try:
                ssh_service.start()
            except RuntimeSshServiceError as error:
                raise EntrypointError(str(error)) from error
            finally:
                startup_shutdown.raise_on_signal = previous_raise_on_signal
            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown()
            try:
                ssh_service.ensure_running_before_comfyui()
            except RuntimeSshServiceError as error:
                raise EntrypointError(str(error)) from error
            try:
                downloads.start_async()
            except RuntimeAsyncQueueStartupError as error:
                stop_startup_auxiliary_services()
                raise EntrypointError(
                    f"async runtime download queue failed to start: {error}"
                ) from error
            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown()
        except _StartupShutdownRequested as request:
            del request
            return finish_startup_signal_shutdown()

        completed: DirectProcess | None = None
        try:
            startup_shutdown.raise_on_signal = False
            try:
                if startup_shutdown.requested_signal is not None:
                    return finish_startup_signal_shutdown()
                try:
                    ssh_service.ensure_running_before_comfyui()
                except RuntimeSshServiceError as error:
                    stop_startup_auxiliary_services()
                    raise EntrypointError(str(error)) from error
                argv = build_comfyui_argv(runtime=runtime, config=config)
                if startup_shutdown.requested_signal is not None:
                    return finish_startup_signal_shutdown()
                try:
                    completed = start_direct_process(
                        argv,
                        cwd=os.fspath(runtime.comfyui_path),
                        env=runtime.env(source_env),
                        description="ComfyUI",
                        starter=runner,
                    )
                except ProcessStartError as error:
                    if startup_shutdown.requested_signal is not None:
                        return finish_startup_signal_shutdown()
                    stop_startup_auxiliary_services()
                    raise EntrypointError(str(error)) from error
                if startup_shutdown.requested_signal is not None:
                    return finish_startup_signal_shutdown(completed)
                ssh_service.monitor_after_comfyui_start()
            finally:
                startup_shutdown.raise_on_signal = True

            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown(completed)
            try:
                _wait_for_readiness_if_required(
                    hook_plan,
                    config=config,
                    child=completed,
                    readiness_waiter=readiness_waiter,
                )
                _run_post_start_hooks_if_required(
                    hook_plan,
                    runtime=runtime,
                    source_env=source_env,
                    child=completed,
                    runtime_hook_runner=runtime_hook_runner,
                    startup_shutdown=startup_shutdown,
                )
            except EntrypointError:
                stop_startup_auxiliary_services()
                raise
            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown(completed)
        except _StartupShutdownRequested as request:
            del request
            if completed is None:
                return finish_startup_signal_shutdown()
            return finish_startup_signal_shutdown(completed)

    assert completed is not None
    return _wait_with_signal_forwarding(
        completed,
        hook_plan=hook_plan,
        runtime=runtime,
        source_env=source_env,
        runtime_stop_hook_runner=runtime_stop_hook_runner,
        downloads=downloads,
        ssh_service=ssh_service,
        shutdown_timeout=config.cdh.shutdown_timeout,
        monotonic=monotonic,
        sleep=sleep,
    )


def build_comfyui_argv(
    *,
    runtime: ContainerRuntime,
    config: RuntimeConfig,
) -> list[str]:
    """Build the final ComfyUI argv from effective runtime config."""
    comfyui = config.comfyui
    return [
        os.fspath(runtime.python),
        os.fspath(runtime.comfyui_path / "main.py"),
        "--listen",
        comfyui.listen,
        "--port",
        str(comfyui.port),
        "--disable-auto-launch",
        *comfyui.extra_args,
    ]


def _run_pre_start_hooks(
    hook_plan: RuntimeHookPlan,
    *,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_hook_runner: RuntimeHookRunner,
    startup_shutdown: _StartupShutdownState,
) -> None:
    if not hook_plan.for_phase("pre-start"):
        return
    startup_shutdown.raise_on_signal = False
    try:
        runtime_hook_runner(
            hook_plan,
            "pre-start",
            runtime=runtime,
            env=source_env,
            log=print,
            cancel_requested=startup_shutdown,
        )
    except RuntimeHookError as error:
        if startup_shutdown.requested_signal is not None:
            startup_shutdown.active_hook_process = error.active_process
            raise _StartupShutdownRequested(
                startup_shutdown.requested_signal
            ) from error
        raise EntrypointError(
            format_runtime_diagnostics("runtime hook failed", error.diagnostics)
        ) from error
    finally:
        startup_shutdown.raise_on_signal = True


def _wait_for_readiness_if_required(
    hook_plan: RuntimeHookPlan,
    *,
    config: RuntimeConfig,
    child: DirectProcess,
    readiness_waiter: ReadinessWaiter,
) -> None:
    if not hook_plan.for_phase("post-start"):
        return
    try:
        readiness_waiter(config.comfyui.port, child=child)
    except ReadinessError as error:
        terminate_direct_process(
            child,
            terminate_timeout=CHILD_TERMINATION_REAP_GRACE_SECONDS,
            kill_timeout=CHILD_TERMINATION_REAP_GRACE_SECONDS,
            poll_interval=CHILD_REAP_POLL_INTERVAL_SECONDS,
        )
        raise EntrypointError(
            format_runtime_diagnostics("ComfyUI readiness failed", error.diagnostics)
        ) from error


def _run_post_start_hooks_if_required(
    hook_plan: RuntimeHookPlan,
    *,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    child: DirectProcess,
    runtime_hook_runner: RuntimeHookRunner,
    startup_shutdown: _StartupShutdownState,
) -> None:
    if not hook_plan.for_phase("post-start"):
        return
    startup_shutdown.raise_on_signal = False
    try:
        runtime_hook_runner(
            hook_plan,
            "post-start",
            runtime=runtime,
            env=source_env,
            log=print,
            cancel_requested=startup_shutdown,
        )
    except RuntimeHookError as error:
        if startup_shutdown.requested_signal is not None:
            startup_shutdown.active_hook_process = error.active_process
            raise _StartupShutdownRequested(
                startup_shutdown.requested_signal
            ) from error
        terminate_direct_process(
            child,
            terminate_timeout=CHILD_TERMINATION_REAP_GRACE_SECONDS,
            kill_timeout=CHILD_TERMINATION_REAP_GRACE_SECONDS,
            poll_interval=CHILD_REAP_POLL_INTERVAL_SECONDS,
        )
        raise EntrypointError(
            format_runtime_diagnostics("runtime hook failed", error.diagnostics)
        ) from error
    finally:
        startup_shutdown.raise_on_signal = True


class _StartupShutdownRequested(BaseException):
    def __init__(self, sig: signal.Signals) -> None:
        self.sig = sig
        super().__init__(sig.name)


@dataclass(frozen=True, slots=True)
class _ShutdownTimeline:
    """One monotonic authority shared by every signal-shutdown participant."""

    deadline: float | None
    pre_stop_deadline: float | None
    auxiliary_deadline: float

    @classmethod
    def start(
        cls,
        shutdown_timeout: int | float,
        *,
        now: float,
    ) -> _ShutdownTimeline:
        deadline = None if shutdown_timeout == -1 else now + float(shutdown_timeout)
        return cls(
            deadline=deadline,
            pre_stop_deadline=(
                None
                if deadline is None
                else max(now, deadline - COMFYUI_SHUTDOWN_RESERVE_SECONDS)
            ),
            auxiliary_deadline=(
                now + AUXILIARY_SHUTDOWN_BOUND_SECONDS
                if deadline is None
                else min(now + AUXILIARY_SHUTDOWN_BOUND_SECONDS, deadline)
            ),
        )


class _StartupShutdownState:
    """Signal state while startup work remains cancellable."""

    def __init__(
        self,
        *,
        shutdown_timeout: int | float,
        monotonic: Callable[[], float],
    ) -> None:
        self.requested_signal: signal.Signals | None = None
        self.repeated_signal = False
        self.raise_on_signal = True
        self.active_hook_process: SessionLeaderProcess | None = None
        self.timeline: _ShutdownTimeline | None = None
        self._shutdown_timeout = shutdown_timeout
        self._monotonic = monotonic
        self._cancel_event = threading.Event()
        self._repeated_event = threading.Event()

    def request_shutdown(self, sig: signal.Signals) -> None:
        if self.requested_signal is None:
            self.requested_signal = sig
            self.timeline = _ShutdownTimeline.start(
                self._shutdown_timeout,
                now=self._monotonic(),
            )
        else:
            self.repeated_signal = True
            self._repeated_event.set()
        self._cancel_event.set()
        if self.raise_on_signal:
            raise _StartupShutdownRequested(self.requested_signal)

    def cancel_requested(self) -> bool:
        return self.requested_signal is not None

    def __call__(self) -> bool:
        return self.cancel_requested()

    def repeated_signal_requested(self) -> bool:
        return self.repeated_signal

    def shutdown_deadline(self) -> float | None:
        timeline = self.timeline
        return None if timeline is None else timeline.pre_stop_deadline

    def wait(self, timeout: float) -> bool:
        return self._cancel_event.wait(timeout)

    def repeated_cancellation(self) -> _RepeatedStartupCancellation:
        return _RepeatedStartupCancellation(self)


@dataclass(frozen=True, slots=True)
class _RepeatedStartupCancellation:
    state: _StartupShutdownState

    def __call__(self) -> bool:
        return self.state.repeated_signal_requested()

    def wait(self, timeout: float) -> bool:
        return self.state._repeated_event.wait(timeout)


@contextmanager
def _startup_shutdown_signal_handlers(state: _StartupShutdownState):
    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
    }

    def request_shutdown(sig: signal.Signals, frame: FrameType | None) -> None:
        del frame
        state.request_shutdown(signal.Signals(sig))

    try:
        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        yield
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


def _finish_startup_signal_shutdown(
    state: _StartupShutdownState,
    *,
    child: DirectProcess | None,
    hook_plan: RuntimeHookPlan,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> int:
    sig = state.requested_signal
    timeline = state.timeline
    assert sig is not None
    assert timeline is not None
    return _finish_signal_shutdown(
        sig,
        timeline=timeline,
        child=child,
        hook_plan=hook_plan,
        runtime=runtime,
        source_env=source_env,
        runtime_stop_hook_runner=runtime_stop_hook_runner,
        stop_hooks_cancelled=state.repeated_cancellation(),
        hook_processes=(
            () if state.active_hook_process is None else (state.active_hook_process,)
        ),
        downloads=downloads,
        ssh_service=ssh_service,
        monotonic=monotonic,
        sleep=sleep,
    )


class _ShutdownRequested(Exception):
    def __init__(
        self,
        sig: signal.Signals,
        *,
        timeline: _ShutdownTimeline,
    ) -> None:
        self.sig = sig
        self.timeline = timeline
        super().__init__(sig.name)


class _ShutdownState:
    """Signal state for one first-signal shutdown timeline."""

    def __init__(self) -> None:
        self.requested = False
        self._stop_hooks_cancelled = False
        self._cancel_event = threading.Event()

    def cancel_stop_hooks(self) -> None:
        self._stop_hooks_cancelled = True
        self._cancel_event.set()

    def stop_hooks_cancelled(self) -> bool:
        return self._stop_hooks_cancelled

    def __call__(self) -> bool:
        return self.stop_hooks_cancelled()

    def wait(self, timeout: float) -> bool:
        return self._cancel_event.wait(timeout)


def _wait_with_signal_forwarding(
    child: DirectProcess,
    *,
    hook_plan: RuntimeHookPlan,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    shutdown_timeout: int | float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> int:
    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    shutdown_state = _ShutdownState()

    def forward(sig: signal.Signals, frame: object) -> None:
        del frame
        if shutdown_state.requested:
            shutdown_state.cancel_stop_hooks()
            return
        requested = signal.Signals(sig)
        if child.poll() is None:
            shutdown_state.requested = True
            raise _ShutdownRequested(
                requested,
                timeline=_ShutdownTimeline.start(
                    shutdown_timeout,
                    now=monotonic(),
                ),
            )

    try:
        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        try:
            exit_code = child.wait()
            downloads.stop(cancel_requested=shutdown_state.stop_hooks_cancelled)
            ssh_service.stop(cancel_requested=shutdown_state.stop_hooks_cancelled)
            return exit_code
        except _ShutdownRequested as request:
            return _finish_signal_shutdown(
                request.sig,
                timeline=request.timeline,
                child=child,
                hook_plan=hook_plan,
                runtime=runtime,
                source_env=source_env,
                runtime_stop_hook_runner=runtime_stop_hook_runner,
                stop_hooks_cancelled=shutdown_state,
                hook_processes=(),
                downloads=downloads,
                ssh_service=ssh_service,
                monotonic=monotonic,
                sleep=sleep,
            )
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


def _finish_signal_shutdown(
    sig: signal.Signals,
    *,
    timeline: _ShutdownTimeline,
    child: DirectProcess | None,
    hook_plan: RuntimeHookPlan,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    stop_hooks_cancelled: Callable[[], bool],
    hook_processes: tuple[SessionLeaderProcess, ...],
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> int:
    """Complete one first-signal shutdown against its sole absolute timeline."""
    downloads.request_stop(deadline=timeline.auxiliary_deadline)
    ssh_service.request_stop()
    if child is None:
        _wait_for_auxiliary_shutdown(
            downloads=downloads,
            ssh_service=ssh_service,
            deadline=timeline.deadline,
            auxiliary_deadline=timeline.auxiliary_deadline,
            hook_processes=hook_processes,
            monotonic=monotonic,
            sleep=sleep,
        )
        return -int(sig)

    if not stop_hooks_cancelled():
        active_hook = _run_stop_hooks_before_signal(
            hook_plan,
            runtime=runtime,
            source_env=source_env,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            cancel_requested=stop_hooks_cancelled,
            deadline=timeline.pre_stop_deadline,
            monotonic=monotonic,
            sleep=sleep,
        )
        if active_hook is not None:
            hook_processes = (*hook_processes, active_hook)
    if monotonic() >= timeline.auxiliary_deadline:
        if not downloads.is_stopped():
            downloads.force_stop()
        if not ssh_service.is_stopped():
            ssh_service.force_stop()
    if child.poll() is None:
        child.send_signal(sig)
    return _wait_for_managed_shutdown(
        child,
        downloads=downloads,
        ssh_service=ssh_service,
        deadline=timeline.deadline,
        auxiliary_deadline=timeline.auxiliary_deadline,
        hook_processes=hook_processes,
        monotonic=monotonic,
        sleep=sleep,
    )


def _run_stop_hooks_before_signal(
    hook_plan: RuntimeHookPlan,
    *,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    cancel_requested: Callable[[], bool],
    deadline: float | None,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> SessionLeaderProcess | None:
    if not hook_plan.for_phase("stop"):
        return None
    if deadline is not None and monotonic() >= deadline:
        return None
    try:
        runtime_stop_hook_runner(
            hook_plan,
            runtime=runtime,
            env=source_env,
            log=print,
            cancel_requested=cancel_requested,
            deadline=deadline,
            monotonic=monotonic,
            sleep=sleep,
        )
    except RuntimeHookError as error:
        render_runtime_diagnostics("Runtime stop hook failed:", error.diagnostics)
        return error.active_process
    return None


def _wait_for_auxiliary_shutdown(
    *,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    deadline: float | None,
    auxiliary_deadline: float,
    hook_processes: tuple[SessionLeaderProcess, ...],
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> None:
    """Bound startup-signal auxiliary cleanup without a second outer budget."""
    while True:
        downloads_stopped = downloads.is_stopped()
        ssh_stopped = ssh_service.is_stopped()
        hooks_stopped = _reap_hook_processes(hook_processes)
        if downloads_stopped and ssh_stopped and hooks_stopped:
            return
        now = monotonic()
        if now >= auxiliary_deadline or (deadline is not None and now >= deadline):
            if not downloads_stopped:
                downloads.force_stop()
            if not ssh_stopped:
                ssh_service.force_stop()
            _force_hook_processes(hook_processes)
            return
        boundary = auxiliary_deadline
        if deadline is not None:
            boundary = min(boundary, deadline)
        sleep(min(CHILD_REAP_POLL_INTERVAL_SECONDS, boundary - now))


def _wait_for_managed_shutdown(
    child: DirectProcess,
    *,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    deadline: float | None,
    auxiliary_deadline: float,
    hook_processes: tuple[SessionLeaderProcess, ...],
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> int:
    """Reap all managed work against one caller-owned absolute timeline."""
    auxiliary_bound_reached = False
    while True:
        child_result = reap_process_if_exited(child)
        downloads_stopped = downloads.is_stopped()
        ssh_stopped = ssh_service.is_stopped()
        hooks_stopped = _reap_hook_processes(hook_processes)
        if (
            child_result is not None
            and downloads_stopped
            and ssh_stopped
            and hooks_stopped
        ):
            return child_result

        now = monotonic()
        if not auxiliary_bound_reached and now >= auxiliary_deadline:
            if not downloads_stopped:
                downloads.force_stop()
            if not ssh_stopped:
                ssh_service.force_stop()
            _force_hook_processes(hook_processes)
            auxiliary_bound_reached = True
            downloads_stopped = downloads.is_stopped()
            ssh_stopped = ssh_service.is_stopped()
            hooks_stopped = _reap_hook_processes(hook_processes)
            now = monotonic()
            if (
                deadline is None
                and child_result is not None
                and not (downloads_stopped and ssh_stopped and hooks_stopped)
            ):
                return child_result

        if deadline is not None and now >= deadline:
            downloads.force_stop()
            ssh_service.force_stop()
            _force_hook_processes(hook_processes)
            if child.poll() is None:
                child.kill()
            result = reap_process_if_exited(child)
            return -int(signal.SIGKILL) if result is None else result

        next_boundary = (
            auxiliary_deadline
            if not auxiliary_bound_reached and now < auxiliary_deadline
            else None
        )
        if deadline is not None:
            next_boundary = (
                deadline if next_boundary is None else min(next_boundary, deadline)
            )
        delay = CHILD_REAP_POLL_INTERVAL_SECONDS
        if next_boundary is not None and now < next_boundary:
            delay = min(delay, next_boundary - now)
        sleep(delay)


def _reap_hook_processes(processes: tuple[SessionLeaderProcess, ...]) -> bool:
    stopped = True
    for process in processes:
        if reap_process_if_exited(process) is None:
            stopped = False
    return stopped


def _force_hook_processes(processes: tuple[SessionLeaderProcess, ...]) -> bool:
    stopped = True
    for process in processes:
        if reap_process_if_exited(process) is not None:
            continue
        try:
            signal_process_group(process.pid, signal.SIGKILL)
        except OSError:
            stopped = False
            continue
        if reap_process_if_exited(process) is None:
            stopped = False
    return stopped
