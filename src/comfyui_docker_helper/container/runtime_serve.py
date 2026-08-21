"""Container runtime composition root."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType, MappingProxyType
from typing import Literal

from comfyui_docker_helper.cli_output import CliOutputSettings, EventSink, OutputDetail
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
from comfyui_docker_helper.container.runtime_event_delivery import (
    RuntimeBackgroundEventSink,
    RuntimeEventDelivery,
    safe_runtime_event_sink,
)
from comfyui_docker_helper.container.runtime_events import (
    RuntimeEvent,
    RuntimeGenerationAdmitted,
    RuntimeGenerationOperation,
    RuntimeGenerationReady,
    RuntimeGenerationStopCause,
    RuntimeGenerationStopped,
    RuntimeGenerationStopping,
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
    RuntimeHealthObserver,
    RuntimeHookRunner,
    RuntimeStopHookRunner,
    run_runtime_lifecycle,
)
from comfyui_docker_helper.container.runtime_logging import (
    RUNTIME_LOGGING_UNAVAILABLE_MESSAGE,
    RuntimeLoggingBroker,
    RuntimeLoggingFactory,
    open_runtime_logging_broker,
)
from comfyui_docker_helper.container.runtime_presentation import (
    default_runtime_display,
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


@dataclass(slots=True)
class _ServeGenerationLease:
    """Finish one admitted pre-lifecycle generation across signal reentry."""

    generation: str
    state: Literal["owned", "stopping", "stopped"] = "owned"
    cause: RuntimeGenerationStopCause | None = None

    def close(
        self,
        cause: RuntimeGenerationStopCause,
        event_sink: EventSink[RuntimeEvent],
    ) -> None:
        if self.state == "stopped":
            return
        if self.cause is None or cause is RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN:
            self.cause = cause
        if self.state == "owned":
            self.state = "stopping"
            event_sink.emit(RuntimeGenerationStopping(self.generation))
        terminal_cause = self.cause
        assert terminal_cause is not None
        self.state = "stopped"
        event_sink.emit(RuntimeGenerationStopped(self.generation, terminal_cause))


@dataclass(frozen=True, slots=True)
class RuntimeEnvironmentSnapshot:
    """Immutable text and byte views of one runtime-start environment."""

    text: Mapping[str, str] = field(repr=False)
    raw: Mapping[bytes, bytes] = field(repr=False)


def capture_runtime_environment(
    environ: Mapping[str, str] | None = None,
) -> RuntimeEnvironmentSnapshot:
    """Capture one byte-preserving environment with an immutable text view."""
    if environ is None:
        raw_environment = dict(os.environb)
    else:
        raw_environment = {
            os.fsencode(name): os.fsencode(value) for name, value in environ.items()
        }
    text_environment = {
        os.fsdecode(name): os.fsdecode(value) for name, value in raw_environment.items()
    }
    return RuntimeEnvironmentSnapshot(
        text=MappingProxyType(text_environment),
        raw=MappingProxyType(raw_environment),
    )


@dataclass(frozen=True, slots=True)
class RuntimeGeneration:
    """Freshly admitted inputs and owners for one runtime lifecycle."""

    config: RuntimeConfig = field(repr=False)
    hook_plan: RuntimeHookPlan
    environment: RuntimeEnvironmentSnapshot = field(repr=False)
    downloads: RuntimeDownloads
    ssh_service: RuntimeSshService

    @property
    def source_env(self) -> Mapping[str, str]:
        """Return the controller-start text environment used by runtime owners."""
        return self.environment.text

    @property
    def source_env_bytes(self) -> Mapping[bytes, bytes]:
        """Return the matching byte environment for SSH projection."""
        return self.environment.raw


class RuntimeGenerationFactory:
    """Admit fresh runtime inputs from one immutable serve environment."""

    def __init__(
        self,
        *,
        runtime: ContainerRuntime,
        background_event_sink: RuntimeBackgroundEventSink,
        event_sink: EventSink[RuntimeEvent],
        baked_config_path: str | Path = BAKED_RUNTIME_CONFIG_PATH,
        mounted_config_path: str | Path = MOUNTED_RUNTIME_CONFIG_PATH,
        baked_hooks_path: str | Path = BAKED_RUNTIME_HOOKS_PATH,
        mounted_hooks_path: str | Path = MOUNTED_RUNTIME_HOOKS_PATH,
        environment: RuntimeEnvironmentSnapshot,
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
        self._environment = environment
        self._source_env = environment.text
        self._runtime_downloader = runtime_downloader
        self._runtime_async_queue_starter = runtime_async_queue_starter
        self._runtime_ssh_starter = runtime_ssh_starter
        self._runtime_state_path = Path(runtime_state_path)
        self._background_event_sink = background_event_sink
        self._event_sink = safe_runtime_event_sink(event_sink)

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
            environment=self._environment,
            downloads=RuntimeDownloads(
                result.config,
                result.files,
                runtime=self._runtime,
                runtime_state_path=self._runtime_state_path,
                downloader=self._runtime_downloader,
                async_queue_starter=self._runtime_async_queue_starter,
                credential_policy=credential_policy,
                event_sink=self._background_event_sink,
                direct_event_sink=self._event_sink,
            ),
            ssh_service=RuntimeSshService(
                result.config,
                environment=self._environment.raw,
                starter=self._runtime_ssh_starter,
                background_event_sink=self._background_event_sink,
                event_sink=self._event_sink,
            ),
        )


def run_runtime_generation_once(
    *,
    event_sink: EventSink[RuntimeEvent],
    background_event_sink: RuntimeBackgroundEventSink,
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
    event_sink = safe_runtime_event_sink(event_sink)
    environment = capture_runtime_environment(environ)
    source_env = environment.text
    effective_runtime = (
        ContainerRuntime.from_env(source_env) if runtime is None else runtime
    )
    generation = RuntimeGenerationFactory(
        runtime=effective_runtime,
        baked_config_path=baked_config_path,
        mounted_config_path=mounted_config_path,
        baked_hooks_path=baked_hooks_path,
        mounted_hooks_path=mounted_hooks_path,
        environment=environment,
        runtime_downloader=runtime_downloader,
        runtime_async_queue_starter=runtime_async_queue_starter,
        runtime_ssh_starter=runtime_ssh_starter,
        runtime_state_path=runtime_state_path,
        background_event_sink=background_event_sink,
        event_sink=event_sink,
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
        event_sink=event_sink,
    )
    return _normalize_exit_code(result.returncode)


def run_runtime_serve(
    output_settings: CliOutputSettings | None = None,
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
    environment = capture_runtime_environment(environ)
    controller = RuntimeController()
    settings = output_settings or CliOutputSettings()
    with runtime_logging_factory(controller.observe_runtime_failure) as logging_broker:
        event_sink = safe_runtime_event_sink(default_runtime_display(settings))
        with RuntimeEventDelivery(
            event_sink,
            clock=time.monotonic,
            information_enabled=settings.detail is not OutputDetail.QUIET,
            progress_enabled=settings.detail is not OutputDetail.QUIET,
        ) as background_event_sink:
            return _run_runtime_serve(
                controller=controller,
                logging_broker=logging_broker,
                runtime=runtime,
                baked_config_path=baked_config_path,
                mounted_config_path=mounted_config_path,
                baked_hooks_path=baked_hooks_path,
                mounted_hooks_path=mounted_hooks_path,
                environment=environment,
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
                event_sink=event_sink,
                background_event_sink=background_event_sink,
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
    environment: RuntimeEnvironmentSnapshot,
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
    event_sink: EventSink[RuntimeEvent],
    background_event_sink: RuntimeBackgroundEventSink,
) -> int:
    """Own the private endpoint and execute serial runtime generations."""
    event_sink = safe_runtime_event_sink(event_sink)
    source_env = environment.text
    effective_runtime = (
        ContainerRuntime.from_env(source_env) if runtime is None else runtime
    )
    factory = RuntimeGenerationFactory(
        runtime=effective_runtime,
        baked_config_path=baked_config_path,
        mounted_config_path=mounted_config_path,
        baked_hooks_path=baked_hooks_path,
        mounted_hooks_path=mounted_hooks_path,
        environment=environment,
        runtime_downloader=runtime_downloader,
        runtime_async_queue_starter=runtime_async_queue_starter,
        runtime_ssh_starter=runtime_ssh_starter,
        runtime_state_path=runtime_state_path,
        background_event_sink=background_event_sink,
        event_sink=event_sink,
    )
    listener = open_runtime_control_listener(control_socket_path)
    with (
        RuntimeControlServer(listener, controller, logging_broker),
        _runtime_controller_signal_handlers(controller),
    ):
        serve_owned_generation: _ServeGenerationLease | None = None

        def close_serve_owned_generation(
            cause: RuntimeGenerationStopCause,
        ) -> None:
            nonlocal serve_owned_generation
            lease = serve_owned_generation
            if lease is None:
                return
            try:
                lease.close(cause, event_sink)
            finally:
                if lease.state == "stopped":
                    serve_owned_generation = None

        try:
            try:
                current_generation = controller.begin_initial_admission()
            except RuntimeControllerError as error:
                failure = controller.runtime_failure_message()
                if failure is None:
                    raise
                raise RuntimeExecutionError(
                    RUNTIME_LOGGING_UNAVAILABLE_MESSAGE
                ) from error
            serve_owned_generation = _ServeGenerationLease(current_generation)
            event_sink.emit(
                RuntimeGenerationAdmitted(
                    current_generation,
                    RuntimeGenerationOperation.INITIAL_START,
                )
            )
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
                    error = RuntimeExecutionError(RUNTIME_LOGGING_UNAVAILABLE_MESSAGE)
                    close_serve_owned_generation(
                        RuntimeGenerationStopCause.CONTROLLER_FAILURE
                    )
                    publish_start_failure(error)
                    raise error
                try:
                    generation = factory.create_generation()
                except RuntimeExecutionError as error:
                    external_exit_code = external_failure_exit_code()
                    if external_exit_code is not None:
                        close_serve_owned_generation(
                            RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN
                        )
                        return external_exit_code
                    close_serve_owned_generation(
                        RuntimeGenerationStopCause.STARTUP_FAILURE
                    )
                    publish_start_failure(error)
                    raise

                def publish_running_checkpoint(
                    *,
                    is_initial: bool = initial_generation,
                    generation_id: str = current_generation,
                ) -> None:
                    failure = controller.runtime_failure_message()
                    if failure is not None:
                        raise RuntimeExecutionError(RUNTIME_LOGGING_UNAVAILABLE_MESSAGE)
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
                            RUNTIME_LOGGING_UNAVAILABLE_MESSAGE
                        ) from transition_error
                    generation_running(controller)
                    event_sink.emit(RuntimeGenerationReady(generation_id))

                try:

                    def claim_lifecycle_ownership() -> None:
                        nonlocal serve_owned_generation
                        serve_owned_generation = None

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
                        event_sink=event_sink,
                        generation=current_generation,
                        runtime_ownership_claimed=claim_lifecycle_ownership,
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
                            RUNTIME_LOGGING_UNAVAILABLE_MESSAGE
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
                    error = RuntimeExecutionError(RUNTIME_LOGGING_UNAVAILABLE_MESSAGE)
                    publish_start_failure(error)
                    raise error

                assert result.cause is RuntimeGenerationStopCause.OPERATOR_RESTART
                successor = controller.allocate_restart_successor()
                if successor is None:
                    failure = controller.runtime_failure_message()
                    if failure is not None:
                        controller.wait_for_terminal_delivery(
                            RUNTIME_CONTROL_ACK_DRAIN_SECONDS
                        )
                        raise RuntimeExecutionError(RUNTIME_LOGGING_UNAVAILABLE_MESSAGE)
                    controller.mark_external_shutdown()
                    shutdown = controller.external_shutdown_snapshot()
                    assert shutdown.signal is not None
                    return 128 + int(shutdown.signal)
                current_generation = successor
                serve_owned_generation = _ServeGenerationLease(successor)
                event_sink.emit(
                    RuntimeGenerationAdmitted(
                        current_generation,
                        RuntimeGenerationOperation.OPERATOR_RESTART,
                    )
                )
                initial_generation = False
        except _RuntimeControllerShutdownRequested as request:
            close_serve_owned_generation(RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN)
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
