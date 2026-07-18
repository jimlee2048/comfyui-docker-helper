"""Ordered container runtime lifecycle orchestration."""

from __future__ import annotations

import os
import signal
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from types import FrameType
from typing import Protocol

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container.process_control import (
    DirectProcess,
    DirectProcessStarter,
    ProcessStartError,
    signal_direct_process,
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
) -> int:
    """Run the fixed cdh startup, child, and cleanup phase order."""
    startup_shutdown = _StartupShutdownState()
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
                stop_startup_auxiliary_services()
                return -int(startup_shutdown.requested_signal)
            _run_pre_start_hooks(
                hook_plan,
                runtime=runtime,
                source_env=source_env,
                runtime_hook_runner=runtime_hook_runner,
                startup_shutdown=startup_shutdown,
            )
            if startup_shutdown.requested_signal is not None:
                stop_startup_auxiliary_services()
                return -int(startup_shutdown.requested_signal)
            previous_raise_on_signal = startup_shutdown.raise_on_signal
            startup_shutdown.raise_on_signal = False
            try:
                ssh_service.start()
            except RuntimeSshServiceError as error:
                raise EntrypointError(str(error)) from error
            finally:
                startup_shutdown.raise_on_signal = previous_raise_on_signal
            if startup_shutdown.requested_signal is not None:
                stop_startup_auxiliary_services()
                return -int(startup_shutdown.requested_signal)
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
                stop_startup_auxiliary_services()
                return -int(startup_shutdown.requested_signal)
        except _StartupShutdownRequested as request:
            stop_startup_auxiliary_services()
            return -int(request.sig)

        completed: DirectProcess | None = None
        try:
            startup_shutdown.raise_on_signal = False
            try:
                if startup_shutdown.requested_signal is not None:
                    stop_startup_auxiliary_services()
                    return -int(startup_shutdown.requested_signal)
                try:
                    ssh_service.ensure_running_before_comfyui()
                except RuntimeSshServiceError as error:
                    stop_startup_auxiliary_services()
                    raise EntrypointError(str(error)) from error
                argv = build_comfyui_argv(runtime=runtime, config=config)
                if startup_shutdown.requested_signal is not None:
                    stop_startup_auxiliary_services()
                    return -int(startup_shutdown.requested_signal)
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
                        stop_startup_auxiliary_services()
                        return -int(startup_shutdown.requested_signal)
                    stop_startup_auxiliary_services()
                    raise EntrypointError(str(error)) from error
                if startup_shutdown.requested_signal is not None:
                    stop_startup_auxiliary_services()
                    return _forward_startup_shutdown_to_child(
                        completed,
                        startup_shutdown.requested_signal,
                    )
                ssh_service.monitor_after_comfyui_start()
            finally:
                startup_shutdown.raise_on_signal = True

            if startup_shutdown.requested_signal is not None:
                stop_startup_auxiliary_services()
                return _forward_startup_shutdown_to_child(
                    completed,
                    startup_shutdown.requested_signal,
                )
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
                stop_startup_auxiliary_services()
                return _forward_startup_shutdown_to_child(
                    completed,
                    startup_shutdown.requested_signal,
                )
        except _StartupShutdownRequested as request:
            if completed is None:
                stop_startup_auxiliary_services()
                return -int(request.sig)
            stop_startup_auxiliary_services()
            return _forward_startup_shutdown_to_child(completed, request.sig)

    assert completed is not None
    return _wait_with_signal_forwarding(
        completed,
        hook_plan=hook_plan,
        runtime=runtime,
        source_env=source_env,
        runtime_stop_hook_runner=runtime_stop_hook_runner,
        downloads=downloads,
        ssh_service=ssh_service,
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
            cancel_requested=startup_shutdown.cancel_requested,
        )
    except RuntimeHookError as error:
        if startup_shutdown.requested_signal is not None:
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
            cancel_requested=startup_shutdown.cancel_requested,
        )
    except RuntimeHookError as error:
        if startup_shutdown.requested_signal is not None:
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


class _StartupShutdownState:
    """Signal state while startup work remains cancellable."""

    def __init__(self) -> None:
        self.requested_signal: signal.Signals | None = None
        self.repeated_signal = False
        self.raise_on_signal = True

    def request_shutdown(self, sig: signal.Signals) -> None:
        if self.requested_signal is None:
            self.requested_signal = sig
        else:
            self.repeated_signal = True
        if self.raise_on_signal:
            raise _StartupShutdownRequested(self.requested_signal)

    def cancel_requested(self) -> bool:
        return self.requested_signal is not None

    def repeated_signal_requested(self) -> bool:
        return self.repeated_signal


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


def _forward_startup_shutdown_to_child(
    child: DirectProcess,
    sig: signal.Signals,
) -> int:
    return signal_direct_process(
        child,
        sig,
        signal_timeout=CHILD_TERMINATION_REAP_GRACE_SECONDS,
        kill_timeout=CHILD_TERMINATION_REAP_GRACE_SECONDS,
        poll_interval=CHILD_REAP_POLL_INTERVAL_SECONDS,
    )


class _ShutdownRequested(Exception):
    def __init__(self, sig: signal.Signals) -> None:
        self.sig = sig
        super().__init__(sig.name)


class _ShutdownState:
    """Signal state during the current pre-T4 graceful shutdown."""

    def __init__(self) -> None:
        self.requested = False
        self._stop_hooks_cancelled = False

    def cancel_stop_hooks(self) -> None:
        self._stop_hooks_cancelled = True

    def stop_hooks_cancelled(self) -> bool:
        return self._stop_hooks_cancelled


def _wait_with_signal_forwarding(
    child: DirectProcess,
    *,
    hook_plan: RuntimeHookPlan,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    downloads: RuntimeDownloads,
    ssh_service: RuntimeSshService,
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
            raise _ShutdownRequested(requested)

    try:
        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        try:
            exit_code = child.wait()
            downloads.stop(cancel_requested=shutdown_state.stop_hooks_cancelled)
            ssh_service.stop(cancel_requested=shutdown_state.stop_hooks_cancelled)
            return exit_code
        except _ShutdownRequested as request:
            downloads.stop(cancel_requested=shutdown_state.stop_hooks_cancelled)
            ssh_service.stop(cancel_requested=shutdown_state.stop_hooks_cancelled)
            if not shutdown_state.stop_hooks_cancelled():
                _run_stop_hooks_before_signal(
                    hook_plan,
                    runtime=runtime,
                    source_env=source_env,
                    runtime_stop_hook_runner=runtime_stop_hook_runner,
                    cancel_requested=shutdown_state.stop_hooks_cancelled,
                )
            return signal_direct_process(
                child,
                request.sig,
                signal_timeout=CHILD_TERMINATION_REAP_GRACE_SECONDS,
                kill_timeout=CHILD_TERMINATION_REAP_GRACE_SECONDS,
                poll_interval=CHILD_REAP_POLL_INTERVAL_SECONDS,
            )
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


def _run_stop_hooks_before_signal(
    hook_plan: RuntimeHookPlan,
    *,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    cancel_requested: Callable[[], bool],
) -> None:
    if not hook_plan.for_phase("stop"):
        return
    try:
        runtime_stop_hook_runner(
            hook_plan,
            runtime=runtime,
            env=source_env,
            log=print,
            cancel_requested=cancel_requested,
        )
    except RuntimeHookError as error:
        render_runtime_diagnostics("Runtime stop hook failed:", error.diagnostics)
