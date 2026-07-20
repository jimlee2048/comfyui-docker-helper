"""Runtime download activation and asynchronous worker ownership."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_diagnostics import (
    runtime_error_reason,
    runtime_source_host,
    short_runtime_identity,
)
from comfyui_docker_helper.container.runtime_download_state import (
    RuntimeDownloadStateWriter,
)
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeDownloadStateObserver,
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
    RuntimeFileReconciliation,
    build_runtime_file_plan,
    download_runtime_files,
    reconcile_runtime_file_plan,
    validate_runtime_file_state_plan,
)
from comfyui_docker_helper.container.runtime_state import (
    RuntimeStateError,
    RuntimeStateStore,
    prepare_runtime_state_for_start,
)
from comfyui_docker_helper.container.transfer_core import CancellableDownloadBackend

ASYNC_QUEUE_STOP_TIMEOUT_SECONDS = 5.0
ASYNC_QUEUE_STOP_POLL_INTERVAL_SECONDS = 0.05


class RuntimeDownloadRunner(Protocol):
    """Runtime file downloader used by one owned runtime operation."""

    def __call__(
        self,
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: RuntimeDownloadStateObserver | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        backend_observer: Callable[[CancellableDownloadBackend], None] | None = None,
    ) -> tuple[RuntimeFileDownloadResult, ...]: ...


@runtime_checkable
class ForceEscalationCancellation(Protocol):
    """Cancellation source that exposes repeated-signal force escalation."""

    def force_requested(self) -> bool: ...


@runtime_checkable
class DeadlineBoundCancellation(Protocol):
    """Cancellation source that exposes the owning absolute deadline."""

    def shutdown_deadline(self) -> float | None: ...


@runtime_checkable
class WakeableForceCancellation(Protocol):
    """Cancellation source that wakes when force escalation is requested."""

    def wait_for_force(self, timeout: float) -> object: ...


class RuntimeAsyncQueueStarter(Protocol):
    """Start the single asynchronous runtime download queue."""

    def __call__(
        self,
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        expected_run_id: str,
        log: Logger,
        handle_observer: Callable[[RuntimeAsyncDownloadQueueHandle], None],
        cancel_requested: Callable[[], bool],
    ) -> RuntimeAsyncDownloadQueueHandle: ...


class RuntimeAsyncDownloadQueueHandle(Protocol):
    """Operations required to stop and join the asynchronous queue."""

    def request_stop(self) -> None: ...

    def terminate_backends(self) -> None: ...

    def request_backend_termination(self, *, deadline: float | None) -> None: ...

    def backend_termination_is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...

    def wait_until_stopped(
        self,
        *,
        timeout: float,
        poll_interval: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> bool: ...


class RuntimeAsyncQueueStartupError(RuntimeError):
    """Async queue infrastructure failed before accepting downloads."""


@dataclass(frozen=True, slots=True)
class _PreparedRuntimeDownloads:
    """Asynchronous work admitted after synchronous activation completes."""

    async_plan: RuntimeFilePlan
    run_id: str | None


@dataclass(slots=True)
class _RuntimeDownloadOperationHandle:
    thread: threading.Thread
    stop_requested: threading.Event
    backends: list[CancellableDownloadBackend]
    backends_lock: threading.Lock
    backend_termination_thread: threading.Thread | None = None

    def request_stop(self) -> None:
        self.stop_requested.set()

    def terminate_backends(self) -> None:
        with self.backends_lock:
            backends = tuple(self.backends)
        for backend in backends:
            with suppress(Exception):
                backend.force_cancel()

    def request_backend_termination(self, *, deadline: float | None) -> None:
        """Start backend cancellation once without blocking the lifecycle owner."""
        with self.backends_lock:
            if self.backend_termination_thread is not None:
                return
            backends = tuple(self.backends)

            def terminate() -> None:
                for backend in backends:
                    with suppress(Exception):
                        backend.cancel(deadline=deadline)

            thread = threading.Thread(
                target=terminate,
                name="cdh-runtime-download-cancellation",
                daemon=True,
            )
            self.backend_termination_thread = thread
            thread.start()

    def backend_termination_is_alive(self) -> bool:
        thread = self.backend_termination_thread
        if thread is None:
            return False
        thread.join(timeout=0.0)
        return thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def wait_until_stopped(
        self,
        *,
        timeout: float,
        poll_interval: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> bool:
        deadline = monotonic() + timeout
        while True:
            self.thread.join(timeout=0.0)
            worker_alive = self.thread.is_alive()
            cancellation_alive = self.backend_termination_is_alive()
            if not worker_alive and not cancellation_alive:
                return True
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            if worker_alive:
                self.thread.join(timeout=min(poll_interval, remaining))
            else:
                cancellation = self.backend_termination_thread
                assert cancellation is not None
                cancellation.join(timeout=min(poll_interval, remaining))


class RuntimeDownloads:
    """Own runtime file activation and the lifetime of its async worker."""

    def __init__(
        self,
        config: RuntimeConfig,
        files: tuple[Mapping[str, Any], ...],
        *,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        downloader: RuntimeDownloadRunner = download_runtime_files,
        async_queue_starter: RuntimeAsyncQueueStarter | None = None,
        log: Logger = print,
    ) -> None:
        self._config = config
        self._files = files
        self._runtime = runtime
        self._runtime_state_path = runtime_state_path
        self._downloader = downloader
        self._async_queue_starter = (
            start_runtime_async_download_queue
            if async_queue_starter is None
            else async_queue_starter
        )
        self._log = log
        self._prepared = _empty_prepared_runtime_downloads()
        self._sync_handle: _RuntimeDownloadOperationHandle | None = None
        self._async_handle: RuntimeAsyncDownloadQueueHandle | None = None

    def activate(
        self,
        *,
        cancel_requested: Callable[[], bool] = lambda: False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Reconcile files and run the synchronous queue as one owned operation."""
        plan = build_runtime_file_plan(
            self._files,
            comfyui_path=self._runtime.comfyui_path,
            default_download_mode=self._config.cdh.default_download_mode,
        )
        stop_requested = threading.Event()
        backends: list[CancellableDownloadBackend] = []
        backends_lock = threading.Lock()
        result: list[_PreparedRuntimeDownloads] = []
        errors: list[BaseException] = []

        def observe_backend(backend: CancellableDownloadBackend) -> None:
            with backends_lock:
                if not any(owned is backend for owned in backends):
                    backends.append(backend)

        def worker() -> None:
            try:
                result.append(
                    _activate_runtime_file_plan(
                        plan,
                        config=self._config,
                        runtime=self._runtime,
                        runtime_downloader=self._downloader,
                        runtime_state_path=self._runtime_state_path,
                        log=self._log,
                        cancel_requested=stop_requested.is_set,
                        backend_observer=observe_backend,
                    )
                )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(
            target=worker,
            name="cdh-runtime-sync-downloads",
            daemon=True,
        )
        handle = _RuntimeDownloadOperationHandle(
            thread=thread,
            stop_requested=stop_requested,
            backends=backends,
            backends_lock=backends_lock,
        )
        self._sync_handle = handle
        thread.start()
        while handle.is_alive():
            if cancel_requested():
                _cancel_sync_operation(
                    handle,
                    cancel_requested=cancel_requested,
                    monotonic=monotonic,
                )
                return
            handle.join(timeout=ASYNC_QUEUE_STOP_POLL_INTERVAL_SECONDS)
        if cancel_requested():
            _cancel_sync_operation(
                handle,
                cancel_requested=cancel_requested,
                monotonic=monotonic,
            )
            return
        if errors:
            raise errors[0]
        if len(result) != 1:
            raise RuntimeAsyncQueueStartupError(
                "synchronous runtime download result is missing"
            )
        self._prepared = result[0]

    def start_async(self, *, cancel_requested: Callable[[], bool]) -> None:
        """Start the prepared asynchronous queue after startup admission."""
        plan = self._prepared.async_plan
        if not plan.items:
            return
        if self._prepared.run_id is None:
            raise RuntimeAsyncQueueStartupError(
                "async runtime download generation is missing"
            )
        self._log(
            "Async runtime download queue scheduled: "
            f"items={len(plan.items)} "
            f"policy={self._config.cdh.download_failure_policy}"
        )

        def own_handle(handle: RuntimeAsyncDownloadQueueHandle) -> None:
            if self._async_handle is not None and self._async_handle is not handle:
                raise RuntimeAsyncQueueStartupError(
                    "async runtime download queue changed ownership"
                )
            self._async_handle = handle

        handle = self._async_queue_starter(
            plan,
            config=self._config,
            runtime=self._runtime,
            runtime_state_path=self._runtime_state_path,
            expected_run_id=self._prepared.run_id,
            log=self._log,
            handle_observer=own_handle,
            cancel_requested=cancel_requested,
        )
        if self._async_handle is not handle:
            raise RuntimeAsyncQueueStartupError(
                "async runtime download queue was not published before startup"
            )

    def stop(
        self,
        *,
        cancel_requested: Callable[[], bool],
        timeout: float = ASYNC_QUEUE_STOP_TIMEOUT_SECONDS,
        poll_interval: float = ASYNC_QUEUE_STOP_POLL_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = time.sleep,
    ) -> bool:
        """Cooperatively stop the queue, then terminate active backends."""
        return stop_runtime_async_download_queue(
            self._async_handle,
            cancel_requested=cancel_requested,
            timeout=timeout,
            poll_interval=poll_interval,
            monotonic=monotonic,
            sleep=sleep,
            log=self._log,
        )

    def request_stop(self, *, deadline: float | None = None) -> None:
        """Promptly cancel accepted sync/async work without waiting."""
        for handle in self._owned_handles():
            if not handle.is_alive():
                continue
            self._log("Runtime download operation stop requested")
            handle.request_stop()
            handle.request_backend_termination(deadline=deadline)

    def is_stopped(self) -> bool:
        """Observe nonblocking joins and report complete worker quiescence."""
        for handle in self._owned_handles():
            if handle.is_alive():
                handle.join(timeout=0.0)
        return all(
            not handle.is_alive() and not handle.backend_termination_is_alive()
            for handle in self._owned_handles()
        )

    def request_force_stop(self) -> None:
        """Request immediate force cancellation for every owned operation."""
        for handle in self._owned_handles():
            handle.request_stop()
            handle.request_backend_termination(deadline=None)
            handle.terminate_backends()

    def _owned_handles(self) -> tuple[RuntimeAsyncDownloadQueueHandle, ...]:
        return tuple(
            handle
            for handle in (self._sync_handle, self._async_handle)
            if handle is not None
        )


def start_runtime_async_download_queue(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    runtime: ContainerRuntime,
    runtime_state_path: Path,
    expected_run_id: str,
    log: Logger,
    handle_observer: Callable[[RuntimeAsyncDownloadQueueHandle], None],
    cancel_requested: Callable[[], bool],
) -> RuntimeAsyncDownloadQueueHandle:
    """Start one cdh-managed background queue for async runtime files."""
    store: RuntimeStateStore | None = None
    try:
        store = RuntimeStateStore.open(runtime_state_path, create_parent=False)
        if store is None:
            raise RuntimeStateError("runtime state is missing")
        state = store.read()
        if state is None:
            raise RuntimeStateError("runtime state is missing")
        validate_runtime_file_state_plan(
            plan,
            state,
            comfyui_path=runtime.comfyui_path,
            default_downloader=config.cdh.default_downloader,
            expected_run_id=expected_run_id,
        )
    except Exception as error:
        if store is not None:
            store.close()
        raise RuntimeAsyncQueueStartupError(str(error)) from error

    state_writer = RuntimeDownloadStateWriter(store, state, log=log)
    accepted = threading.Event()
    stop_requested = threading.Event()
    startup_finished = threading.Event()
    startup_error: list[BaseException] = []
    backends: list[CancellableDownloadBackend] = []
    backends_lock = threading.Lock()

    def accept_queue() -> None:
        accepted.set()

    def observe_backend(backend: CancellableDownloadBackend) -> None:
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
            store.close()
            startup_finished.set()

    thread: threading.Thread | None = None
    try:
        thread = threading.Thread(
            target=worker,
            name="cdh-runtime-async-downloads",
            daemon=True,
        )
        handle = _RuntimeDownloadOperationHandle(
            thread=thread,
            stop_requested=stop_requested,
            backends=backends,
            backends_lock=backends_lock,
        )
        handle_observer(handle)
        thread.start()
    except BaseException as error:
        if thread is None or thread.ident is None:
            store.close()
        if not isinstance(error, Exception):
            raise
        raise RuntimeAsyncQueueStartupError(str(error)) from error

    while not accepted.is_set():
        if cancel_requested():
            return handle
        if startup_finished.wait(0.01):
            break
    if not accepted.is_set():
        error = startup_error[0] if startup_error else RuntimeError("worker exited")
        raise RuntimeAsyncQueueStartupError(str(error)) from error

    log(f"Async runtime download queue accepted: items={len(plan.items)}")
    return handle


def _activate_runtime_file_plan(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    runtime: ContainerRuntime,
    runtime_downloader: RuntimeDownloadRunner,
    runtime_state_path: Path,
    log: Logger,
    cancel_requested: Callable[[], bool],
    backend_observer: Callable[[CancellableDownloadBackend], None],
) -> _PreparedRuntimeDownloads:
    store = RuntimeStateStore.open(
        runtime_state_path,
        create_parent=bool(plan.items),
    )
    if store is None:
        return _empty_prepared_runtime_downloads()
    try:
        run_id = str(uuid.uuid4())
        state = prepare_runtime_state_for_start(
            store,
            desired_downloads=bool(plan.items),
            run_id=run_id,
        )
        if state is None:
            return _empty_prepared_runtime_downloads()

        reconciliation = reconcile_runtime_file_plan(
            plan,
            state,
            comfyui_path=runtime.comfyui_path,
            default_downloader=config.cdh.default_downloader,
            resume_download=config.cdh.downloader.aria2.resume_download,
        )
        _log_runtime_download_reconciliation(reconciliation, log=log)
        store.write(reconciliation.state)
        _log_runtime_download_reconciliation_persisted(reconciliation, log=log)

        sync_plan, prepared = _split_runtime_download_queues(
            reconciliation.download_plan,
            run_id=run_id,
        )
        if not sync_plan.items:
            return prepared

        state_writer = RuntimeDownloadStateWriter(
            store,
            reconciliation.state,
            log=log,
        )
        runtime_downloader(
            sync_plan,
            config=config,
            log=log,
            state_observer=state_writer,
            cancel_requested=cancel_requested,
            backend_observer=backend_observer,
        )
        return prepared
    finally:
        store.close()


def _empty_prepared_runtime_downloads() -> _PreparedRuntimeDownloads:
    return _PreparedRuntimeDownloads(
        async_plan=RuntimeFilePlan(items=()),
        run_id=None,
    )


def _force_requested(cancel_requested: Callable[[], bool]) -> bool:
    return (
        isinstance(cancel_requested, ForceEscalationCancellation)
        and cancel_requested.force_requested()
    )


def _cancel_sync_operation(
    handle: _RuntimeDownloadOperationHandle,
    *,
    cancel_requested: Callable[[], bool],
    monotonic: Callable[[], float],
) -> None:
    """Cancel one sync operation against one absolute component boundary."""
    deadline = monotonic() + ASYNC_QUEUE_STOP_TIMEOUT_SECONDS
    if isinstance(cancel_requested, DeadlineBoundCancellation):
        outer_deadline = cancel_requested.shutdown_deadline()
        if outer_deadline is not None:
            deadline = min(deadline, outer_deadline)
    handle.request_stop()
    handle.request_backend_termination(deadline=deadline)
    forced = False
    while not handle.wait_until_stopped(
        timeout=0.0,
        poll_interval=ASYNC_QUEUE_STOP_POLL_INTERVAL_SECONDS,
        monotonic=monotonic,
    ):
        now = monotonic()
        if _force_requested(cancel_requested) or now >= deadline:
            handle.terminate_backends()
            forced = True
        delay = ASYNC_QUEUE_STOP_POLL_INTERVAL_SECONDS
        if not forced:
            delay = min(delay, max(0.0, deadline - now))
        if isinstance(cancel_requested, WakeableForceCancellation):
            cancel_requested.wait_for_force(delay)
        else:
            time.sleep(delay)


def _split_runtime_download_queues(
    plan: RuntimeFilePlan,
    *,
    run_id: str,
) -> tuple[RuntimeFilePlan, _PreparedRuntimeDownloads]:
    return (
        RuntimeFilePlan(
            items=tuple(item for item in plan.items if item.download_mode == "sync")
        ),
        _PreparedRuntimeDownloads(
            async_plan=RuntimeFilePlan(
                items=tuple(
                    item for item in plan.items if item.download_mode == "async"
                )
            ),
            run_id=run_id,
        ),
    )


def _log_runtime_download_reconciliation(
    reconciliation: RuntimeFileReconciliation,
    *,
    log: Logger,
) -> None:
    for item in reconciliation.items:
        log(
            "Runtime download reconcile: "
            f"mode={item.item.download_mode} target={item.item.relative_target} "
            f"status={item.status} scheduled={str(item.scheduled).lower()} "
            f"source_host={runtime_source_host(item.item.url)} "
            f"identity={short_runtime_identity(item.digest)}"
        )
    for pending in reconciliation.cleanup_pending:
        entry = reconciliation.state.downloads[pending.digest]
        log(
            "WARNING: Runtime stale download cleanup remains pending: "
            f"target={entry.target} "
            f"identity={short_runtime_identity(pending.digest)} "
            f"reason={runtime_error_reason(pending.reason)}"
        )


def _log_runtime_download_reconciliation_persisted(
    reconciliation: RuntimeFileReconciliation,
    *,
    log: Logger,
) -> None:
    async_items = [
        item for item in reconciliation.items if item.item.download_mode == "async"
    ]
    async_scheduled = sum(1 for item in async_items if item.scheduled)
    async_skipped = len(async_items) - async_scheduled
    log(
        "Runtime download reconciliation persisted: "
        f"entries={len(reconciliation.state.downloads)} "
        f"async_scheduled={async_scheduled} async_skipped={async_skipped} "
        f"stale_entries={len(reconciliation.stale_entry_digests)} "
        f"cleanup_pending={len(reconciliation.cleanup_pending)}"
    )


def stop_runtime_async_download_queue(
    handle: RuntimeAsyncDownloadQueueHandle | None,
    *,
    cancel_requested: Callable[[], bool],
    timeout: float = ASYNC_QUEUE_STOP_TIMEOUT_SECONDS,
    poll_interval: float = ASYNC_QUEUE_STOP_POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    log: Logger = print,
) -> bool:
    """Stop one owned queue against one absolute component boundary."""
    if handle is None or not handle.is_alive():
        return True

    log("Async runtime download queue stop requested")
    handle.request_stop()
    deadline = monotonic() + timeout
    forced = False
    while handle.is_alive() or handle.backend_termination_is_alive():
        now = monotonic()
        if not forced and (cancel_requested() or now >= deadline):
            reason = "interrupted" if cancel_requested() else f"exceeded {timeout:.1f}s"
            log(f"WARNING: Async runtime download queue {reason}; forcing backends")
            handle.request_backend_termination(deadline=deadline)
            handle.terminate_backends()
            forced = True
        delay = poll_interval if forced else min(poll_interval, deadline - now)
        handle.join(timeout=max(0.0, delay))
        if handle.is_alive() or handle.backend_termination_is_alive():
            sleep(0)
    log("Async runtime download queue stopped")
    return not forced
