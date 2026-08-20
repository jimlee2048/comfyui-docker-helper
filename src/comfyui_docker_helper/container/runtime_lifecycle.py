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
from typing import Literal, Protocol, TypeVar

from comfyui_docker_helper.cli_output import EventSink
from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container.process_control import (
    DirectProcess,
    DirectProcessStarter,
    ProcessStartError,
    SessionLeaderProcess,
    reap_process_if_exited,
    request_force_direct_process,
    request_force_process_group,
    send_direct_process_signal,
    start_direct_process,
    terminate_direct_process_until,
    terminate_process_group_until,
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
from comfyui_docker_helper.container.runtime_event_delivery import (
    safe_runtime_event_sink,
)
from comfyui_docker_helper.container.runtime_events import (
    RuntimeEvent,
    RuntimeGenerationStopCause,
    RuntimeGenerationStopped,
    RuntimeGenerationStopping,
    RuntimePhase,
    RuntimePhaseCompleted,
    RuntimePhaseFailed,
    RuntimePhaseStarted,
    RuntimeSshOutcome,
    RuntimeSshStatus,
)
from comfyui_docker_helper.container.runtime_files import (
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
from comfyui_docker_helper.container.runtime_logging import (
    RUNTIME_LOGGING_UNAVAILABLE_MESSAGE,
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
_ResultT = TypeVar("_ResultT")


type RuntimeGenerationResultCause = Literal[
    RuntimeGenerationStopCause.NATURAL_EXIT,
    RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN,
    RuntimeGenerationStopCause.OPERATOR_RESTART,
    RuntimeGenerationStopCause.CONTROLLER_FAILURE,
]


@dataclass(frozen=True, slots=True)
class RuntimeGenerationResult:
    """Raw terminal result and cause for one runtime generation."""

    cause: RuntimeGenerationResultCause
    returncode: int


class RuntimeRestartAcceptor(Protocol):
    """Publish a level-triggered restart proposal to the lifecycle owner."""

    def accept_if_requested(self, *, accepted_at: float) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


class RuntimeHealthObserver(Protocol):
    """Expose one controller-fatal runtime failure without owning cleanup."""

    def runtime_failure_message(self) -> str | None: ...

    def wait(self, timeout: float) -> bool: ...


def _runtime_failure_message(observer: RuntimeHealthObserver | None) -> str | None:
    return None if observer is None else observer.runtime_failure_message()


class RuntimeExecutionError(ApplicationError):
    """A user-facing container runtime execution failure."""


class _RuntimeLifecycleEvents:
    """Own local phase pairing and exactly-once generation teardown facts."""

    def __init__(
        self,
        event_sink: EventSink[RuntimeEvent],
        generation: str | None,
    ) -> None:
        self._event_sink = safe_runtime_event_sink(event_sink)
        assert self._event_sink is not None
        self._generation = generation
        self._active_phase: RuntimePhase | None = None
        self._stop_cause: RuntimeGenerationStopCause | None = None
        self._stopped = False

    def start_phase(self, phase: RuntimePhase) -> None:
        if self._active_phase is not None:
            raise RuntimeError("Runtime lifecycle phase ownership overlapped")
        self._active_phase = phase
        self._emit(RuntimePhaseStarted(phase))

    def complete_phase(self, phase: RuntimePhase) -> None:
        if self._active_phase is not phase:
            raise RuntimeError("Runtime lifecycle phase completion was not paired")
        self._active_phase = None
        self._emit(RuntimePhaseCompleted(phase))

    def fail_active_phase(self) -> None:
        phase = self._active_phase
        if phase is None:
            return
        self._active_phase = None
        self._emit(RuntimePhaseFailed(phase))

    def begin_stopping(self, cause: RuntimeGenerationStopCause) -> None:
        self.fail_active_phase()
        if self._generation is None:
            return
        if self._stop_cause is None:
            self._stop_cause = cause
            self._emit(RuntimeGenerationStopping(self._generation))
        elif cause is RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN:
            self._stop_cause = cause

    def run_generation_cleanup(
        self,
        operation: Callable[[], _ResultT],
    ) -> _ResultT:
        if self._generation is None:
            return operation()
        self.start_phase(RuntimePhase.GENERATION_CLEANUP)
        try:
            result = operation()
        except BaseException:
            self.fail_active_phase()
            raise
        self.complete_phase(RuntimePhase.GENERATION_CLEANUP)
        return result

    def mark_stopped(self) -> None:
        if self._generation is None or self._stop_cause is None or self._stopped:
            return
        self._stopped = True
        self._emit(RuntimeGenerationStopped(self._generation, self._stop_cause))

    def emit(self, event: RuntimeEvent) -> None:
        self._emit(event)

    def _emit(self, event: RuntimeEvent) -> None:
        self._event_sink.emit(event)

    @property
    def event_sink(self) -> EventSink[RuntimeEvent]:
        return self._event_sink


class RuntimeHookRunner(Protocol):
    """Run one startup hook phase."""

    def __call__(
        self,
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        cancel_requested: Callable[[], bool],
        event_sink: EventSink[RuntimeEvent],
    ) -> tuple[RuntimeHookResult, ...]: ...


class RuntimeStopHookRunner(Protocol):
    """Run stop hooks with cooperative cancellation."""

    def __call__(
        self,
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        cancel_requested: Callable[[], bool],
        deadline: float | None,
        monotonic: Callable[[], float],
        sleep: Callable[[float], object],
        event_sink: EventSink[RuntimeEvent],
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
    restart_acceptor: RuntimeRestartAcceptor | None = None,
    runtime_health: RuntimeHealthObserver | None = None,
    runtime_started: Callable[[], object] = lambda: None,
    external_shutdown_observer: Callable[[signal.Signals], object] = lambda _sig: None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    event_sink: EventSink[RuntimeEvent],
    generation: str | None = None,
    runtime_ownership_claimed: Callable[[], object] = lambda: None,
) -> RuntimeGenerationResult:
    """Run the fixed cdh startup, child, and cleanup phase order."""
    startup_shutdown = _StartupShutdownState(
        shutdown_timeout=config.cdh.shutdown_timeout,
        monotonic=monotonic,
        external_shutdown_observer=external_shutdown_observer,
    )
    startup_cancellation = _RuntimeStartupCancellation(
        state=startup_shutdown,
        runtime_health=runtime_health,
    )
    lifecycle_events = _RuntimeLifecycleEvents(event_sink, generation)
    startup_shutdown.raise_on_signal = False
    with _startup_shutdown_signal_handlers(startup_shutdown):
        runtime_ownership_claimed()

        def finish_startup_signal_shutdown(
            child: DirectProcess | None = None,
        ) -> RuntimeGenerationResult:
            lifecycle_events.begin_stopping(
                RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN
            )
            previous_raise_on_signal = startup_shutdown.raise_on_signal
            startup_shutdown.raise_on_signal = False
            try:
                result = RuntimeGenerationResult(
                    cause=RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN,
                    returncode=_finish_startup_signal_shutdown(
                        startup_shutdown,
                        child=child,
                        hook_plan=hook_plan,
                        runtime=runtime,
                        source_env=source_env,
                        runtime_stop_hook_runner=runtime_stop_hook_runner,
                        downloads=downloads,
                        ssh_service=ssh_service,
                        lifecycle_events=lifecycle_events,
                        monotonic=monotonic,
                        sleep=sleep,
                    ),
                )
                return result
            finally:
                startup_shutdown.raise_on_signal = previous_raise_on_signal

        def cleanup_startup_failure(
            child: DirectProcess | None = None,
            *,
            cause: RuntimeGenerationStopCause = (
                RuntimeGenerationStopCause.STARTUP_FAILURE
            ),
        ) -> None:
            lifecycle_events.begin_stopping(cause)
            previous_raise_on_signal = startup_shutdown.raise_on_signal
            startup_shutdown.raise_on_signal = False
            if generation is not None:
                lifecycle_events.start_phase(RuntimePhase.GENERATION_CLEANUP)
            try:
                timeline = startup_shutdown.timeline or _ShutdownTimeline.start(
                    config.cdh.shutdown_timeout,
                    now=monotonic(),
                )

                def component_timeout() -> float:
                    if timeline.deadline is None:
                        return AUXILIARY_SHUTDOWN_BOUND_SECONDS
                    return min(
                        AUXILIARY_SHUTDOWN_BOUND_SECONDS,
                        max(0.0, timeline.deadline - monotonic()),
                    )

                downloads.stop(
                    cancel_requested=startup_shutdown.repeated_signal_requested,
                    timeout=component_timeout(),
                    monotonic=monotonic,
                    sleep=sleep,
                )
                ssh_service.stop(
                    cancel_requested=startup_shutdown.repeated_signal_requested,
                    timeout=component_timeout(),
                    monotonic=monotonic,
                    sleep=sleep,
                )
                auxiliary_deadline = monotonic() + component_timeout()
                _wait_for_auxiliary_shutdown(
                    downloads=downloads,
                    ssh_service=ssh_service,
                    deadline=timeline.deadline,
                    auxiliary_deadline=auxiliary_deadline,
                    hook_processes=(),
                    force_requested=startup_shutdown.repeated_signal_requested,
                    monotonic=monotonic,
                    sleep=sleep,
                )
                if child is not None:
                    child_deadline = monotonic() + component_timeout()
                    child_deadline = min(
                        child_deadline,
                        monotonic() + CHILD_TERMINATION_REAP_GRACE_SECONDS * 2,
                    )
                    terminate_direct_process_until(
                        child,
                        deadline=child_deadline,
                        poll_interval=CHILD_REAP_POLL_INTERVAL_SECONDS,
                        force_requested=startup_shutdown.repeated_signal_requested,
                        monotonic=monotonic,
                        sleep=sleep,
                    )
                active_hook = startup_shutdown.active_hook_process
                if active_hook is not None:
                    hook_deadline = (
                        monotonic() + AUXILIARY_SHUTDOWN_BOUND_SECONDS
                        if timeline.deadline is None
                        else timeline.deadline
                    )
                    terminate_process_group_until(
                        active_hook,
                        deadline=hook_deadline,
                        poll_interval=CHILD_REAP_POLL_INTERVAL_SECONDS,
                        force_requested=startup_shutdown.repeated_signal_requested,
                        monotonic=monotonic,
                        sleep=sleep,
                    )
            except BaseException:
                lifecycle_events.fail_active_phase()
                raise
            else:
                if generation is not None:
                    lifecycle_events.complete_phase(RuntimePhase.GENERATION_CLEANUP)
                    _finalize_runtime_generation(
                        state=startup_shutdown,
                        lifecycle_events=lifecycle_events,
                    )
            finally:
                startup_shutdown.raise_on_signal = previous_raise_on_signal

        def ensure_runtime_health(child: DirectProcess | None = None) -> None:
            failure = _runtime_failure_message(runtime_health)
            if failure is None:
                return
            startup_shutdown.admit_runtime_failure()
            cleanup_startup_failure(
                child,
                cause=RuntimeGenerationStopCause.CONTROLLER_FAILURE,
            )
            raise RuntimeExecutionError(RUNTIME_LOGGING_UNAVAILABLE_MESSAGE)

        def prioritize_runtime_health(
            error: BaseException,
            child: DirectProcess | None = None,
        ) -> None:
            failure = _runtime_failure_message(runtime_health)
            if failure is None:
                return
            startup_shutdown.admit_runtime_failure()
            cleanup_startup_failure(
                child,
                cause=RuntimeGenerationStopCause.CONTROLLER_FAILURE,
            )
            raise RuntimeExecutionError(RUNTIME_LOGGING_UNAVAILABLE_MESSAGE) from error

        if startup_shutdown.requested_signal is not None:
            return finish_startup_signal_shutdown()

        try:
            ensure_runtime_health()
            previous_raise_on_signal = startup_shutdown.raise_on_signal
            startup_shutdown.raise_on_signal = False
            try:
                lifecycle_events.start_phase(RuntimePhase.RUNTIME_FILES_PREPARATION)
                downloads.activate(cancel_requested=startup_cancellation)
            except RuntimeFilePlanError as error:
                prioritize_runtime_health(error)
                cleanup_startup_failure()
                raise RuntimeExecutionError(
                    format_runtime_diagnostics(
                        "runtime file configuration is invalid", error.diagnostics
                    )
                ) from error
            except RuntimeStateError as error:
                prioritize_runtime_health(error)
                cleanup_startup_failure()
                raise RuntimeExecutionError(
                    "runtime state is invalid or unavailable; remove the runtime "
                    "state file and restart"
                ) from error
            except RuntimeFileDownloadError as error:
                prioritize_runtime_health(error)
                cleanup_startup_failure()
                raise RuntimeExecutionError(
                    format_runtime_diagnostics(
                        "runtime download failed", error.diagnostics
                    )
                ) from error
            except ApplicationError as error:
                prioritize_runtime_health(error)
                cleanup_startup_failure()
                raise RuntimeExecutionError("runtime download failed") from error
            except RuntimeAsyncQueueStartupError as error:
                prioritize_runtime_health(error)
                cleanup_startup_failure()
                raise RuntimeExecutionError("runtime download queue failed") from error
            finally:
                startup_shutdown.raise_on_signal = previous_raise_on_signal
            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown()
            ensure_runtime_health()
            lifecycle_events.complete_phase(RuntimePhase.RUNTIME_FILES_PREPARATION)
            pre_start_hooks = hook_plan.for_phase("pre-start")
            if pre_start_hooks:
                lifecycle_events.start_phase(RuntimePhase.PRE_START_HOOKS)
            try:
                _run_pre_start_hooks(
                    hook_plan,
                    runtime=runtime,
                    source_env=source_env,
                    runtime_hook_runner=runtime_hook_runner,
                    startup_shutdown=startup_shutdown,
                    startup_cancellation=startup_cancellation,
                    event_sink=lifecycle_events.event_sink,
                )
            except RuntimeExecutionError as error:
                prioritize_runtime_health(error)
                cleanup_startup_failure()
                raise
            if pre_start_hooks:
                lifecycle_events.complete_phase(RuntimePhase.PRE_START_HOOKS)
            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown()
            ensure_runtime_health()
            previous_raise_on_signal = startup_shutdown.raise_on_signal
            startup_shutdown.raise_on_signal = False
            ssh_enabled = config.system.ssh.enable
            try:
                if ssh_enabled:
                    lifecycle_events.start_phase(RuntimePhase.SSH_STARTUP)
                ssh_service.start(cancel_requested=startup_cancellation)
            except RuntimeSshServiceError as error:
                prioritize_runtime_health(error)
                cleanup_startup_failure()
                raise RuntimeExecutionError(str(error)) from error
            finally:
                startup_shutdown.raise_on_signal = previous_raise_on_signal
            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown()
            ensure_runtime_health()
            try:
                ssh_service.ensure_running_before_comfyui()
            except RuntimeSshServiceError as error:
                cleanup_startup_failure()
                raise RuntimeExecutionError(str(error)) from error
            if ssh_enabled:
                lifecycle_events.complete_phase(RuntimePhase.SSH_STARTUP)
            lifecycle_events.emit(
                RuntimeSshOutcome(
                    RuntimeSshStatus(ssh_service.startup_outcome())
                    if ssh_enabled
                    else RuntimeSshStatus.DISABLED
                ),
            )
            ensure_runtime_health()
            try:
                previous_raise_on_signal = startup_shutdown.raise_on_signal
                startup_shutdown.raise_on_signal = False
                downloads.start_async(cancel_requested=startup_cancellation)
            except RuntimeAsyncQueueStartupError as error:
                prioritize_runtime_health(error)
                cleanup_startup_failure()
                raise RuntimeExecutionError(
                    "async runtime download queue failed to start"
                ) from error
            finally:
                startup_shutdown.raise_on_signal = previous_raise_on_signal
            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown()
            ensure_runtime_health()
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
                    cleanup_startup_failure()
                    raise RuntimeExecutionError(str(error)) from error
                argv = build_comfyui_argv(runtime=runtime, config=config)
                if startup_shutdown.requested_signal is not None:
                    return finish_startup_signal_shutdown()
                ensure_runtime_health()
                lifecycle_events.start_phase(RuntimePhase.COMFYUI_STARTUP)
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
                    cleanup_startup_failure()
                    raise RuntimeExecutionError(str(error)) from error
                lifecycle_events.complete_phase(RuntimePhase.COMFYUI_STARTUP)
                if startup_shutdown.requested_signal is not None:
                    return finish_startup_signal_shutdown(completed)
                ssh_service.monitor_after_comfyui_start()
            finally:
                startup_shutdown.raise_on_signal = True

            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown(completed)
            ensure_runtime_health(completed)
            post_start_hooks = hook_plan.for_phase("post-start")
            try:
                if post_start_hooks:
                    lifecycle_events.start_phase(RuntimePhase.COMFYUI_READINESS)
                _wait_for_readiness_if_required(
                    hook_plan,
                    config=config,
                    child=completed,
                    readiness_waiter=readiness_waiter,
                )
                if post_start_hooks:
                    lifecycle_events.complete_phase(RuntimePhase.COMFYUI_READINESS)
                    lifecycle_events.start_phase(RuntimePhase.POST_START_HOOKS)
                _run_post_start_hooks_if_required(
                    hook_plan,
                    runtime=runtime,
                    source_env=source_env,
                    child=completed,
                    runtime_hook_runner=runtime_hook_runner,
                    startup_shutdown=startup_shutdown,
                    startup_cancellation=startup_cancellation,
                    event_sink=lifecycle_events.event_sink,
                )
                if post_start_hooks:
                    lifecycle_events.complete_phase(RuntimePhase.POST_START_HOOKS)
            except RuntimeExecutionError as error:
                prioritize_runtime_health(error, completed)
                cleanup_startup_failure(completed)
                raise
            if startup_shutdown.requested_signal is not None:
                return finish_startup_signal_shutdown(completed)
            ensure_runtime_health(completed)
            try:
                runtime_started()
            except RuntimeExecutionError:
                cleanup_startup_failure(
                    completed,
                    cause=RuntimeGenerationStopCause.CONTROLLER_FAILURE,
                )
                raise
        except _StartupShutdownRequested as request:
            del request
            if completed is None:
                return finish_startup_signal_shutdown()
            return finish_startup_signal_shutdown(completed)

        try:
            assert completed is not None
            return _wait_with_existing_signal_state(
                completed,
                hook_plan=hook_plan,
                runtime=runtime,
                source_env=source_env,
                runtime_stop_hook_runner=runtime_stop_hook_runner,
                downloads=downloads,
                ssh_service=ssh_service,
                lifecycle_events=lifecycle_events,
                startup_shutdown=startup_shutdown,
                restart_acceptor=restart_acceptor,
                runtime_health=runtime_health,
                monotonic=monotonic,
                sleep=sleep,
            )
        except _StartupShutdownRequested:
            return finish_startup_signal_shutdown(completed)


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
    startup_cancellation: _RuntimeStartupCancellation,
    event_sink: EventSink[RuntimeEvent],
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
            cancel_requested=startup_cancellation,
            event_sink=event_sink,
        )
    except RuntimeHookError as error:
        startup_shutdown.active_hook_process = error.active_process
        if startup_shutdown.requested_signal is not None:
            raise _StartupShutdownRequested(
                startup_shutdown.requested_signal
            ) from error
        raise RuntimeExecutionError(
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
        raise RuntimeExecutionError(
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
    startup_cancellation: _RuntimeStartupCancellation,
    event_sink: EventSink[RuntimeEvent],
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
            cancel_requested=startup_cancellation,
            event_sink=event_sink,
        )
    except RuntimeHookError as error:
        startup_shutdown.active_hook_process = error.active_process
        if startup_shutdown.requested_signal is not None:
            raise _StartupShutdownRequested(
                startup_shutdown.requested_signal
            ) from error
        raise RuntimeExecutionError(
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


@dataclass(frozen=True, slots=True)
class _OrdinarySignalDecision:
    force: bool
    signal: signal.Signals


class _StartupShutdownState:
    """Signal state while startup work remains cancellable."""

    def __init__(
        self,
        *,
        shutdown_timeout: int | float,
        monotonic: Callable[[], float],
        external_shutdown_observer: Callable[[signal.Signals], object] = (
            lambda _sig: None
        ),
    ) -> None:
        self._external_signal_state: tuple[signal.Signals | None, bool] = (
            None,
            False,
        )
        self.raise_on_signal = True
        self.active_hook_process: SessionLeaderProcess | None = None
        self.timeline: _ShutdownTimeline | None = None
        self._shutdown_timeout = shutdown_timeout
        self._monotonic = monotonic
        self._external_shutdown_observer = external_shutdown_observer
        self._cancel_event = threading.Event()
        self._repeated_event = threading.Event()
        self._signal_admission_enabled = True

    def request_shutdown(self, sig: signal.Signals) -> None:
        if not self._signal_admission_enabled:
            self._external_shutdown_observer(sig)
            return
        self._external_shutdown_observer(sig)
        requested_signal, _repeated_signal = self._external_signal_state
        if requested_signal is None:
            self._external_signal_state = (sig, False)
            self.timeline = _ShutdownTimeline.start(
                self._shutdown_timeout,
                now=self._monotonic(),
            )
        else:
            self._external_signal_state = (requested_signal, True)
            self._repeated_event.set()
        self._cancel_event.set()
        if self.raise_on_signal:
            raise _StartupShutdownRequested(self.requested_signal)

    def cancel_requested(self) -> bool:
        return self.requested_signal is not None

    def admit_runtime_failure(self) -> None:
        if self.timeline is None:
            self.timeline = _ShutdownTimeline.start(
                self._shutdown_timeout,
                now=self._monotonic(),
            )

    @property
    def requested_signal(self) -> signal.Signals | None:
        return self._external_signal_state[0]

    @property
    def repeated_signal(self) -> bool:
        return self._external_signal_state[1]

    def __call__(self) -> bool:
        return self.cancel_requested()

    def repeated_signal_requested(self) -> bool:
        return self.repeated_signal

    def force_requested(self) -> bool:
        return self.repeated_signal_requested()

    def ordinary_signal_decision(
        self,
        default: signal.Signals,
    ) -> _OrdinarySignalDecision:
        requested_signal, repeated_signal = self._external_signal_state
        return _OrdinarySignalDecision(
            force=repeated_signal,
            signal=requested_signal or default,
        )

    def shutdown_deadline(self) -> float | None:
        timeline = self.timeline
        return None if timeline is None else timeline.pre_stop_deadline

    @property
    def shutdown_timeout(self) -> int | float:
        return self._shutdown_timeout

    def wait(self, timeout: float) -> bool:
        return self._cancel_event.wait(timeout)

    def wait_for_force(self, timeout: float) -> bool:
        return self._repeated_event.wait(timeout)

    def repeated_cancellation(self) -> _RepeatedStartupCancellation:
        return _RepeatedStartupCancellation(self)

    def disable_signal_admission(self) -> None:
        """Freeze this generation's signal state while still notifying its observer."""
        self._signal_admission_enabled = False


def _finalize_runtime_generation(
    *,
    state: _StartupShutdownState,
    lifecycle_events: _RuntimeLifecycleEvents,
) -> bool:
    """Freeze signal ownership, apply takeover precedence, and publish terminal."""
    state.disable_signal_admission()
    external_shutdown = state.requested_signal is not None
    if external_shutdown:
        lifecycle_events.begin_stopping(RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN)
    lifecycle_events.mark_stopped()
    return external_shutdown


@dataclass(frozen=True, slots=True)
class _RuntimeStartupCancellation:
    state: _StartupShutdownState
    runtime_health: RuntimeHealthObserver | None

    def __call__(self) -> bool:
        if self.state.cancel_requested():
            return True
        return _runtime_failure_message(self.runtime_health) is not None

    def wait(self, timeout: float) -> bool:
        if self.runtime_health is None:
            return self.state.wait(timeout)
        return self.runtime_health.wait(timeout)

    def force_requested(self) -> bool:
        return self.state.force_requested()

    def wait_for_force(self, timeout: float) -> bool:
        return self.state.wait_for_force(timeout)

    def shutdown_deadline(self) -> float | None:
        return self.state.shutdown_deadline()


@dataclass(frozen=True, slots=True)
class _RepeatedStartupCancellation:
    state: _StartupShutdownState

    def __call__(self) -> bool:
        return self.state.repeated_signal_requested()

    def force_requested(self) -> bool:
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
    lifecycle_events: _RuntimeLifecycleEvents,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> int:
    lifecycle_events.begin_stopping(RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN)
    state.raise_on_signal = False
    if child is not None and child.poll() is not None:
        exit_code = child.wait()

        def cleanup_terminal_child() -> int:
            downloads.stop(cancel_requested=lambda: False)
            ssh_service.stop(cancel_requested=lambda: False)
            return exit_code

        result = lifecycle_events.run_generation_cleanup(cleanup_terminal_child)
        _finalize_runtime_generation(
            state=state,
            lifecycle_events=lifecycle_events,
        )
        return result
    sig = state.requested_signal
    timeline = state.timeline
    assert sig is not None
    assert timeline is not None
    result = _finish_signal_shutdown(
        sig,
        timeline=timeline,
        child=child,
        hook_plan=hook_plan,
        runtime=runtime,
        source_env=source_env,
        runtime_stop_hook_runner=runtime_stop_hook_runner,
        stop_hooks_cancelled=state.repeated_cancellation(),
        force_requested=state.repeated_signal_requested,
        hook_processes=(
            () if state.active_hook_process is None else (state.active_hook_process,)
        ),
        downloads=downloads,
        ssh_service=ssh_service,
        lifecycle_events=lifecycle_events,
        cause=RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN,
        monotonic=monotonic,
        sleep=sleep,
    ).returncode
    _finalize_runtime_generation(
        state=state,
        lifecycle_events=lifecycle_events,
    )
    return result


def _wait_with_existing_signal_state(
    child: DirectProcess,
    *,
    hook_plan: RuntimeHookPlan,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    startup_shutdown: _StartupShutdownState,
    lifecycle_events: _RuntimeLifecycleEvents,
    restart_acceptor: RuntimeRestartAcceptor | None = None,
    runtime_health: RuntimeHealthObserver | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> RuntimeGenerationResult:
    """Wait for the child under the startup-installed signal authority."""
    startup_shutdown.raise_on_signal = True
    try:
        try:
            if restart_acceptor is None and runtime_health is None:
                exit_code = child.wait()
            else:
                while True:
                    child_result = reap_process_if_exited(child)
                    if child_result is not None:
                        exit_code = child_result
                        break
                    requested_signal = startup_shutdown.requested_signal
                    if requested_signal is not None:
                        raise _StartupShutdownRequested(requested_signal)
                    failure = _runtime_failure_message(runtime_health)
                    if failure is not None:
                        return _finish_controller_failure(
                            child,
                            hook_plan=hook_plan,
                            runtime=runtime,
                            source_env=source_env,
                            runtime_stop_hook_runner=runtime_stop_hook_runner,
                            downloads=downloads,
                            ssh_service=ssh_service,
                            startup_shutdown=startup_shutdown,
                            lifecycle_events=lifecycle_events,
                            timeline=_ShutdownTimeline.start(
                                startup_shutdown.shutdown_timeout,
                                now=monotonic(),
                            ),
                            monotonic=monotonic,
                            sleep=sleep,
                        )

                    startup_shutdown.raise_on_signal = False
                    try:
                        child_result = reap_process_if_exited(child)
                        if child_result is not None:
                            exit_code = child_result
                            break
                        requested_signal = startup_shutdown.requested_signal
                        if requested_signal is not None:
                            raise _StartupShutdownRequested(requested_signal)
                        failure = _runtime_failure_message(runtime_health)
                        if failure is not None:
                            return _finish_controller_failure(
                                child,
                                hook_plan=hook_plan,
                                runtime=runtime,
                                source_env=source_env,
                                runtime_stop_hook_runner=runtime_stop_hook_runner,
                                downloads=downloads,
                                ssh_service=ssh_service,
                                startup_shutdown=startup_shutdown,
                                lifecycle_events=lifecycle_events,
                                timeline=_ShutdownTimeline.start(
                                    startup_shutdown.shutdown_timeout,
                                    now=monotonic(),
                                ),
                                monotonic=monotonic,
                                sleep=sleep,
                            )
                        accepted_at = monotonic()
                        if restart_acceptor is not None and (
                            restart_acceptor.accept_if_requested(
                                accepted_at=accepted_at
                            )
                        ):
                            return _finish_operator_restart(
                                child,
                                hook_plan=hook_plan,
                                runtime=runtime,
                                source_env=source_env,
                                runtime_stop_hook_runner=runtime_stop_hook_runner,
                                downloads=downloads,
                                ssh_service=ssh_service,
                                startup_shutdown=startup_shutdown,
                                lifecycle_events=lifecycle_events,
                                timeline=_ShutdownTimeline.start(
                                    startup_shutdown.shutdown_timeout,
                                    now=accepted_at,
                                ),
                                monotonic=monotonic,
                                sleep=sleep,
                            )
                    finally:
                        startup_shutdown.raise_on_signal = True
                    if restart_acceptor is None:
                        sleep(CHILD_REAP_POLL_INTERVAL_SECONDS)
                    else:
                        restart_acceptor.wait(CHILD_REAP_POLL_INTERVAL_SECONDS)
        except _StartupShutdownRequested:
            if child.poll() is None:
                startup_shutdown.raise_on_signal = False
                return RuntimeGenerationResult(
                    cause=RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN,
                    returncode=_finish_startup_signal_shutdown(
                        startup_shutdown,
                        child=child,
                        hook_plan=hook_plan,
                        runtime=runtime,
                        source_env=source_env,
                        runtime_stop_hook_runner=runtime_stop_hook_runner,
                        downloads=downloads,
                        ssh_service=ssh_service,
                        lifecycle_events=lifecycle_events,
                        monotonic=monotonic,
                        sleep=sleep,
                    ),
                )
            exit_code = child.wait()

        startup_shutdown.raise_on_signal = False
        startup_shutdown.disable_signal_admission()
        lifecycle_events.begin_stopping(RuntimeGenerationStopCause.NATURAL_EXIT)

        def cleanup_natural_exit() -> RuntimeGenerationResult:
            downloads.stop(cancel_requested=startup_shutdown.repeated_signal_requested)
            ssh_service.stop(
                cancel_requested=startup_shutdown.repeated_signal_requested
            )
            return RuntimeGenerationResult(
                cause=RuntimeGenerationStopCause.NATURAL_EXIT,
                returncode=exit_code,
            )

        result = lifecycle_events.run_generation_cleanup(cleanup_natural_exit)
        _finalize_runtime_generation(
            state=startup_shutdown,
            lifecycle_events=lifecycle_events,
        )
        return result
    finally:
        startup_shutdown.raise_on_signal = False


def _finish_operator_restart(
    child: DirectProcess,
    *,
    hook_plan: RuntimeHookPlan,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    startup_shutdown: _StartupShutdownState,
    lifecycle_events: _RuntimeLifecycleEvents,
    timeline: _ShutdownTimeline,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> RuntimeGenerationResult:
    startup_shutdown.raise_on_signal = False
    shutdown = _finish_signal_shutdown(
        signal.SIGTERM,
        timeline=timeline,
        child=child,
        hook_plan=hook_plan,
        runtime=runtime,
        source_env=source_env,
        runtime_stop_hook_runner=runtime_stop_hook_runner,
        stop_hooks_cancelled=startup_shutdown.repeated_cancellation(),
        force_requested=startup_shutdown.repeated_signal_requested,
        hook_processes=(),
        downloads=downloads,
        ssh_service=ssh_service,
        lifecycle_events=lifecycle_events,
        cause=RuntimeGenerationStopCause.OPERATOR_RESTART,
        monotonic=monotonic,
        sleep=sleep,
        ordinary_signal_decider=startup_shutdown.ordinary_signal_decision,
    )
    if _finalize_runtime_generation(
        state=startup_shutdown,
        lifecycle_events=lifecycle_events,
    ):
        return RuntimeGenerationResult(
            cause=RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN,
            returncode=shutdown.returncode,
        )
    if shutdown.stop_hook_attempt.failure is not None:
        raise shutdown.stop_hook_attempt.failure
    return RuntimeGenerationResult(
        cause=RuntimeGenerationStopCause.OPERATOR_RESTART,
        returncode=shutdown.returncode,
    )


def _finish_controller_failure(
    child: DirectProcess,
    *,
    hook_plan: RuntimeHookPlan,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    startup_shutdown: _StartupShutdownState,
    lifecycle_events: _RuntimeLifecycleEvents,
    timeline: _ShutdownTimeline,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> RuntimeGenerationResult:
    startup_shutdown.raise_on_signal = False
    shutdown = _finish_signal_shutdown(
        signal.SIGTERM,
        timeline=timeline,
        child=child,
        hook_plan=hook_plan,
        runtime=runtime,
        source_env=source_env,
        runtime_stop_hook_runner=runtime_stop_hook_runner,
        stop_hooks_cancelled=startup_shutdown.repeated_cancellation(),
        force_requested=startup_shutdown.repeated_signal_requested,
        hook_processes=(),
        downloads=downloads,
        ssh_service=ssh_service,
        lifecycle_events=lifecycle_events,
        cause=RuntimeGenerationStopCause.CONTROLLER_FAILURE,
        monotonic=monotonic,
        sleep=sleep,
        ordinary_signal_decider=startup_shutdown.ordinary_signal_decision,
    )
    if _finalize_runtime_generation(
        state=startup_shutdown,
        lifecycle_events=lifecycle_events,
    ):
        return RuntimeGenerationResult(
            cause=RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN,
            returncode=shutdown.returncode,
        )
    return RuntimeGenerationResult(
        cause=RuntimeGenerationStopCause.CONTROLLER_FAILURE,
        returncode=shutdown.returncode,
    )


@dataclass(frozen=True, slots=True)
class _StopHookAttempt:
    active_process: SessionLeaderProcess | None = None
    failure: RuntimeExecutionError | None = None


@dataclass(frozen=True, slots=True)
class _ManagedShutdownResult:
    returncode: int
    stop_hook_attempt: _StopHookAttempt


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
    force_requested: Callable[[], bool],
    hook_processes: tuple[SessionLeaderProcess, ...],
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    lifecycle_events: _RuntimeLifecycleEvents,
    cause: RuntimeGenerationStopCause,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
    ordinary_signal_decider: (
        Callable[[signal.Signals], _OrdinarySignalDecision] | None
    ) = None,
) -> _ManagedShutdownResult:
    """Complete one first-signal shutdown against its sole absolute timeline."""
    lifecycle_events.begin_stopping(cause)
    stop_hook_attempt = _StopHookAttempt()
    downloads.request_stop(deadline=timeline.auxiliary_deadline)
    ssh_service.request_stop()
    if force_requested():
        return lifecycle_events.run_generation_cleanup(
            lambda: _ManagedShutdownResult(
                returncode=_force_managed_shutdown(
                    sig,
                    child=child,
                    downloads=downloads,
                    ssh_service=ssh_service,
                    hook_processes=hook_processes,
                    deadline=timeline.deadline,
                    monotonic=monotonic,
                    sleep=sleep,
                ),
                stop_hook_attempt=stop_hook_attempt,
            )
        )
    if child is None:

        def cleanup_without_child() -> _ManagedShutdownResult:
            _wait_for_auxiliary_shutdown(
                downloads=downloads,
                ssh_service=ssh_service,
                deadline=timeline.deadline,
                auxiliary_deadline=timeline.auxiliary_deadline,
                hook_processes=hook_processes,
                force_requested=force_requested,
                monotonic=monotonic,
                sleep=sleep,
            )
            return _ManagedShutdownResult(
                returncode=-int(sig),
                stop_hook_attempt=stop_hook_attempt,
            )

        return lifecycle_events.run_generation_cleanup(cleanup_without_child)

    if not stop_hooks_cancelled():
        stop_hook_attempt = _run_stop_hooks_before_signal(
            hook_plan,
            runtime=runtime,
            source_env=source_env,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            cancel_requested=stop_hooks_cancelled,
            deadline=timeline.pre_stop_deadline,
            lifecycle_events=lifecycle_events,
            monotonic=monotonic,
            sleep=sleep,
        )
        if stop_hook_attempt.active_process is not None:
            hook_processes = (*hook_processes, stop_hook_attempt.active_process)

    def cleanup_child() -> _ManagedShutdownResult:
        if force_requested():
            return _ManagedShutdownResult(
                returncode=_force_managed_shutdown(
                    sig,
                    child=child,
                    downloads=downloads,
                    ssh_service=ssh_service,
                    hook_processes=hook_processes,
                    deadline=timeline.deadline,
                    monotonic=monotonic,
                    sleep=sleep,
                ),
                stop_hook_attempt=stop_hook_attempt,
            )
        if monotonic() >= timeline.auxiliary_deadline:
            _force_auxiliary_shutdown(
                downloads,
                ssh_service,
                downloads_stopped=downloads.is_stopped(),
                ssh_stopped=ssh_service.is_stopped(),
            )
        ordinary_signal_decision = (
            _OrdinarySignalDecision(force=force_requested(), signal=sig)
            if ordinary_signal_decider is None
            else ordinary_signal_decider(sig)
        )
        if not ordinary_signal_decision.force:
            send_direct_process_signal(child, ordinary_signal_decision.signal)
            return _ManagedShutdownResult(
                returncode=_wait_for_managed_shutdown(
                    child,
                    downloads=downloads,
                    ssh_service=ssh_service,
                    deadline=timeline.deadline,
                    auxiliary_deadline=timeline.auxiliary_deadline,
                    hook_processes=hook_processes,
                    force_requested=force_requested,
                    monotonic=monotonic,
                    sleep=sleep,
                ),
                stop_hook_attempt=stop_hook_attempt,
            )
        return _ManagedShutdownResult(
            returncode=_force_managed_shutdown(
                sig,
                child=child,
                downloads=downloads,
                ssh_service=ssh_service,
                hook_processes=hook_processes,
                deadline=timeline.deadline,
                monotonic=monotonic,
                sleep=sleep,
            ),
            stop_hook_attempt=stop_hook_attempt,
        )

    return lifecycle_events.run_generation_cleanup(cleanup_child)


def _run_stop_hooks_before_signal(
    hook_plan: RuntimeHookPlan,
    *,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    cancel_requested: Callable[[], bool],
    deadline: float | None,
    lifecycle_events: _RuntimeLifecycleEvents,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> _StopHookAttempt:
    if not hook_plan.for_phase("stop"):
        return _StopHookAttempt()
    if deadline is not None and monotonic() >= deadline:
        return _StopHookAttempt(
            failure=RuntimeExecutionError(
                "runtime stop hook failed: shutdown deadline expired before execution"
            )
        )
    try:
        lifecycle_events.start_phase(RuntimePhase.STOP_HOOKS)
        runtime_stop_hook_runner(
            hook_plan,
            runtime=runtime,
            env=source_env,
            cancel_requested=cancel_requested,
            deadline=deadline,
            monotonic=monotonic,
            sleep=sleep,
            event_sink=lifecycle_events.event_sink,
        )
    except RuntimeHookError as error:
        lifecycle_events.fail_active_phase()
        render_runtime_diagnostics("Runtime stop hook failed:", error.diagnostics)
        return _StopHookAttempt(
            active_process=error.active_process,
            failure=RuntimeExecutionError(
                format_runtime_diagnostics(
                    "runtime stop hook failed",
                    error.diagnostics,
                )
            ),
        )
    lifecycle_events.complete_phase(RuntimePhase.STOP_HOOKS)
    return _StopHookAttempt()


def _wait_for_auxiliary_shutdown(
    *,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    deadline: float | None,
    auxiliary_deadline: float,
    hook_processes: tuple[SessionLeaderProcess, ...],
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
    force_requested: Callable[[], bool] = lambda: False,
) -> None:
    """Bound startup-signal auxiliary cleanup without a second outer budget."""
    while True:
        if force_requested():
            _force_managed_shutdown(
                signal.SIGTERM,
                child=None,
                downloads=downloads,
                ssh_service=ssh_service,
                hook_processes=hook_processes,
                deadline=deadline,
                monotonic=monotonic,
                sleep=sleep,
            )
            return
        downloads_stopped = downloads.is_stopped()
        ssh_stopped = ssh_service.is_stopped()
        hooks_stopped = _reap_hook_processes(hook_processes)
        if downloads_stopped and ssh_stopped and hooks_stopped:
            return
        now = monotonic()
        if now >= auxiliary_deadline or (deadline is not None and now >= deadline):
            _force_auxiliary_shutdown(
                downloads,
                ssh_service,
                downloads_stopped=downloads_stopped,
                ssh_stopped=ssh_stopped,
            )
            _force_hook_processes(hook_processes)
            return _force_managed_shutdown(
                signal.SIGTERM,
                child=None,
                downloads=downloads,
                ssh_service=ssh_service,
                hook_processes=hook_processes,
                deadline=deadline,
                monotonic=monotonic,
                sleep=sleep,
                force_already_requested=True,
            )
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
    force_requested: Callable[[], bool] = lambda: False,
) -> int:
    """Reap all managed work against one caller-owned absolute timeline."""
    auxiliary_bound_reached = False
    while True:
        if force_requested():
            return _force_managed_shutdown(
                signal.SIGTERM,
                child=child,
                downloads=downloads,
                ssh_service=ssh_service,
                hook_processes=hook_processes,
                deadline=deadline,
                monotonic=monotonic,
                sleep=sleep,
            )
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
            _force_auxiliary_shutdown(
                downloads,
                ssh_service,
                downloads_stopped=downloads_stopped,
                ssh_stopped=ssh_stopped,
            )
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
                return _force_managed_shutdown(
                    signal.SIGTERM,
                    child=child,
                    downloads=downloads,
                    ssh_service=ssh_service,
                    hook_processes=hook_processes,
                    deadline=None,
                    monotonic=monotonic,
                    sleep=sleep,
                    force_already_requested=True,
                )

        if deadline is not None and now >= deadline:
            _force_auxiliary_shutdown(
                downloads,
                ssh_service,
                downloads_stopped=downloads.is_stopped(),
                ssh_stopped=ssh_service.is_stopped(),
            )
            _force_hook_processes(hook_processes)
            request_force_direct_process(child)
            return _force_managed_shutdown(
                signal.SIGTERM,
                child=child,
                downloads=downloads,
                ssh_service=ssh_service,
                hook_processes=hook_processes,
                deadline=deadline,
                monotonic=monotonic,
                sleep=sleep,
                force_already_requested=True,
            )

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


def _force_managed_shutdown(
    sig: signal.Signals,
    *,
    child: DirectProcess | None,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    hook_processes: tuple[SessionLeaderProcess, ...],
    deadline: float | None,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
    force_already_requested: bool = False,
) -> int:
    """Force exact owners concurrently and return only after real quiescence."""
    if not force_already_requested:
        downloads.request_force_stop()
        ssh_service.request_force_stop()
        _force_hook_processes(hook_processes)
        if child is not None:
            request_force_direct_process(child)

    while True:
        child_result = None if child is None else reap_process_if_exited(child)
        downloads_stopped = downloads.is_stopped()
        ssh_stopped = ssh_service.is_stopped()
        hooks_stopped = _reap_hook_processes(hook_processes)
        if (
            (child is None or child_result is not None)
            and downloads_stopped
            and ssh_stopped
            and hooks_stopped
        ):
            if child is None:
                return -int(sig)
            assert child_result is not None
            return child_result

        # The caller's deadline controls escalation, not permission to report a
        # still-owned operation as stopped. Once exhausted, remain fail closed
        # until exact terminal evidence arrives or the orchestrator hard-kills.
        now = monotonic()
        if deadline is not None and now < deadline:
            sleep(min(CHILD_REAP_POLL_INTERVAL_SECONDS, deadline - now))
        else:
            sleep(CHILD_REAP_POLL_INTERVAL_SECONDS)


def _force_auxiliary_shutdown(
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
    *,
    downloads_stopped: bool,
    ssh_stopped: bool,
) -> None:
    """Force active auxiliaries concurrently without creating a new wait."""
    if not downloads_stopped:
        downloads.request_force_stop()
    if not ssh_stopped:
        ssh_service.request_force_stop()


def _reap_hook_processes(processes: tuple[SessionLeaderProcess, ...]) -> bool:
    stopped = True
    for process in processes:
        if reap_process_if_exited(process) is None:
            stopped = False
    return stopped


def _force_hook_processes(processes: tuple[SessionLeaderProcess, ...]) -> bool:
    for process in processes:
        request_force_process_group(process)
    return _reap_hook_processes(processes)
