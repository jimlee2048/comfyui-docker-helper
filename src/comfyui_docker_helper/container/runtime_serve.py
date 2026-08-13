"""Container runtime composition root."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, MappingProxyType

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
from comfyui_docker_helper.container.runtime_control import (
    RUNTIME_CONTROL_ACK_DRAIN_SECONDS,
    RUNTIME_CONTROL_SOCKET_PATH,
    open_runtime_control_listener,
)
from comfyui_docker_helper.container.runtime_control_server import RuntimeControlServer
from comfyui_docker_helper.container.runtime_controller import (
    RuntimeController,
    RuntimeControllerError,
)
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
    ReadinessWaiter,
    RuntimeExecutionError,
    RuntimeGenerationStopCause,
    RuntimeHealthObserver,
    RuntimeHookRunner,
    RuntimeStopHookRunner,
    run_runtime_lifecycle,
)
from comfyui_docker_helper.container.runtime_logging import (
    RuntimeLoggingBroker,
    RuntimeLoggingFactory,
    open_runtime_logging_broker,
)
from comfyui_docker_helper.container.runtime_secret_session import (
    RuntimeDownloaderCredentialPolicy,
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
            raise RuntimeExecutionError(
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
            raise RuntimeExecutionError(
                format_runtime_diagnostics(
                    "runtime hook configuration is invalid",
                    error.diagnostics,
                )
            ) from error
        render_runtime_diagnostics("Runtime hook warnings:", hook_plan.warnings)
        credential_policy = (
            RuntimeDownloaderCredentialPolicy.from_config(
                result.config,
                environ=self._source_env,
            )
            if result.config.cdh.downloader.credentials
            else None
        )

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
                credential_policy=credential_policy,
            ),
            ssh_service=RuntimeSshService(
                result.config,
                runtime=self._runtime,
                starter=self._runtime_ssh_starter,
            ),
        )


def run_runtime_generation_once(
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
    runtime_health: RuntimeHealthObserver | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> int:
    """Compose and execute one generation through the injected test seam."""
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
    result = run_runtime_lifecycle(
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
        runtime_health=runtime_health,
        monotonic=monotonic,
        sleep=sleep,
    )
    return _normalize_exit_code(result.returncode)


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
    control_socket_path: Path = RUNTIME_CONTROL_SOCKET_PATH,
    generation_running: Callable[[RuntimeController], object] = lambda _controller: (
        None
    ),
    runtime_logging_factory: RuntimeLoggingFactory = open_runtime_logging_broker,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> int:
    """Own controller-lifetime output and execute serial runtime generations."""
    controller = RuntimeController()
    with runtime_logging_factory(controller.observe_runtime_failure) as logging_broker:
        return _run_runtime_serve(
            controller=controller,
            logging_broker=logging_broker,
            runtime=runtime,
            baked_config_path=baked_config_path,
            mounted_config_path=mounted_config_path,
            baked_hooks_path=baked_hooks_path,
            mounted_hooks_path=mounted_hooks_path,
            environ=environ,
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            readiness_waiter=readiness_waiter,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_state_path=runtime_state_path,
            control_socket_path=control_socket_path,
            generation_running=generation_running,
            monotonic=monotonic,
            sleep=sleep,
        )


def _run_runtime_serve(
    *,
    controller: RuntimeController,
    logging_broker: RuntimeLoggingBroker,
    runtime: ContainerRuntime | None,
    baked_config_path: str | Path,
    mounted_config_path: str | Path,
    baked_hooks_path: str | Path,
    mounted_hooks_path: str | Path,
    environ: Mapping[str, str] | None,
    runner: DirectProcessStarter,
    runtime_downloader: RuntimeDownloadRunner,
    runtime_async_queue_starter: RuntimeAsyncQueueStarter,
    runtime_hook_runner: RuntimeHookRunner,
    runtime_stop_hook_runner: RuntimeStopHookRunner,
    readiness_waiter: ReadinessWaiter,
    runtime_ssh_starter: RuntimeSshStarter,
    runtime_state_path: str | Path,
    control_socket_path: Path,
    generation_running: Callable[[RuntimeController], object],
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> int:
    """Own the private endpoint and execute serial runtime generations."""
    source_env = MappingProxyType(dict(os.environ if environ is None else environ))
    effective_runtime = (
        ContainerRuntime.from_env(source_env) if runtime is None else runtime
    )
    factory = RuntimeGenerationFactory(
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
    )
    listener = open_runtime_control_listener(control_socket_path)
    with (
        RuntimeControlServer(listener, controller, logging_broker),
        _runtime_controller_signal_handlers(controller),
    ):
        try:
            try:
                controller.begin_initial_admission()
            except RuntimeControllerError as error:
                failure = controller.runtime_failure_message()
                if failure is None:
                    raise
                raise RuntimeExecutionError(
                    f"runtime logging failed: {failure}"
                ) from error
            initial_generation = True

            def publish_start_failure(error: RuntimeExecutionError) -> None:
                snapshot = controller.snapshot()
                if snapshot.operation is None:
                    controller.mark_generation_terminal(str(error))
                elif (
                    snapshot.last_restart is not None
                    and snapshot.last_restart.id == snapshot.operation
                ):
                    controller.wait_for_terminal_delivery(
                        RUNTIME_CONTROL_ACK_DRAIN_SECONDS
                    )
                else:
                    controller.publish_restart_terminal(
                        "failed",
                        message=str(error),
                    )
                    controller.wait_for_terminal_delivery(
                        RUNTIME_CONTROL_ACK_DRAIN_SECONDS
                    )

            def external_failure_exit_code() -> int | None:
                shutdown = controller.external_shutdown_snapshot()
                if shutdown.signal is None:
                    return None
                controller.mark_external_shutdown()
                return 128 + int(shutdown.signal)

            while True:
                failure = controller.runtime_failure_message()
                if failure is not None:
                    error = RuntimeExecutionError(f"runtime logging failed: {failure}")
                    publish_start_failure(error)
                    raise error
                try:
                    generation = factory.create_generation()
                except RuntimeExecutionError as error:
                    external_exit_code = external_failure_exit_code()
                    if external_exit_code is not None:
                        return external_exit_code
                    publish_start_failure(error)
                    raise

                def publish_running_checkpoint(
                    *,
                    is_initial: bool = initial_generation,
                ) -> None:
                    failure = controller.runtime_failure_message()
                    if failure is not None:
                        raise RuntimeExecutionError(
                            f"runtime logging failed: {failure}"
                        )
                    try:
                        if is_initial:
                            controller.mark_initial_generation_running()
                        else:
                            controller.publish_restart_terminal("succeeded")
                            controller.release_successful_restart()
                    except RuntimeControllerError as transition_error:
                        failure = controller.runtime_failure_message()
                        if failure is None:
                            raise
                        raise RuntimeExecutionError(
                            f"runtime logging failed: {failure}"
                        ) from transition_error
                    generation_running(controller)

                try:
                    result = run_runtime_lifecycle(
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
                        restart_acceptor=controller,
                        runtime_health=controller,
                        runtime_started=publish_running_checkpoint,
                        external_shutdown_observer=(controller.observe_external_signal),
                        monotonic=monotonic,
                        sleep=sleep,
                    )
                except RuntimeExecutionError as error:
                    external_exit_code = external_failure_exit_code()
                    if external_exit_code is not None:
                        return external_exit_code
                    publish_start_failure(error)
                    raise

                if result.cause is RuntimeGenerationStopCause.NATURAL_EXIT:
                    failure = controller.runtime_failure_message()
                    if failure is not None:
                        error = RuntimeExecutionError(
                            f"runtime logging failed: {failure}"
                        )
                        publish_start_failure(error)
                        raise error
                    controller.mark_generation_terminal("ComfyUI exited.")
                    return _normalize_exit_code(result.returncode)
                if result.cause is RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN:
                    controller.mark_external_shutdown()
                    return _normalize_exit_code(result.returncode)
                if result.cause is RuntimeGenerationStopCause.CONTROLLER_FAILURE:
                    failure = controller.runtime_failure_message()
                    assert failure is not None
                    error = RuntimeExecutionError(f"runtime logging failed: {failure}")
                    publish_start_failure(error)
                    raise error

                successor = controller.allocate_restart_successor()
                if successor is None:
                    failure = controller.runtime_failure_message()
                    if failure is not None:
                        controller.wait_for_terminal_delivery(
                            RUNTIME_CONTROL_ACK_DRAIN_SECONDS
                        )
                        raise RuntimeExecutionError(
                            f"runtime logging failed: {failure}"
                        )
                    controller.mark_external_shutdown()
                    shutdown = controller.external_shutdown_snapshot()
                    assert shutdown.signal is not None
                    return 128 + int(shutdown.signal)
                initial_generation = False
        except _RuntimeControllerShutdownRequested as request:
            controller.mark_external_shutdown()
            return 128 + int(request.signal)


class _RuntimeControllerShutdownRequested(BaseException):
    def __init__(self, sig: signal.Signals) -> None:
        self.signal = sig
        super().__init__(sig.name)


@contextmanager
def _runtime_controller_signal_handlers(controller: RuntimeController):
    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
    }

    def observe(sig: signal.Signals, frame: FrameType | None) -> None:
        del frame
        admitted = signal.Signals(sig)
        controller.observe_external_signal(admitted)
        shutdown = controller.external_shutdown_snapshot()
        if shutdown.repeated:
            return
        assert shutdown.signal is not None
        raise _RuntimeControllerShutdownRequested(shutdown.signal)

    try:
        signal.signal(signal.SIGTERM, observe)
        signal.signal(signal.SIGINT, observe)
        yield
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


def _normalize_exit_code(returncode: int) -> int:
    if returncode >= 0:
        return returncode
    return 128 + abs(returncode)
