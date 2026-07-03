"""Container runtime entrypoint orchestration."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from comfyui_docker_helper.config import (
    BAKED_RUNTIME_CONFIG_PATH,
    MOUNTED_RUNTIME_CONFIG_PATH,
    Diagnostic,
    RuntimeConfig,
    RuntimeConfigurationError,
    RuntimeConfigurationResult,
    load_runtime_config,
)
from comfyui_docker_helper.container.readiness import (
    ReadinessError,
    wait_for_comfyui_readiness,
)
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeFileDownloadError,
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
    RuntimeFilePlanError,
    build_runtime_file_plan,
    download_runtime_files,
)
from comfyui_docker_helper.container.runtime_hooks import (
    BAKED_RUNTIME_HOOKS_PATH,
    MOUNTED_RUNTIME_HOOKS_PATH,
    RuntimeHookError,
    RuntimeHookPlan,
    RuntimeHookResult,
    discover_runtime_hooks,
    run_runtime_hooks,
    run_runtime_stop_hooks,
)
from comfyui_docker_helper.errors import ApplicationError


class EntrypointError(ApplicationError):
    """A user-facing container entrypoint failure."""


class EntrypointRunner(Protocol):
    """Subprocess-compatible runner for the ComfyUI child process."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> ChildProcess: ...


class ChildProcess(Protocol):
    """Minimal child process interface used by the entrypoint."""

    returncode: int | None

    def wait(self) -> int: ...

    def poll(self) -> int | None: ...

    def send_signal(self, sig: signal.Signals) -> None: ...

    def terminate(self) -> None: ...


class RuntimeDownloadRunner(Protocol):
    """Runtime file downloader callable used before ComfyUI spawn."""

    def __call__(
        self,
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
    ) -> tuple[RuntimeFileDownloadResult, ...]: ...


class RuntimeHookRunner(Protocol):
    """Runtime hook phase runner callable used during startup."""

    def __call__(
        self,
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        start_new_session: bool = False,
    ) -> tuple[RuntimeHookResult, ...]: ...


class RuntimeStopHookRunner(Protocol):
    """Runtime stop-hook runner with shutdown cancellation support."""

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
    """ComfyUI readiness waiter callable used before post-start hooks."""

    def __call__(
        self,
        port: int,
        *,
        child: ChildProcess,
    ) -> object: ...


def run_entrypoint(
    *,
    runtime: ContainerRuntime,
    baked_config_path: str | Path = BAKED_RUNTIME_CONFIG_PATH,
    mounted_config_path: str | Path = MOUNTED_RUNTIME_CONFIG_PATH,
    baked_hooks_path: str | Path = BAKED_RUNTIME_HOOKS_PATH,
    mounted_hooks_path: str | Path = MOUNTED_RUNTIME_HOOKS_PATH,
    environ: Mapping[str, str] | None = None,
    runner: EntrypointRunner = subprocess.Popen,
    runtime_downloader: RuntimeDownloadRunner = download_runtime_files,
    runtime_hook_runner: RuntimeHookRunner = run_runtime_hooks,
    runtime_stop_hook_runner: RuntimeStopHookRunner = run_runtime_stop_hooks,
    readiness_waiter: ReadinessWaiter = wait_for_comfyui_readiness,
) -> int:
    """Load runtime config, run ComfyUI, and return the child exit code."""
    source_env = os.environ if environ is None else environ
    try:
        result = load_runtime_config(
            baked_config_path=baked_config_path,
            mounted_config_path=mounted_config_path,
            environ=source_env,
        )
    except RuntimeConfigurationError as error:
        raise EntrypointError(
            _format_runtime_config_error(error.diagnostics)
        ) from error

    _render_runtime_config_warnings(result.warnings)
    hook_plan = _discover_runtime_hooks(
        baked_hooks_path=baked_hooks_path,
        mounted_hooks_path=mounted_hooks_path,
    )
    _run_runtime_downloads(
        result,
        runtime=runtime,
        runtime_downloader=runtime_downloader,
    )
    _run_pre_start_hooks(
        hook_plan,
        runtime=runtime,
        source_env=source_env,
        runtime_hook_runner=runtime_hook_runner,
    )

    argv = build_comfyui_argv(runtime=runtime, config=result.config)
    try:
        completed = runner(
            argv,
            cwd=os.fspath(runtime.comfyui_path),
            env=runtime.env(source_env),
            shell=False,
        )
    except FileNotFoundError as error:
        raise EntrypointError(f"ComfyUI executable not found: {argv[0]}") from error
    except OSError as error:
        raise EntrypointError(f"ComfyUI failed to start: {error}") from error

    _wait_for_readiness_if_required(
        hook_plan,
        config=result.config,
        child=completed,
        readiness_waiter=readiness_waiter,
    )
    _run_post_start_hooks_if_required(
        hook_plan,
        runtime=runtime,
        source_env=source_env,
        child=completed,
        runtime_hook_runner=runtime_hook_runner,
    )

    return _normalize_child_exit_code(
        _wait_with_signal_forwarding(
            completed,
            hook_plan=hook_plan,
            runtime=runtime,
            source_env=source_env,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
    )


def _discover_runtime_hooks(
    *,
    baked_hooks_path: str | Path,
    mounted_hooks_path: str | Path,
) -> RuntimeHookPlan:
    try:
        return discover_runtime_hooks(
            baked_hooks_path=baked_hooks_path,
            mounted_hooks_path=mounted_hooks_path,
        )
    except RuntimeHookError as error:
        raise EntrypointError(
            _format_diagnostics(
                "runtime hook configuration is invalid",
                error.diagnostics,
            )
        ) from error


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


def _run_runtime_downloads(
    result: RuntimeConfigurationResult,
    *,
    runtime: ContainerRuntime,
    runtime_downloader: RuntimeDownloadRunner,
) -> None:
    if not result.files:
        return

    try:
        plan = build_runtime_file_plan(
            ({"files": list(result.files)},),
            comfyui_path=runtime.comfyui_path,
        )
        runtime_downloader(plan, config=result.config, log=print)
    except RuntimeFilePlanError as error:
        raise EntrypointError(
            _format_diagnostics(
                "runtime file configuration is invalid", error.diagnostics
            )
        ) from error
    except RuntimeFileDownloadError as error:
        raise EntrypointError(
            _format_diagnostics("runtime download failed", error.diagnostics)
        ) from error
    except ApplicationError as error:
        raise EntrypointError(f"runtime download failed: {error}") from error


def _run_pre_start_hooks(
    hook_plan: RuntimeHookPlan,
    *,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_hook_runner: RuntimeHookRunner,
) -> None:
    if not hook_plan.for_phase("pre-start"):
        return
    try:
        runtime_hook_runner(
            hook_plan,
            "pre-start",
            runtime=runtime,
            env=source_env,
            log=print,
        )
    except RuntimeHookError as error:
        raise EntrypointError(
            _format_diagnostics("runtime hook failed", error.diagnostics)
        ) from error


def _wait_for_readiness_if_required(
    hook_plan: RuntimeHookPlan,
    *,
    config: RuntimeConfig,
    child: ChildProcess,
    readiness_waiter: ReadinessWaiter,
) -> None:
    if not hook_plan.for_phase("post-start"):
        return
    try:
        readiness_waiter(config.comfyui.port, child=child)
    except ReadinessError as error:
        _terminate_child_if_running(child)
        raise EntrypointError(
            _format_diagnostics("ComfyUI readiness failed", error.diagnostics)
        ) from error


def _run_post_start_hooks_if_required(
    hook_plan: RuntimeHookPlan,
    *,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    child: ChildProcess,
    runtime_hook_runner: RuntimeHookRunner,
) -> None:
    if not hook_plan.for_phase("post-start"):
        return
    try:
        runtime_hook_runner(
            hook_plan,
            "post-start",
            runtime=runtime,
            env=source_env,
            log=print,
        )
    except RuntimeHookError as error:
        _terminate_child_if_running(child)
        raise EntrypointError(
            _format_diagnostics("runtime hook failed", error.diagnostics)
        ) from error


def _terminate_child_if_running(child: ChildProcess) -> None:
    if child.poll() is not None:
        return
    with suppress(OSError):
        child.terminate()


class _ShutdownRequested(Exception):
    """The first normal-shutdown signal received during child wait."""

    def __init__(self, sig: signal.Signals) -> None:
        self.sig = sig
        super().__init__(sig.name)


class _ShutdownState:
    """Mutable state shared with signal handlers during graceful shutdown."""

    def __init__(self) -> None:
        self.requested = False
        self._stop_hooks_cancelled = False

    def cancel_stop_hooks(self) -> None:
        self._stop_hooks_cancelled = True

    def stop_hooks_cancelled(self) -> bool:
        return self._stop_hooks_cancelled


def _wait_with_signal_forwarding(
    child: ChildProcess,
    *,
    hook_plan: RuntimeHookPlan,
    runtime: ContainerRuntime,
    source_env: Mapping[str, str],
    runtime_stop_hook_runner: RuntimeStopHookRunner,
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
            return child.wait()
        except _ShutdownRequested as request:
            _run_stop_hooks_before_signal(
                hook_plan,
                runtime=runtime,
                source_env=source_env,
                runtime_stop_hook_runner=runtime_stop_hook_runner,
                cancel_requested=shutdown_state.stop_hooks_cancelled,
            )
            if child.poll() is None:
                child.send_signal(request.sig)
            return child.wait()
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
        _render_nonfatal_diagnostics("Runtime stop hook failed:", error.diagnostics)


def _normalize_child_exit_code(returncode: int) -> int:
    if returncode >= 0:
        return returncode
    return 128 + abs(returncode)


def _format_runtime_config_error(diagnostics: tuple[Diagnostic, ...]) -> str:
    return _format_diagnostics("runtime configuration is invalid", diagnostics)


def _format_diagnostics(header: str, diagnostics: tuple[Diagnostic, ...]) -> str:
    lines = [header]
    for diagnostic in diagnostics:
        lines.append(
            f"[{_format_path(diagnostic.path)}] "
            f"{diagnostic.message} ({diagnostic.code})"
        )
    return "\n".join(lines)


def _render_runtime_config_warnings(diagnostics: tuple[Diagnostic, ...]) -> None:
    if not diagnostics:
        return
    print("Runtime configuration warnings:", file=sys.stderr)
    _render_diagnostics_to_stderr(diagnostics)


def _render_nonfatal_diagnostics(
    header: str,
    diagnostics: tuple[Diagnostic, ...],
) -> None:
    if not diagnostics:
        return
    print(header, file=sys.stderr)
    _render_diagnostics_to_stderr(diagnostics)


def _render_diagnostics_to_stderr(diagnostics: tuple[Diagnostic, ...]) -> None:
    for diagnostic in diagnostics:
        print(
            f"[{_format_path(diagnostic.path)}] "
            f"{diagnostic.message} "
            f"({diagnostic.code}; severity={diagnostic.severity})",
            file=sys.stderr,
        )


def _format_path(path: tuple[str | int, ...]) -> str:
    if not path:
        return "<root>"
    return ".".join(str(part) for part in path)
