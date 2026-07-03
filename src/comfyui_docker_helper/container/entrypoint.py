"""Container runtime entrypoint orchestration."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
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

    def send_signal(self, sig: signal.Signals) -> None: ...


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
    ) -> tuple[RuntimeHookResult, ...]: ...


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

    return _normalize_child_exit_code(_wait_with_signal_forwarding(completed))


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


def _wait_with_signal_forwarding(child: ChildProcess) -> int:
    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
    }

    def forward(sig: signal.Signals, frame: object) -> None:
        if child.returncode is None:
            child.send_signal(sig)

    try:
        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        return child.wait()
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


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
