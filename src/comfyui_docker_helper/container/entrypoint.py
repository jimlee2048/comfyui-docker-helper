"""Container runtime entrypoint orchestration."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import Parameter, signature
from pathlib import Path
from types import FrameType
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
from comfyui_docker_helper.container.runtime_diagnostics import (
    runtime_error_reason,
    runtime_source_host,
    short_runtime_identity,
)
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeDownloadObservedStatus,
    RuntimeDownloadStateObserver,
    RuntimeFileDownloadError,
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
    RuntimeFilePlanError,
    RuntimeFilePlanItem,
    RuntimeFileReconciliation,
    build_runtime_file_plan,
    download_runtime_files,
    reconcile_runtime_file_plan,
    runtime_file_identity_digest,
)
from comfyui_docker_helper.container.runtime_hooks import (
    BAKED_RUNTIME_HOOKS_PATH,
    MOUNTED_RUNTIME_HOOKS_PATH,
    RuntimeHookError,
    RuntimeHookPlan,
    RuntimeHookResult,
    discover_runtime_hooks,
    run_runtime_startup_hooks,
    run_runtime_stop_hooks,
)
from comfyui_docker_helper.container.runtime_state import (
    RUNTIME_STATE_PATH,
    RuntimeDownloadEntry,
    RuntimeDownloadsState,
    RuntimeState,
    RuntimeStateError,
    failed_runtime_download_entry,
    load_runtime_state,
    prepare_runtime_state_for_start,
    write_runtime_state,
)
from comfyui_docker_helper.errors import ApplicationError

CHILD_TERMINATION_REAP_GRACE_SECONDS = 2.0
CHILD_REAP_POLL_INTERVAL_SECONDS = 0.1
ASYNC_QUEUE_STOP_TIMEOUT_SECONDS = 5.0
ASYNC_QUEUE_STOP_POLL_INTERVAL_SECONDS = 0.05


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
        state_observer: RuntimeDownloadStateObserver | None = None,
    ) -> tuple[RuntimeFileDownloadResult, ...]: ...


class RuntimeAsyncQueueStarter(Protocol):
    """Async runtime download queue starter used before ComfyUI spawn."""

    def __call__(
        self,
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> RuntimeAsyncDownloadQueueHandle | None: ...


class RuntimeAsyncDownloadQueueHandle(Protocol):
    """Async runtime download queue handle used during shutdown."""

    def request_stop(self) -> None: ...

    def terminate_backends(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


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
        cancel_requested: Callable[[], bool],
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


@dataclass(frozen=True, slots=True)
class _RuntimeDownloadQueues:
    sync_plan: RuntimeFilePlan
    async_plan: RuntimeFilePlan


class RuntimeAsyncQueueStartupError(RuntimeError):
    """Async queue infrastructure failed before accepting planned work."""


@dataclass(frozen=True, slots=True)
class _RuntimeAsyncDownloadQueueHandle:
    """Internal handle for the single async runtime download worker."""

    thread: threading.Thread
    accepted: threading.Event
    stop_requested: threading.Event
    backends: list[object]
    backends_lock: threading.Lock

    def request_stop(self) -> None:
        self.stop_requested.set()

    def terminate_backends(self) -> None:
        with self.backends_lock:
            backends = tuple(self.backends)
        for backend in backends:
            cancel = getattr(backend, "cancel", None)
            if callable(cancel):
                with suppress(Exception):
                    cancel()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def is_alive(self) -> bool:
        return self.thread.is_alive()


def start_runtime_async_download_queue(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    runtime: ContainerRuntime,
    runtime_state_path: Path,
    log: Logger,
) -> _RuntimeAsyncDownloadQueueHandle:
    """Start one cdh-managed background queue for async runtime files."""
    del runtime
    try:
        state = load_runtime_state(runtime_state_path)
        _validate_async_download_state_entries(plan, state)
    except Exception as error:
        raise RuntimeAsyncQueueStartupError(str(error)) from error

    state_writer = _RuntimeDownloadStateWriter(runtime_state_path, state, log=log)
    accepted = threading.Event()
    stop_requested = threading.Event()
    startup_finished = threading.Event()
    startup_error: list[BaseException] = []
    backends: list[object] = []
    backends_lock = threading.Lock()

    def accept_queue() -> None:
        accepted.set()

    def observe_backend(backend: object) -> None:
        with backends_lock:
            if not any(registered is backend for registered in backends):
                backends.append(backend)

    def worker() -> None:
        try:
            download_runtime_files(
                plan,
                config=config,
                log=log,
                state_observer=state_writer,
                startup_observer=accept_queue,
                cancel_requested=stop_requested.is_set,
                backend_observer=observe_backend,
            )
            log(f"Async runtime download queue finished: items={len(plan.items)}")
        except Exception as error:
            if not accepted.is_set():
                startup_error.append(error)
            else:
                log(
                    "WARNING: async runtime download worker failed: "
                    f"reason={runtime_error_reason(error)}"
                )
        finally:
            startup_finished.set()

    try:
        thread = threading.Thread(
            target=worker,
            name="cdh-runtime-async-downloads",
            daemon=True,
        )
        thread.start()
    except Exception as error:
        raise RuntimeAsyncQueueStartupError(str(error)) from error

    while not accepted.is_set():
        if startup_finished.wait(0.01):
            break
    if not accepted.is_set():
        error = startup_error[0] if startup_error else RuntimeError("worker exited")
        raise RuntimeAsyncQueueStartupError(str(error)) from error

    log(f"Async runtime download queue accepted: items={len(plan.items)}")
    return _RuntimeAsyncDownloadQueueHandle(
        thread=thread,
        accepted=accepted,
        stop_requested=stop_requested,
        backends=backends,
        backends_lock=backends_lock,
    )


def _validate_async_download_state_entries(
    plan: RuntimeFilePlan,
    state: RuntimeState,
) -> None:
    for item in plan.items:
        digest = runtime_file_identity_digest(item)
        if digest not in state.downloads.entries:
            raise RuntimeStateError(
                f"runtime download state entry is missing for {item.relative_target}"
            )


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
    runtime_async_queue_starter: RuntimeAsyncQueueStarter = (
        start_runtime_async_download_queue
    ),
    runtime_hook_runner: RuntimeHookRunner = run_runtime_startup_hooks,
    runtime_stop_hook_runner: RuntimeStopHookRunner = run_runtime_stop_hooks,
    readiness_waiter: ReadinessWaiter = wait_for_comfyui_readiness,
    runtime_state_path: str | Path = RUNTIME_STATE_PATH,
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
    startup_shutdown = _StartupShutdownState()
    with _startup_shutdown_signal_handlers(startup_shutdown):
        async_handle: RuntimeAsyncDownloadQueueHandle | None = None
        try:
            download_queues = _run_runtime_downloads(
                result,
                runtime=runtime,
                runtime_downloader=runtime_downloader,
                runtime_state_path=runtime_state_path,
            )
            if startup_shutdown.requested_signal is not None:
                return _normalize_signal_exit_code(startup_shutdown.requested_signal)
            _run_pre_start_hooks(
                hook_plan,
                runtime=runtime,
                source_env=source_env,
                runtime_hook_runner=runtime_hook_runner,
                startup_shutdown=startup_shutdown,
            )
            if startup_shutdown.requested_signal is not None:
                return _normalize_signal_exit_code(startup_shutdown.requested_signal)
            async_handle = _start_runtime_async_download_queue(
                download_queues.async_plan,
                config=result.config,
                runtime=runtime,
                runtime_state_path=Path(runtime_state_path),
                runtime_async_queue_starter=runtime_async_queue_starter,
            )
            if startup_shutdown.requested_signal is not None:
                _stop_runtime_async_download_queue_for_startup(
                    async_handle,
                    startup_shutdown=startup_shutdown,
                )
                return _normalize_signal_exit_code(startup_shutdown.requested_signal)
        except _StartupShutdownRequested as request:
            _stop_runtime_async_download_queue_for_startup(
                async_handle,
                startup_shutdown=startup_shutdown,
            )
            return _normalize_signal_exit_code(request.sig)

        completed: ChildProcess | None = None
        try:
            startup_shutdown.raise_on_signal = False
            try:
                if startup_shutdown.requested_signal is not None:
                    _stop_runtime_async_download_queue_for_startup(
                        async_handle,
                        startup_shutdown=startup_shutdown,
                    )
                    return _normalize_signal_exit_code(
                        startup_shutdown.requested_signal
                    )
                argv = build_comfyui_argv(runtime=runtime, config=result.config)
                if startup_shutdown.requested_signal is not None:
                    _stop_runtime_async_download_queue_for_startup(
                        async_handle,
                        startup_shutdown=startup_shutdown,
                    )
                    return _normalize_signal_exit_code(
                        startup_shutdown.requested_signal
                    )
                try:
                    completed = runner(
                        argv,
                        cwd=os.fspath(runtime.comfyui_path),
                        env=runtime.env(source_env),
                        shell=False,
                    )
                except FileNotFoundError as error:
                    if startup_shutdown.requested_signal is not None:
                        _stop_runtime_async_download_queue_for_startup(
                            async_handle,
                            startup_shutdown=startup_shutdown,
                        )
                        return _normalize_signal_exit_code(
                            startup_shutdown.requested_signal
                        )
                    raise EntrypointError(
                        f"ComfyUI executable not found: {argv[0]}"
                    ) from error
                except OSError as error:
                    if startup_shutdown.requested_signal is not None:
                        _stop_runtime_async_download_queue_for_startup(
                            async_handle,
                            startup_shutdown=startup_shutdown,
                        )
                        return _normalize_signal_exit_code(
                            startup_shutdown.requested_signal
                        )
                    raise EntrypointError(
                        f"ComfyUI failed to start: {error}"
                    ) from error
                if startup_shutdown.requested_signal is not None:
                    _stop_runtime_async_download_queue_for_startup(
                        async_handle,
                        startup_shutdown=startup_shutdown,
                    )
                    return _forward_startup_shutdown_to_child(
                        completed,
                        startup_shutdown.requested_signal,
                    )
            finally:
                startup_shutdown.raise_on_signal = True

            if startup_shutdown.requested_signal is not None:
                _stop_runtime_async_download_queue_for_startup(
                    async_handle,
                    startup_shutdown=startup_shutdown,
                )
                return _forward_startup_shutdown_to_child(
                    completed,
                    startup_shutdown.requested_signal,
                )
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
                startup_shutdown=startup_shutdown,
            )
            if startup_shutdown.requested_signal is not None:
                _stop_runtime_async_download_queue_for_startup(
                    async_handle,
                    startup_shutdown=startup_shutdown,
                )
                return _forward_startup_shutdown_to_child(
                    completed,
                    startup_shutdown.requested_signal,
                )
        except _StartupShutdownRequested as request:
            if completed is None:
                _stop_runtime_async_download_queue_for_startup(
                    async_handle,
                    startup_shutdown=startup_shutdown,
                )
                return _normalize_signal_exit_code(request.sig)
            _stop_runtime_async_download_queue_for_startup(
                async_handle,
                startup_shutdown=startup_shutdown,
            )
            return _forward_startup_shutdown_to_child(completed, request.sig)

    assert completed is not None
    return _normalize_child_exit_code(
        _wait_with_signal_forwarding(
            completed,
            hook_plan=hook_plan,
            runtime=runtime,
            source_env=source_env,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            async_handle=async_handle,
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
    runtime_state_path: str | Path,
) -> _RuntimeDownloadQueues:
    if not result.files:
        return _empty_runtime_download_queues()

    try:
        plan = build_runtime_file_plan(
            ({"files": list(result.files)},),
            comfyui_path=runtime.comfyui_path,
            default_download_mode=result.config.cdh.default_download_mode,
        )
        return _activate_runtime_file_plan(
            plan,
            config=result.config,
            runtime=runtime,
            runtime_downloader=runtime_downloader,
            runtime_state_path=Path(runtime_state_path),
        )
    except RuntimeFilePlanError as error:
        raise EntrypointError(
            _format_diagnostics(
                "runtime file configuration is invalid", error.diagnostics
            )
        ) from error
    except RuntimeStateError as error:
        raise EntrypointError(f"runtime state failed: {error}") from error
    except RuntimeFileDownloadError as error:
        raise EntrypointError(
            _format_diagnostics("runtime download failed", error.diagnostics)
        ) from error
    except ApplicationError as error:
        raise EntrypointError(f"runtime download failed: {error}") from error


def _activate_runtime_file_plan(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    runtime: ContainerRuntime,
    runtime_downloader: RuntimeDownloadRunner,
    runtime_state_path: Path,
) -> _RuntimeDownloadQueues:
    if not plan.items:
        return _empty_runtime_download_queues()

    now = datetime.now(UTC)
    state = prepare_runtime_state_for_start(
        runtime_state_path,
        active_downloads=True,
        run_id=str(uuid.uuid4()),
        now=now,
    )
    assert state is not None

    reconciliation = reconcile_runtime_file_plan(
        plan,
        state,
        now=now,
        comfyui_path=runtime.comfyui_path,
    )
    _log_runtime_download_reconciliation(reconciliation)
    write_runtime_state(runtime_state_path, reconciliation.state)
    _log_runtime_download_reconciliation_persisted(reconciliation)

    queues = _split_runtime_download_queues(reconciliation.download_plan)
    if not queues.sync_plan.items:
        return queues

    state_writer = _RuntimeDownloadStateWriter(
        runtime_state_path,
        reconciliation.state,
        log=print,
    )
    _call_runtime_downloader(
        runtime_downloader,
        queues.sync_plan,
        config=config,
        log=print,
        state_observer=state_writer,
    )
    return queues


def _empty_runtime_download_queues() -> _RuntimeDownloadQueues:
    empty_plan = RuntimeFilePlan(items=())
    return _RuntimeDownloadQueues(sync_plan=empty_plan, async_plan=empty_plan)


def _log_runtime_download_reconciliation(
    reconciliation: RuntimeFileReconciliation,
) -> None:
    for item in reconciliation.items:
        print(
            "Runtime download reconcile: "
            f"mode={item.item.download_mode} target={item.item.relative_target} "
            f"status={item.status} scheduled={str(item.scheduled).lower()} "
            f"source_host={runtime_source_host(item.item.url)} "
            f"identity={short_runtime_identity(item.digest)}"
        )


def _log_runtime_download_reconciliation_persisted(
    reconciliation: RuntimeFileReconciliation,
) -> None:
    async_items = [
        item for item in reconciliation.items if item.item.download_mode == "async"
    ]
    async_scheduled = sum(1 for item in async_items if item.scheduled)
    async_skipped = len(async_items) - async_scheduled
    print(
        "Runtime download reconciliation persisted: "
        f"entries={len(reconciliation.state.downloads.entries)} "
        f"async_scheduled={async_scheduled} async_skipped={async_skipped} "
        f"stale_entries={len(reconciliation.stale_entry_digests)} "
        f"stale_staging={len(reconciliation.stale_staging_candidates)}"
    )


def _split_runtime_download_queues(
    plan: RuntimeFilePlan,
) -> _RuntimeDownloadQueues:
    return _RuntimeDownloadQueues(
        sync_plan=RuntimeFilePlan(
            items=tuple(item for item in plan.items if item.download_mode == "sync")
        ),
        async_plan=RuntimeFilePlan(
            items=tuple(item for item in plan.items if item.download_mode == "async")
        ),
    )


def _start_runtime_async_download_queue(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    runtime: ContainerRuntime,
    runtime_state_path: Path,
    runtime_async_queue_starter: RuntimeAsyncQueueStarter,
) -> RuntimeAsyncDownloadQueueHandle | None:
    if not plan.items:
        return None
    print(
        "Async runtime download queue scheduled: "
        f"items={len(plan.items)} policy={config.cdh.download_failure_policy}"
    )
    try:
        return runtime_async_queue_starter(
            plan,
            config=config,
            runtime=runtime,
            runtime_state_path=runtime_state_path,
            log=print,
        )
    except RuntimeAsyncQueueStartupError as error:
        raise EntrypointError(
            f"async runtime download queue failed to start: {error}"
        ) from error


def _stop_runtime_async_download_queue(
    handle: RuntimeAsyncDownloadQueueHandle | None,
    *,
    cancel_requested: Callable[[], bool],
    timeout: float = ASYNC_QUEUE_STOP_TIMEOUT_SECONDS,
    poll_interval: float = ASYNC_QUEUE_STOP_POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> bool:
    if handle is None or not handle.is_alive():
        return True

    print("Async runtime download queue stop requested")
    handle.request_stop()
    deadline = monotonic() + timeout
    while handle.is_alive():
        if cancel_requested():
            print(
                "WARNING: Async runtime download queue stop interrupted; "
                "terminating backends"
            )
            handle.terminate_backends()
            return False
        now = monotonic()
        if now >= deadline:
            print(
                "WARNING: Async runtime download queue did not stop in "
                f"{timeout:.1f}s; terminating backends"
            )
            handle.terminate_backends()
            return False
        handle.join(timeout=min(poll_interval, deadline - now))
        if handle.is_alive():
            sleep(0)
    print("Async runtime download queue stopped")
    return True


def _stop_runtime_async_download_queue_for_startup(
    handle: RuntimeAsyncDownloadQueueHandle | None,
    *,
    startup_shutdown: _StartupShutdownState,
) -> bool:
    previous_raise_on_signal = startup_shutdown.raise_on_signal
    startup_shutdown.raise_on_signal = False
    try:
        return _stop_runtime_async_download_queue(
            handle,
            cancel_requested=startup_shutdown.repeated_signal_requested,
        )
    finally:
        startup_shutdown.raise_on_signal = previous_raise_on_signal


def _call_runtime_downloader(
    runtime_downloader: RuntimeDownloadRunner,
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    log: Logger,
    state_observer: RuntimeDownloadStateObserver,
) -> tuple[RuntimeFileDownloadResult, ...]:
    if _runtime_downloader_accepts_state_observer(runtime_downloader):
        return runtime_downloader(
            plan,
            config=config,
            log=log,
            state_observer=state_observer,
        )
    return runtime_downloader(plan, config=config, log=log)


def _runtime_downloader_accepts_state_observer(
    runtime_downloader: RuntimeDownloadRunner,
) -> bool:
    try:
        parameters = signature(runtime_downloader).parameters
    except (TypeError, ValueError):
        return True
    return "state_observer" in parameters or any(
        parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


class _RuntimeDownloadStateWriter:
    """Persist runtime download state transitions."""

    def __init__(
        self,
        path: Path,
        state: RuntimeState,
        *,
        log: Logger | None = None,
    ) -> None:
        self._path = path
        self._state = state
        self._log = log

    def __call__(
        self,
        item: RuntimeFilePlanItem,
        status: RuntimeDownloadObservedStatus,
        *,
        error: object | None = None,
    ) -> None:
        digest = runtime_file_identity_digest(item)
        try:
            entry = self._state.downloads.entries[digest]
        except KeyError as missing:
            raise RuntimeStateError(
                f"runtime download state entry is missing for {item.relative_target}"
            ) from missing

        now = datetime.now(UTC)
        if status == "downloading":
            updated_entry = RuntimeDownloadEntry.model_validate(
                {
                    **entry.model_dump(),
                    "status": "downloading",
                    "attempts": entry.attempts + 1,
                    "attempt_run_id": self._state.run_id,
                    "last_error": None,
                    "updated_at": now,
                }
            )
        elif status in ("failed", "exhausted"):
            updated_entry = failed_runtime_download_entry(
                entry,
                status=status,
                last_error=error,
                updated_at=now,
            )
        else:
            updated_entry = RuntimeDownloadEntry.model_validate(
                {
                    **entry.model_dump(),
                    "status": "completed",
                    "last_error": None,
                    "updated_at": now,
                }
            )

        entries = dict(self._state.downloads.entries)
        entries[digest] = updated_entry
        self._state = RuntimeState(
            schema_version=self._state.schema_version,
            updated_at=now,
            run_id=self._state.run_id,
            downloads=RuntimeDownloadsState(entries=entries),
        )
        write_runtime_state(self._path, self._state)
        if self._log is not None:
            self._log(
                "Runtime download state persisted: "
                f"mode={item.download_mode} target={item.relative_target} "
                f"status={updated_entry.status} attempts={updated_entry.attempts} "
                f"identity={short_runtime_identity(digest)}"
            )


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
            _format_diagnostics("runtime hook failed", error.diagnostics)
        ) from error
    finally:
        startup_shutdown.raise_on_signal = True


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
        _terminate_child_if_running(child)
        raise EntrypointError(
            _format_diagnostics("runtime hook failed", error.diagnostics)
        ) from error
    finally:
        startup_shutdown.raise_on_signal = True


def _terminate_child_if_running(child: ChildProcess) -> None:
    if _reap_child_if_exited(child):
        return
    with suppress(OSError):
        child.terminate()
    _reap_child_until_exited(child)


def _reap_child_if_exited(child: ChildProcess) -> bool:
    if child.poll() is None:
        return False
    with suppress(OSError):
        child.wait()
    return True


def _reap_child_until_exited(child: ChildProcess) -> None:
    deadline = time.monotonic() + CHILD_TERMINATION_REAP_GRACE_SECONDS
    while True:
        if _reap_child_if_exited(child):
            return
        now = time.monotonic()
        if now >= deadline:
            return
        time.sleep(min(CHILD_REAP_POLL_INTERVAL_SECONDS, deadline - now))


class _StartupShutdownRequested(BaseException):
    """A normal shutdown signal received before startup completed."""

    def __init__(self, sig: signal.Signals) -> None:
        self.sig = sig
        super().__init__(sig.name)


class _StartupShutdownState:
    """Signal state used while startup work is still cancellable."""

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
def _startup_shutdown_signal_handlers(
    state: _StartupShutdownState,
):
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


def _normalize_signal_exit_code(sig: signal.Signals) -> int:
    return _normalize_child_exit_code(-int(sig))


def _forward_startup_shutdown_to_child(
    child: ChildProcess,
    sig: signal.Signals,
) -> int:
    if child.poll() is None:
        child.send_signal(sig)
    return _normalize_child_exit_code(child.wait())


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
    async_handle: RuntimeAsyncDownloadQueueHandle | None = None,
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
            _stop_runtime_async_download_queue(
                async_handle,
                cancel_requested=shutdown_state.stop_hooks_cancelled,
            )
            if not shutdown_state.stop_hooks_cancelled():
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
