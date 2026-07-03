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
    load_runtime_config,
)
from comfyui_docker_helper.container.runners import ContainerRuntime
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


def run_entrypoint(
    *,
    runtime: ContainerRuntime,
    baked_config_path: str | Path = BAKED_RUNTIME_CONFIG_PATH,
    mounted_config_path: str | Path = MOUNTED_RUNTIME_CONFIG_PATH,
    environ: Mapping[str, str] | None = None,
    runner: EntrypointRunner = subprocess.Popen,
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
    lines = ["runtime configuration is invalid"]
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
