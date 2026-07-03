"""Container runtime entrypoint orchestration."""

from __future__ import annotations

import os
import subprocess
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
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]: ...


def run_entrypoint(
    *,
    runtime: ContainerRuntime,
    baked_config_path: str | Path = BAKED_RUNTIME_CONFIG_PATH,
    mounted_config_path: str | Path = MOUNTED_RUNTIME_CONFIG_PATH,
    environ: Mapping[str, str] | None = None,
    runner: EntrypointRunner = subprocess.run,
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

    argv = build_comfyui_argv(runtime=runtime, config=result.config)
    try:
        completed = runner(
            argv,
            cwd=os.fspath(runtime.comfyui_path),
            env=runtime.env(source_env),
            shell=False,
            check=False,
        )
    except FileNotFoundError as error:
        raise EntrypointError(f"ComfyUI executable not found: {argv[0]}") from error
    except OSError as error:
        raise EntrypointError(f"ComfyUI failed to start: {error}") from error

    return completed.returncode


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


def _format_runtime_config_error(diagnostics: tuple[Diagnostic, ...]) -> str:
    lines = ["runtime configuration is invalid"]
    for diagnostic in diagnostics:
        lines.append(
            f"[{_format_path(diagnostic.path)}] "
            f"{diagnostic.message} ({diagnostic.code})"
        )
    return "\n".join(lines)


def _format_path(path: tuple[str | int, ...]) -> str:
    if not path:
        return "<root>"
    return ".".join(str(part) for part in path)
