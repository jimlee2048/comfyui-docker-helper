"""Container runtime composition root."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from comfyui_docker_helper.config import (
    BAKED_RUNTIME_CONFIG_PATH,
    MOUNTED_RUNTIME_CONFIG_PATH,
    RuntimeConfig,
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
    RuntimeHookPlan,
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


@dataclass(frozen=True, slots=True)
class RuntimeGeneration:
    """Freshly admitted inputs and owners for one runtime lifecycle."""

    config: RuntimeConfig
    hook_plan: RuntimeHookPlan
    source_env: Mapping[str, str]
    downloads: RuntimeDownloads
    ssh_service: RuntimeSshService


class RuntimeGenerationFactory:
    """Admit fresh runtime inputs from one immutable serve environment."""

    def __init__(
        self,
        *,
        runtime: ContainerRuntime,
        baked_config_path: str | Path = BAKED_RUNTIME_CONFIG_PATH,
        mounted_config_path: str | Path = MOUNTED_RUNTIME_CONFIG_PATH,
        baked_hooks_path: str | Path = BAKED_RUNTIME_HOOKS_PATH,
        mounted_hooks_path: str | Path = MOUNTED_RUNTIME_HOOKS_PATH,
        environ: Mapping[str, str] | None = None,
        runtime_downloader: RuntimeDownloadRunner = download_runtime_files,
        runtime_async_queue_starter: RuntimeAsyncQueueStarter = (
            start_runtime_async_download_queue
        ),
        runtime_ssh_starter: RuntimeSshStarter = start_sshd_if_enabled,
        runtime_state_path: str | Path = RUNTIME_STATE_PATH,
    ) -> None:
        self._runtime = runtime
        self._baked_config_path = Path(baked_config_path)
        self._mounted_config_path = Path(mounted_config_path)
        self._baked_hooks_path = Path(baked_hooks_path)
        self._mounted_hooks_path = Path(mounted_hooks_path)
        self._source_env = MappingProxyType(
            dict(os.environ if environ is None else environ)
        )
        self._runtime_downloader = runtime_downloader
        self._runtime_async_queue_starter = runtime_async_queue_starter
        self._runtime_ssh_starter = runtime_ssh_starter
        self._runtime_state_path = Path(runtime_state_path)

    def create_generation(self) -> RuntimeGeneration:
        """Read current runtime files and construct fresh component owners."""
        try:
            result = load_runtime_config(
                baked_config_path=self._baked_config_path,
                mounted_config_path=self._mounted_config_path,
                environ=self._source_env,
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
                baked_hooks_path=self._baked_hooks_path,
                mounted_hooks_path=self._mounted_hooks_path,
            )
        except RuntimeHookError as error:
            raise EntrypointError(
                format_runtime_diagnostics(
                    "runtime hook configuration is invalid",
                    error.diagnostics,
                )
            ) from error

        return RuntimeGeneration(
            config=result.config,
            hook_plan=hook_plan,
            source_env=self._source_env,
            downloads=RuntimeDownloads(
                result.config,
                result.files,
                runtime=self._runtime,
                runtime_state_path=self._runtime_state_path,
                downloader=self._runtime_downloader,
                async_queue_starter=self._runtime_async_queue_starter,
            ),
            ssh_service=RuntimeSshService(
                result.config,
                runtime=self._runtime,
                starter=self._runtime_ssh_starter,
            ),
        )


def run_runtime_serve(
    *,
    runtime: ContainerRuntime | None = None,
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
    source_env = MappingProxyType(dict(os.environ if environ is None else environ))
    effective_runtime = (
        ContainerRuntime.from_env(source_env) if runtime is None else runtime
    )
    generation = RuntimeGenerationFactory(
        runtime=effective_runtime,
        baked_config_path=baked_config_path,
        mounted_config_path=mounted_config_path,
        baked_hooks_path=baked_hooks_path,
        mounted_hooks_path=mounted_hooks_path,
        environ=source_env,
        runtime_downloader=runtime_downloader,
        runtime_async_queue_starter=runtime_async_queue_starter,
        runtime_ssh_starter=runtime_ssh_starter,
        runtime_state_path=runtime_state_path,
    ).create_generation()
    return _normalize_exit_code(
        run_runtime_lifecycle(
            generation.config,
            generation.hook_plan,
            runtime=effective_runtime,
            source_env=generation.source_env,
            downloads=generation.downloads,
            ssh_service=generation.ssh_service,
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
