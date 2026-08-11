"""Container runtime composition root."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from comfyui_docker_helper.config import (
    BAKED_RUNTIME_CONFIG_PATH,
    MOUNTED_RUNTIME_CONFIG_PATH,
    RuntimeConfigurationError,
    load_runtime_config,
)
from comfyui_docker_helper.container.process_control import DirectProcessStarter
from comfyui_docker_helper.container.readiness import wait_for_comfyui_readiness
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_diagnostics import (
    format_runtime_diagnostics,
    render_runtime_diagnostics,
)
from comfyui_docker_helper.container.runtime_downloads import (
    RuntimeAsyncQueueStarter,
    RuntimeDownloadRunner,
    RuntimeDownloads,
    start_runtime_async_download_queue,
)
from comfyui_docker_helper.container.runtime_files import download_runtime_files
from comfyui_docker_helper.container.runtime_hooks import (
    BAKED_RUNTIME_HOOKS_PATH,
    MOUNTED_RUNTIME_HOOKS_PATH,
    RuntimeHookError,
    discover_runtime_hooks,
    run_runtime_startup_hooks,
    run_runtime_stop_hooks,
)
from comfyui_docker_helper.container.runtime_lifecycle import (
    EntrypointError,
    ReadinessWaiter,
    RuntimeHookRunner,
    RuntimeStopHookRunner,
    run_runtime_lifecycle,
)
from comfyui_docker_helper.container.runtime_ssh_service import (
    RuntimeSshService,
    RuntimeSshStarter,
)
from comfyui_docker_helper.container.runtime_state import RUNTIME_STATE_PATH
from comfyui_docker_helper.container.ssh import start_sshd_if_enabled


def run_entrypoint(
    *,
    runtime: ContainerRuntime,
    baked_config_path: str | Path = BAKED_RUNTIME_CONFIG_PATH,
    mounted_config_path: str | Path = MOUNTED_RUNTIME_CONFIG_PATH,
    baked_hooks_path: str | Path = BAKED_RUNTIME_HOOKS_PATH,
    mounted_hooks_path: str | Path = MOUNTED_RUNTIME_HOOKS_PATH,
    environ: Mapping[str, str] | None = None,
    runner: DirectProcessStarter = subprocess.Popen,
    runtime_downloader: RuntimeDownloadRunner = download_runtime_files,
    runtime_async_queue_starter: RuntimeAsyncQueueStarter = (
        start_runtime_async_download_queue
    ),
    runtime_hook_runner: RuntimeHookRunner = run_runtime_startup_hooks,
    runtime_stop_hook_runner: RuntimeStopHookRunner = run_runtime_stop_hooks,
    readiness_waiter: ReadinessWaiter = wait_for_comfyui_readiness,
    runtime_ssh_starter: RuntimeSshStarter = start_sshd_if_enabled,
    runtime_state_path: str | Path = RUNTIME_STATE_PATH,
) -> int:
    """Compose the admitted runtime inputs and execute one lifecycle."""
    source_env = os.environ if environ is None else environ
    try:
        result = load_runtime_config(
            baked_config_path=baked_config_path,
            mounted_config_path=mounted_config_path,
            environ=source_env,
        )
    except RuntimeConfigurationError as error:
        raise EntrypointError(
            format_runtime_diagnostics(
                "runtime configuration is invalid",
                error.diagnostics,
            )
        ) from error

    render_runtime_diagnostics("Runtime configuration warnings:", result.warnings)
    try:
        hook_plan = discover_runtime_hooks(
            baked_hooks_path=baked_hooks_path,
            mounted_hooks_path=mounted_hooks_path,
        )
    except RuntimeHookError as error:
        raise EntrypointError(
            format_runtime_diagnostics(
                "runtime hook configuration is invalid",
                error.diagnostics,
            )
        ) from error
    render_runtime_diagnostics("Runtime hook warnings:", hook_plan.warnings)

    downloads = RuntimeDownloads(
        result.config,
        result.files,
        runtime=runtime,
        runtime_state_path=Path(runtime_state_path),
        downloader=runtime_downloader,
        async_queue_starter=runtime_async_queue_starter,
    )
    ssh_service = RuntimeSshService(
        result.config,
        runtime=runtime,
        starter=runtime_ssh_starter,
    )
    return _normalize_exit_code(
        run_runtime_lifecycle(
            result.config,
            hook_plan,
            runtime=runtime,
            source_env=source_env,
            downloads=downloads,
            ssh_service=ssh_service,
            runner=runner,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            readiness_waiter=readiness_waiter,
        )
    )


def _normalize_exit_code(returncode: int) -> int:
    if returncode >= 0:
        return returncode
    return 128 + abs(returncode)
