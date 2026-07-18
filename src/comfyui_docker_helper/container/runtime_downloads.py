"""Runtime download activation and asynchronous worker ownership."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

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

ASYNC_QUEUE_STOP_TIMEOUT_SECONDS = 5.0
ASYNC_QUEUE_STOP_POLL_INTERVAL_SECONDS = 0.05


class RuntimeDownloadRunner(Protocol):
    """Runtime file downloader used by the synchronous component path."""

    def __call__(
        self,
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: RuntimeDownloadStateObserver | None = None,
    ) -> tuple[RuntimeFileDownloadResult, ...]: ...


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
    ) -> RuntimeAsyncDownloadQueueHandle: ...


class RuntimeAsyncDownloadQueueHandle(Protocol):
    """Operations required to stop and join the asynchronous queue."""

    def request_stop(self) -> None: ...

    def terminate_backends(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


class RuntimeAsyncQueueStartupError(RuntimeError):
    """Async queue infrastructure failed before accepting downloads."""


@dataclass(frozen=True, slots=True)
class _PreparedRuntimeDownloads:
    """Asynchronous work admitted after synchronous activation completes."""

    async_plan: RuntimeFilePlan
    run_id: str | None


@dataclass(frozen=True, slots=True)
class _RuntimeAsyncDownloadQueueHandle:
    thread: threading.Thread
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
        self._async_handle: RuntimeAsyncDownloadQueueHandle | None = None

    def activate(self) -> None:
        """Reconcile desired files and complete the synchronous queue."""
        plan = build_runtime_file_plan(
            ({"files": list(self._files)},),
            comfyui_path=self._runtime.comfyui_path,
            default_download_mode=self._config.cdh.default_download_mode,
        )
        self._prepared = _activate_runtime_file_plan(
            plan,
            config=self._config,
            runtime=self._runtime,
            runtime_downloader=self._downloader,
            runtime_state_path=self._runtime_state_path,
            log=self._log,
        )

    def start_async(self) -> None:
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
        self._async_handle = self._async_queue_starter(
            plan,
            config=self._config,
            runtime=self._runtime,
            runtime_state_path=self._runtime_state_path,
            expected_run_id=self._prepared.run_id,
            log=self._log,
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


def start_runtime_async_download_queue(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    runtime: ContainerRuntime,
    runtime_state_path: Path,
    expected_run_id: str,
    log: Logger,
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
            store.close()
            startup_finished.set()

    try:
        thread = threading.Thread(
            target=worker,
            name="cdh-runtime-async-downloads",
            daemon=True,
        )
        thread.start()
    except Exception as error:
        store.close()
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
        stop_requested=stop_requested,
        backends=backends,
        backends_lock=backends_lock,
    )


def _activate_runtime_file_plan(
    plan: RuntimeFilePlan,
    *,
    config: RuntimeConfig,
    runtime: ContainerRuntime,
    runtime_downloader: RuntimeDownloadRunner,
    runtime_state_path: Path,
    log: Logger,
) -> _PreparedRuntimeDownloads:
    now = datetime.now(UTC)
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
            now=now,
        )
        if state is None:
            return _empty_prepared_runtime_downloads()

        reconciliation = reconcile_runtime_file_plan(
            plan,
            state,
            now=now,
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
        )
        return prepared
    finally:
        store.close()


def _empty_prepared_runtime_downloads() -> _PreparedRuntimeDownloads:
    return _PreparedRuntimeDownloads(
        async_plan=RuntimeFilePlan(items=()),
        run_id=None,
    )


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
    for digest in sorted(reconciliation.cleanup_pending_digests):
        entry = reconciliation.state.downloads.entries[digest]
        log(
            "WARNING: Runtime stale download cleanup remains pending: "
            f"target={entry.target} identity={short_runtime_identity(digest)} "
            f"reason={runtime_error_reason(entry.last_error)}"
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
        f"entries={len(reconciliation.state.downloads.entries)} "
        f"async_scheduled={async_scheduled} async_skipped={async_skipped} "
        f"stale_entries={len(reconciliation.stale_entry_digests)} "
        f"cleanup_pending={len(reconciliation.cleanup_pending_digests)}"
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
    """Cooperatively stop one owned queue and bound backend termination."""
    if handle is None or not handle.is_alive():
        return True

    log("Async runtime download queue stop requested")
    handle.request_stop()
    deadline = monotonic() + timeout
    while handle.is_alive():
        if cancel_requested():
            log(
                "WARNING: Async runtime download queue stop interrupted; "
                "terminating backends"
            )
            handle.terminate_backends()
            _join_after_backend_termination(
                handle,
                timeout=timeout,
                poll_interval=poll_interval,
                monotonic=monotonic,
                sleep=sleep,
                log=log,
            )
            return False
        now = monotonic()
        if now >= deadline:
            log(
                "WARNING: Async runtime download queue did not stop in "
                f"{timeout:.1f}s; terminating backends"
            )
            handle.terminate_backends()
            _join_after_backend_termination(
                handle,
                timeout=timeout,
                poll_interval=poll_interval,
                monotonic=monotonic,
                sleep=sleep,
                log=log,
            )
            return False
        handle.join(timeout=min(poll_interval, deadline - now))
        if handle.is_alive():
            sleep(0)
    log("Async runtime download queue stopped")
    return True


def _join_after_backend_termination(
    handle: RuntimeAsyncDownloadQueueHandle,
    *,
    timeout: float,
    poll_interval: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
    log: Logger,
) -> None:
    deadline = monotonic() + min(timeout, poll_interval)
    while handle.is_alive():
        now = monotonic()
        if now >= deadline:
            log(
                "WARNING: Async runtime download queue remained alive after "
                "backend termination"
            )
            return
        handle.join(timeout=min(poll_interval, deadline - now))
        if handle.is_alive():
            sleep(0)
