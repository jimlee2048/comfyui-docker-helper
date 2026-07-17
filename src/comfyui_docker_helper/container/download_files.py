"""Container-side file transport adapters and build orchestration."""

from __future__ import annotations

import asyncio
import email.utils
import secrets
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from types import TracebackType
from typing import Literal, Protocol

import aria2p
import httpx

from comfyui_docker_helper.config.build_plan import FilesPhase
from comfyui_docker_helper.config.url_validation import (
    DownloaderName,
    require_downloader_name,
)
from comfyui_docker_helper.container.attempt_coordinator import (
    AttemptCancelled,
    AttemptExhausted,
    AttemptOrdinaryTerminal,
    AttemptSucceeded,
    coordinate_transfer_attempts,
)
from comfyui_docker_helper.container.transfer_core import (
    Aria2DownloadSettings,
    DownloadBackend,
    DownloadCancelled,
    DownloaderSettings,
    DownloadFilesError,
    DownloadStatus,
    FileTransferOutcome,
    FileTransferRequest,
    HttpxDownloadSettings,
    Logger,
    StagingDisposition,
    TransportCancelled,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportOutcome,
    TransportRequest,
    TransportResumeRejected,
    TransportRetryable,
    TransportSink,
    TransportSuccess,
    verify_required_final,
)


@dataclass(frozen=True, slots=True)
class FileDownloadItem:
    """One resolved build-time file request."""

    url: str
    filename: str
    target: Path
    overwrite: bool
    downloader: DownloaderName
    checksum: str | None = None


@dataclass(frozen=True, slots=True)
class FileDownloadPlan:
    """Ordered required build files and transport settings."""

    comfyui_root: Path
    downloader: DownloaderSettings
    items: tuple[FileDownloadItem, ...]
    download_max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """One required build-file outcome with expected/observed evidence."""

    item: FileDownloadItem
    outcome: FileTransferOutcome

    @property
    def status(self) -> DownloadStatus:
        return self.outcome.status


class DownloadBackendPreparer(Protocol):
    """Optional backend hook for startup work before downloads begin."""

    def prepare(self, settings: DownloaderSettings) -> None: ...


class HttpxDownloader:
    """HTTPX adapter that writes one response to supplied staging."""

    chunk_size = 1024 * 1024
    progress_interval_seconds = 5.0

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Monotonic = time.monotonic,
        wall_clock: Monotonic = time.time,
        log: Logger = print,
    ) -> None:
        self._transport = transport
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._log = log
        self._cancel_requested = threading.Event()
        self._active_lock = threading.Lock()
        self._active: (
            tuple[
                asyncio.AbstractEventLoop,
                asyncio.Task[TransportOutcome],
            ]
            | None
        ) = None

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        """Write HTTP attempts to the exact core-owned staging inode."""
        return asyncio.run(self._download(request, settings))

    async def _download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        task = asyncio.current_task()
        if task is None:
            raise DownloadFilesError("HTTP download task could not be identified")
        loop = asyncio.get_running_loop()
        with self._active_lock:
            if self._cancel_requested.is_set():
                return _transport_cancelled("httpx")
            self._active = (loop, task)
        try:
            try:
                outcome = await self._download_once(request, settings)
            except OSError as error:
                raise DownloadFilesError(
                    "HTTP download failed while writing supplied staging "
                    f"{request.sink.display_path}: {error}"
                ) from error
            # This lock acquisition is the terminal linearization point. A cancel
            # observed first wins; after this point the task does not suspend again.
            with self._active_lock:
                if self._cancel_requested.is_set():
                    return _transport_cancelled("httpx")
            return outcome
        except asyncio.CancelledError as error:
            if self._cancel_requested.is_set():
                return _transport_cancelled("httpx")
            raise DownloadFilesError(
                "HTTP download task was cancelled without a cdh cancellation request"
            ) from error
        finally:
            with self._active_lock:
                if self._active == (loop, task):
                    self._active = None

    async def _download_once(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        timeout = httpx.Timeout(settings.httpx.timeout)
        try:
            async with (
                httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=timeout,
                    transport=self._transport,
                ) as client,
                client.stream("GET", request.url) as response,
            ):
                failure = _http_failure_outcome(
                    response,
                    wall_clock=self._wall_clock,
                )
                if failure is not None:
                    return failure
                length = await self._write_response(response, request.sink)
                return TransportSuccess(
                    length=length,
                    namespace="httpx",
                    http_status=response.status_code,
                )
        except httpx.TooManyRedirects:
            return TransportOrdinaryTerminal(
                diagnostic=TransportDiagnostic(
                    namespace="httpx",
                    summary=f"HTTP download exceeded redirect limits: {request.url}",
                ),
                http_status=None,
            )
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProxyError,
            httpx.RemoteProtocolError,
        ) as error:
            if self._cancel_requested.is_set():
                return _transport_cancelled("httpx")
            return TransportRetryable(
                diagnostic=TransportDiagnostic(
                    namespace="httpx",
                    summary=f"HTTP transport failed for {request.url}: {error}",
                )
            )
        except (httpx.TransportError, httpx.RequestError) as error:
            raise DownloadFilesError(
                f"HTTP transport invariant failed for {request.url}: {error}"
            ) from error

    async def _write_response(
        self,
        response: httpx.Response,
        sink: TransportSink,
    ) -> int:
        downloaded = 0
        last_log = self._monotonic()
        with sink.open_for_write() as output:
            async for chunk in response.aiter_bytes(chunk_size=self.chunk_size):
                if self._cancel_requested.is_set():
                    raise asyncio.CancelledError
                if not chunk:
                    continue
                output.write(chunk)
                downloaded += len(chunk)
                now = self._monotonic()
                if now - last_log >= self.progress_interval_seconds:
                    self._log(f"Downloaded {downloaded} bytes to {sink.display_path}")
                    last_log = now
        return downloaded

    def cancel(self) -> None:
        with self._active_lock:
            if self._cancel_requested.is_set():
                return
            self._cancel_requested.set()
            active = self._active
        if active is not None:
            loop, task = active
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)


class _Aria2LifecycleState(Enum):
    NEW = auto()
    STARTING = auto()
    READY = auto()
    CLOSING = auto()
    CLEANUP_FAILED = auto()
    CLOSED = auto()


class Aria2Downloader:
    """aria2 RPC adapter that writes only to supplied staging."""

    startup_timeout_seconds = 10.0
    poll_interval_seconds = 0.5
    rpc_timeout_seconds = 5.0
    shutdown_timeout_seconds = 5.0

    def __init__(
        self,
        *,
        process_factory: ProcessFactory = subprocess.Popen,
        client_factory: Aria2ClientFactory = aria2p.Client,
        api_factory: Aria2ApiFactory = aria2p.API,
        secret_factory: SecretFactory = lambda: secrets.token_urlsafe(32),
        cancel_wait: CancellationWait | None = None,
        monotonic: Monotonic = time.monotonic,
        log: Logger = print,
    ) -> None:
        self._process_factory = process_factory
        self._client_factory = client_factory
        self._api_factory = api_factory
        self._secret_factory = secret_factory
        self._monotonic = monotonic
        self._log = log
        self._process: Aria2Process | None = None
        self._client: Aria2Client | None = None
        self._api: Aria2Api | None = None
        self._started_settings: Aria2DownloadSettings | None = None
        self._state = _Aria2LifecycleState.NEW
        self._close_requested = False
        self._lifecycle = threading.Condition(threading.RLock())
        self._teardown_generation = 0
        self._completed_teardown_generation = 0
        self._teardown_error: BaseException | None = None
        self._cancel_requested = threading.Event()
        self._cancel_wait = cancel_wait or self._cancel_requested.wait

    def __enter__(self) -> Aria2Downloader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def prepare(self, settings: DownloaderSettings) -> None:
        self._raise_prepare_cancelled()
        self._ensure_started(settings)

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        if self._cancel_requested.is_set():
            self._require_cancelled_daemon_quiescence()
            return _transport_cancelled("aria2")
        try:
            api = self._ensure_started(settings)
        except DownloadCancelled:
            self._require_cancelled_daemon_quiescence()
            return _transport_cancelled("aria2")
        options = _aria2_options(request, settings.aria2)
        try:
            download = api.add_uris([request.url], options=options)
        except Exception as error:
            if self._cancel_requested.is_set():
                self._require_cancelled_daemon_quiescence()
                return _transport_cancelled("aria2")
            self._reap_unquiescent_item(error)
            raise DownloadFilesError(
                f"aria2 RPC submit failed for {request.url}: {error}"
            ) from error
        try:
            transport = self._wait_for_download(
                download,
                request,
                resumed=(
                    settings.aria2.resume_download and request.sink.resume_allowed
                ),
            )
        except Exception as error:
            self._reap_unquiescent_item(error)
            raise
        if transport is not None:
            if isinstance(transport, TransportCancelled):
                self._require_cancelled_daemon_quiescence()
            return transport
        try:
            length = request.sink.current_length()
        except DownloadFilesError:
            raise
        except OSError as error:
            raise DownloadFilesError(
                f"aria2 supplied staging cannot be inspected: {error}"
            ) from error
        return TransportSuccess(length=length, namespace="aria2", http_status=None)

    def close(self) -> None:
        self._close_until(self._monotonic() + self.shutdown_timeout_seconds)

    def _close_until(self, deadline: float) -> None:
        waiting_generation: int | None = None
        with self._lifecycle:
            self._close_requested = True
            self._lifecycle.notify_all()
            while True:
                if self._state is _Aria2LifecycleState.STARTING:
                    self._wait_for_lifecycle_change(deadline)
                    continue
                if self._state is _Aria2LifecycleState.CLOSING:
                    waiting_generation = self._teardown_generation
                    self._wait_for_lifecycle_change(deadline)
                    if (
                        self._completed_teardown_generation == waiting_generation
                        and self._state is not _Aria2LifecycleState.CLOSING
                    ):
                        if self._teardown_error is not None:
                            raise self._teardown_error
                        return
                    continue
                if self._process is None:
                    self._api = None
                    self._client = None
                    self._started_settings = None
                    self._state = _Aria2LifecycleState.CLOSED
                    self._teardown_error = None
                    self._lifecycle.notify_all()
                    return
                process = self._process
                client = self._client
                self._api = None
                self._state = _Aria2LifecycleState.CLOSING
                self._teardown_generation += 1
                generation = self._teardown_generation
                self._teardown_error = None
                self._lifecycle.notify_all()
                break

        cleanup_error: BaseException | None = None
        try:
            self._shutdown_process(client, process, deadline=deadline)
        except BaseException as error:
            cleanup_error = error
        finally:
            cleanup_error = self._publish_teardown_result(
                process,
                generation=generation,
                error=cleanup_error,
            )
        if cleanup_error is not None:
            raise cleanup_error

    def _publish_teardown_result(
        self,
        process: Aria2Process,
        *,
        generation: int,
        error: BaseException | None,
    ) -> BaseException | None:
        with self._lifecycle:
            if self._process is not process and error is None:
                error = DownloadFilesError(
                    "aria2 lifecycle lost the exact child during teardown"
                )
            if error is None:
                self._process = None
                self._client = None
                self._api = None
                self._started_settings = None
                self._state = _Aria2LifecycleState.CLOSED
            else:
                # Retain the exact child and client so a later close can retry reap.
                self._state = _Aria2LifecycleState.CLEANUP_FAILED
            self._completed_teardown_generation = generation
            self._teardown_error = error
            self._lifecycle.notify_all()
        return error

    def cancel(self) -> None:
        with self._lifecycle:
            self._cancel_requested.set()
            self._lifecycle.notify_all()
        self.close()

    def _ensure_started(self, settings: DownloaderSettings) -> Aria2Api:
        self._raise_prepare_cancelled()
        deadline = self._monotonic() + self.startup_timeout_seconds
        with self._lifecycle:
            while True:
                if self._cancel_requested.is_set():
                    raise DownloadCancelled("aria2 download cancelled")
                if self._close_requested or self._state is _Aria2LifecycleState.CLOSED:
                    raise DownloadFilesError("aria2 adapter is already closed")
                if self._state is _Aria2LifecycleState.STARTING:
                    self._wait_for_lifecycle_change(deadline)
                    continue
                if self._state is _Aria2LifecycleState.CLOSING:
                    self._wait_for_lifecycle_change(deadline)
                    continue
                if self._state is _Aria2LifecycleState.CLEANUP_FAILED:
                    raise DownloadFilesError(
                        "aria2 daemon cleanup must succeed before reuse"
                    )
                if self._state is _Aria2LifecycleState.READY:
                    if self._started_settings != settings.aria2:
                        raise DownloadFilesError(
                            "aria2 adapter cannot reuse a daemon with "
                            "different settings"
                        )
                    self._fail_if_daemon_exited_locked("during daemon reuse")
                    if self._api is None:
                        raise DownloadFilesError("aria2 RPC API is not ready")
                    return self._api
                self._state = _Aria2LifecycleState.STARTING
                self._started_settings = settings.aria2
                self._lifecycle.notify_all()
                break

        try:
            try:
                secret = self._secret_factory()
            except Exception as error:
                raise DownloadFilesError(
                    "aria2 RPC secret generation failed"
                ) from error
            argv = _aria2_daemon_argv(settings.aria2, secret)
            process = self._process_factory(argv)
        except BaseException as error:
            self._finish_start_without_process()
            if isinstance(error, FileNotFoundError):
                raise DownloadFilesError("aria2c executable not found") from error
            if isinstance(error, OSError):
                raise DownloadFilesError(f"aria2c failed to start: {error}") from error
            if isinstance(error, DownloadFilesError):
                raise
            if isinstance(error, Exception):
                raise DownloadFilesError("aria2 daemon startup failed") from error
            raise

        client: Aria2Client | None = None
        try:
            with self._lifecycle:
                self._process = process
                self._lifecycle.notify_all()
            client = self._client_factory(
                host="http://localhost",
                port=settings.aria2.rpc_port,
                secret=secret,
                timeout=self.rpc_timeout_seconds,
            )
            with self._lifecycle:
                self._client = client
                self._lifecycle.notify_all()
            api = self._api_factory(client)
            self._wait_until_ready(client, settings.aria2.rpc_port)
            with self._lifecycle:
                if self._cancel_requested.is_set() or self._close_requested:
                    raise DownloadCancelled("aria2 download cancelled")
                self._api = api
                self._state = _Aria2LifecycleState.READY
                self._lifecycle.notify_all()
        except BaseException as error:
            self._publish_failed_start(process, client)
            # Teardown publishes CLEANUP_FAILED and retains the exact child before
            # an interruption escapes. Preserve the startup exception as the cause.
            with suppress(BaseException):
                self._close_until(self._monotonic() + self.shutdown_timeout_seconds)
            if isinstance(error, (DownloadFilesError, DownloadCancelled)):
                raise
            if isinstance(error, Exception):
                raise DownloadFilesError("aria2 daemon startup failed") from error
            raise
        self._log(f"aria2 RPC daemon started on port {settings.aria2.rpc_port}")
        return api

    def _wait_until_ready(self, client: Aria2Client, port: int) -> None:
        deadline = self._monotonic() + self.startup_timeout_seconds
        last_error: Exception | None = None
        while True:
            self._raise_prepare_cancelled()
            self._fail_if_daemon_exited("before RPC became ready")
            try:
                client.get_version()
                return
            except Exception as error:
                last_error = error
            if self._monotonic() >= deadline:
                raise DownloadFilesError(
                    f"aria2 RPC did not become ready on configured port {port}"
                ) from last_error
            if self._cancel_wait(self.poll_interval_seconds):
                raise DownloadCancelled("aria2 download cancelled")

    def _wait_for_download(
        self,
        download: Aria2Download,
        request: TransportRequest,
        *,
        resumed: bool,
    ) -> TransportOutcome | None:
        while True:
            with self._lifecycle:
                if self._cancel_requested.is_set():
                    return _transport_cancelled("aria2")
                self._fail_if_daemon_exited_locked(f"while downloading {request.url}")
            try:
                download.update()
            except Exception as error:
                with self._lifecycle:
                    if self._cancel_requested.is_set():
                        return _transport_cancelled("aria2")
                    raise DownloadFilesError(
                        "aria2 RPC disconnected while downloading "
                        f"{request.url}: {error}"
                    ) from error
            try:
                status = download.status
            except Exception as error:
                with self._lifecycle:
                    if self._cancel_requested.is_set():
                        return _transport_cancelled("aria2")
                    raise DownloadFilesError(
                        "aria2 RPC returned a malformed download status"
                    ) from error
            with self._lifecycle:
                if self._cancel_requested.is_set():
                    return _transport_cancelled("aria2")
                if not isinstance(status, str):
                    raise DownloadFilesError(
                        "aria2 RPC returned a malformed download status"
                    )
                if status == "complete":
                    self._log(f"aria2 download complete: {request.sink.display_path}")
                    return None
                if status == "removed":
                    raise DownloadFilesError(
                        "aria2 unexpectedly removed an active download"
                    )
                if status == "error":
                    return _classify_aria2_error(
                        download,
                        resumed=resumed,
                    )
                if status not in {"active", "waiting"}:
                    raise DownloadFilesError(
                        "aria2 RPC returned an unexpected download status"
                    )
            if self._cancel_wait(self.poll_interval_seconds):
                return _transport_cancelled("aria2")

    def _fail_if_daemon_exited(self, detail: str) -> None:
        with self._lifecycle:
            self._fail_if_daemon_exited_locked(detail)

    def _fail_if_daemon_exited_locked(self, detail: str) -> None:
        process = self._process
        if process is None:
            raise DownloadFilesError("aria2 daemon is not running")
        returncode = process.poll()
        if returncode is not None:
            raise DownloadFilesError(
                f"aria2 daemon exited with code {returncode} {detail}"
            )

    def _require_cancelled_daemon_quiescence(self) -> None:
        deadline = self._monotonic() + self.shutdown_timeout_seconds
        with self._lifecycle:
            while (
                self._process is not None
                and self._state is not _Aria2LifecycleState.CLEANUP_FAILED
            ):
                self._wait_for_lifecycle_change(deadline)
            if self._process is not None:
                raise DownloadFilesError(
                    "aria2 cancellation returned before the daemon became quiescent"
                )

    def _reap_unquiescent_item(self, item_error: Exception) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:
            raise DownloadFilesError(
                "aria2 failed item could not be made quiescent"
            ) from cleanup_error
        if self._process is not None:
            raise DownloadFilesError(
                "aria2 failed item retained a live daemon after cleanup"
            ) from item_error

    def _raise_prepare_cancelled(self) -> None:
        if self._cancel_requested.is_set():
            raise DownloadCancelled("download cancelled")

    def _finish_start_without_process(self) -> None:
        with self._lifecycle:
            self._started_settings = None
            self._state = (
                _Aria2LifecycleState.CLOSED
                if self._close_requested
                else _Aria2LifecycleState.NEW
            )
            self._lifecycle.notify_all()

    def _publish_failed_start(
        self,
        process: Aria2Process,
        client: Aria2Client | None,
    ) -> None:
        with self._lifecycle:
            if self._process is None:
                self._process = process
            elif self._process is not process:
                raise DownloadFilesError(
                    "aria2 lifecycle lost the exact child during startup"
                )
            if client is not None:
                if self._client is None:
                    self._client = client
                elif self._client is not client:
                    raise DownloadFilesError(
                        "aria2 lifecycle changed RPC client during startup"
                    )
            if self._state is _Aria2LifecycleState.STARTING:
                self._state = _Aria2LifecycleState.READY
            self._close_requested = True
            self._lifecycle.notify_all()

    def _wait_for_lifecycle_change(self, deadline: float) -> None:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise DownloadFilesError(
                "aria2 lifecycle did not settle within its deadline"
            )
        self._lifecycle.wait(remaining)

    def _shutdown_process(
        self,
        client: Aria2Client | None,
        process: Aria2Process,
        *,
        deadline: float,
    ) -> None:
        """Stop and reap one exact child within one total deadline."""
        if process.poll() is not None:
            if not _wait_for_aria2_process(
                process, _remaining_deadline(deadline, self._monotonic)
            ):
                raise DownloadFilesError("aria2 daemon could not be reaped")
            return

        if client is not None:
            rpc_shutdown = threading.Thread(
                target=_shutdown_aria2_rpc,
                args=(client,),
                daemon=True,
                name="cdh-aria2-rpc-shutdown",
            )
            rpc_shutdown.start()
            rpc_shutdown.join(_shutdown_stage_timeout(deadline, self._monotonic, 4))
            if _wait_for_aria2_process(
                process,
                _shutdown_stage_timeout(deadline, self._monotonic, 3),
            ):
                return

        process.terminate()
        if _wait_for_aria2_process(
            process,
            _shutdown_stage_timeout(deadline, self._monotonic, 2),
        ):
            return

        process.kill()
        if not _wait_for_aria2_process(
            process,
            _remaining_deadline(deadline, self._monotonic),
        ):
            raise DownloadFilesError(
                "aria2 daemon did not exit within the shutdown deadline"
            )


def file_download_plan(
    payload: FilesPhase,
    comfyui_root: str | Path,
) -> FileDownloadPlan:
    """Project admitted BuildPlan file inputs without runtime failure policy."""
    plan = FileDownloadPlan(
        comfyui_root=Path(comfyui_root),
        downloader=DownloaderSettings(
            default=require_downloader_name(payload.downloader.default),
            aria2=Aria2DownloadSettings(
                rpc_port=payload.downloader.aria2.rpc_port,
                split=payload.downloader.aria2.split,
                max_connection_per_server=(
                    payload.downloader.aria2.max_connection_per_server
                ),
                min_split_size=payload.downloader.aria2.min_split_size,
                resume_download=payload.downloader.aria2.resume_download,
            ),
            httpx=HttpxDownloadSettings(
                timeout=payload.downloader.httpx.timeout,
            ),
        ),
        items=tuple(
            FileDownloadItem(
                url=item.url,
                filename=Path(item.target).name,
                target=Path(item.target),
                overwrite=item.overwrite,
                downloader=require_downloader_name(item.downloader),
                checksum=item.checksum,
            )
            for item in payload.files
        ),
        download_max_attempts=payload.download_max_attempts,
    )
    _validate_download_plan(plan)
    return plan


def process_file_downloads(
    plan: FileDownloadPlan,
    *,
    backends: Mapping[str, DownloadBackend],
    log: Logger = print,
) -> tuple[DownloadResult, ...]:
    """Process required build files serially; every failure remains fatal."""
    _validate_download_plan(plan)
    results: list[DownloadResult] = []
    for index, item in enumerate(plan.items, 1):
        log(f"Processing required build file {index}/{len(plan.items)}: {item.target}")
        try:
            backend = backends[item.downloader]
        except KeyError as error:
            raise DownloadFilesError(
                f"download backend is not configured: {item.downloader}"
            ) from error
        outcome = _download_with_policy(item, backend, plan, log=log)
        if outcome.status is DownloadStatus.SKIPPED:
            log(f"Required build file already present: {item.target}")
        else:
            log(f"Required build file placed: {item.target}")
        _verify_build_file_postcondition(plan, item)
        results.append(DownloadResult(item=item, outcome=outcome))
    for item in plan.items:
        _verify_build_file_postcondition(plan, item)
    return tuple(results)


def _verify_build_file_postcondition(
    plan: FileDownloadPlan,
    item: FileDownloadItem,
) -> None:
    verify_required_final(
        root=plan.comfyui_root,
        target=item.target,
        expected_checksum=item.checksum,
    )


def download_files(
    files: FilesPhase,
    comfyui_root: str | Path,
    *,
    httpx_downloader: DownloadBackend | None = None,
    aria2_downloader_factory: Aria2DownloaderFactory = Aria2Downloader,
    log: Logger = print,
) -> tuple[DownloadResult, ...]:
    """Download required build files from one admitted BuildPlan phase."""
    plan = file_download_plan(files, comfyui_root)
    httpx_backend = httpx_downloader or HttpxDownloader(log=log)
    backends: dict[str, DownloadBackend] = {"httpx": httpx_backend}
    if not any(item.downloader == "aria2" for item in plan.items):
        return process_file_downloads(plan, backends=backends, log=log)
    with aria2_downloader_factory(log=log) as aria2_backend:
        backends["aria2"] = aria2_backend
        return process_file_downloads(plan, backends=backends, log=log)


def _download_with_policy(
    item: FileDownloadItem,
    backend: DownloadBackend,
    plan: FileDownloadPlan,
    *,
    log: Logger,
) -> FileTransferOutcome:
    settings = plan.downloader
    request = FileTransferRequest(
        root=plan.comfyui_root,
        url=item.url,
        target=item.target,
        overwrite=item.overwrite,
        expected_checksum=item.checksum,
        staging_disposition=StagingDisposition.CLEAN,
    )
    result = coordinate_transfer_attempts(
        request,
        backend_name=item.downloader,
        backend=backend,
        settings=settings,
        max_attempts=plan.download_max_attempts,
        log=log,
    )
    if isinstance(result, AttemptSucceeded):
        return result.outcome
    if isinstance(result, AttemptOrdinaryTerminal):
        raise result.error
    if isinstance(result, AttemptExhausted):
        raise result.error
    if isinstance(result, AttemptCancelled):
        raise DownloadCancelled("required build file download was cancelled")
    raise AssertionError("attempt coordinator returned an unknown result")


def _validate_download_plan(plan: FileDownloadPlan) -> None:
    if not plan.comfyui_root.is_absolute():
        raise DownloadFilesError("download root must be an absolute path")
    for item in plan.items:
        if not item.target.is_absolute():
            raise DownloadFilesError("download target must be an absolute path")
        try:
            relative = item.target.relative_to(plan.comfyui_root)
        except ValueError as error:
            raise DownloadFilesError(
                f"download target escapes COMFYUI_PATH: {item.target}"
            ) from error
        if not relative.parts or ".." in relative.parts:
            raise DownloadFilesError(
                "download target must be a strict descendant of COMFYUI_PATH: "
                f"{item.target}"
            )


class CancellationWait(Protocol):
    def __call__(self, timeout: float) -> bool: ...


class Monotonic(Protocol):
    def __call__(self) -> float: ...


class SecretFactory(Protocol):
    def __call__(self) -> str: ...


class Aria2Process(Protocol):
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class ProcessFactory(Protocol):
    def __call__(self, argv: Sequence[str]) -> Aria2Process: ...


class Aria2Client(Protocol):
    def get_version(self) -> object: ...
    def shutdown(self) -> object: ...


class Aria2ClientFactory(Protocol):
    def __call__(
        self,
        *,
        host: str,
        port: int,
        secret: str,
        timeout: float,
    ) -> Aria2Client: ...


class Aria2Download(Protocol):
    status: str
    error_code: str | None

    def update(self) -> None: ...


class Aria2Api(Protocol):
    def add_uris(
        self,
        uris: list[str],
        options: Mapping[str, str] | None = None,
    ) -> Aria2Download: ...


class Aria2ApiFactory(Protocol):
    def __call__(self, client: Aria2Client) -> Aria2Api: ...


class ManagedDownloadBackend(DownloadBackend, Protocol):
    def __enter__(self) -> ManagedDownloadBackend: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class Aria2DownloaderFactory(Protocol):
    def __call__(self, *, log: Logger) -> ManagedDownloadBackend: ...


def _http_failure_outcome(
    response: httpx.Response,
    *,
    wall_clock: Monotonic = time.time,
) -> TransportRetryable | TransportOrdinaryTerminal | None:
    status = response.status_code
    if status in {408, 429} or 500 <= status <= 599:
        return TransportRetryable(
            diagnostic=TransportDiagnostic(
                namespace="httpx",
                summary=(
                    f"HTTP download got retryable status {status}: {response.url}"
                ),
            ),
            http_status=status,
            retry_after_seconds=_normalized_retry_after(
                response,
                wall_clock=wall_clock,
            ),
        )
    if 400 <= status <= 599:
        return TransportOrdinaryTerminal(
            diagnostic=TransportDiagnostic(
                namespace="httpx",
                summary=(
                    f"HTTP download got non-retryable status {status}: {response.url}"
                ),
            ),
            http_status=status,
        )
    return None


def _normalized_retry_after(
    response: httpx.Response,
    *,
    wall_clock: Monotonic,
) -> float | None:
    values = [
        value.decode("latin-1")
        for name, value in response.headers.raw
        if name.lower() == b"retry-after"
    ]
    if len(values) != 1:
        return None
    value = values[0].strip()
    if value.isascii() and value.isdecimal():
        try:
            return float(int(value))
        except (ValueError, OverflowError):
            return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    try:
        delay = parsed.timestamp() - wall_clock()
    except (OSError, OverflowError, ValueError):
        return None
    return delay if delay >= 0 else None


def _transport_cancelled(
    namespace: Literal["httpx", "aria2"],
) -> TransportCancelled:
    return TransportCancelled(
        diagnostic=TransportDiagnostic(
            namespace=namespace,
            summary=f"{namespace} download cancelled",
        )
    )


def _classify_aria2_error(
    download: Aria2Download,
    *,
    resumed: bool,
) -> TransportOutcome:
    """Map only aria2's deliberately small stable machine-code allowlist."""
    try:
        code = download.error_code
    except Exception as error:
        raise DownloadFilesError(
            "aria2 RPC returned malformed error metadata"
        ) from error
    if not isinstance(code, str):
        raise DownloadFilesError("aria2 RPC returned malformed error metadata")
    if code == "8":
        if not resumed:
            raise DownloadFilesError(
                "aria2 rejected resume without an admitted resumed request"
            )
        return TransportResumeRejected(
            diagnostic=TransportDiagnostic(
                namespace="aria2",
                summary="aria2 reported that the remote server rejected resume",
            )
        )

    retryable_summaries = {
        "2": "aria2 reported a timeout",
        "6": "aria2 reported a network failure",
        "19": "aria2 reported a name-resolution failure",
        "29": "aria2 reported temporary server unavailability",
    }
    terminal_summaries = {
        "3": "aria2 reported that the remote resource was not found",
        "4": "aria2 reported that the remote resource was not found",
        "23": "aria2 reported too many redirects",
        "24": "aria2 reported an HTTP authorization failure",
        "22": "aria2 reported an indeterminate HTTP failure",
    }
    if code in retryable_summaries:
        return TransportRetryable(
            diagnostic=TransportDiagnostic(
                namespace="aria2",
                summary=retryable_summaries[code],
            ),
            http_status=None,
        )
    if code in terminal_summaries:
        return TransportOrdinaryTerminal(
            diagnostic=TransportDiagnostic(
                namespace="aria2",
                summary=terminal_summaries[code],
            ),
            http_status=None,
        )
    raise DownloadFilesError("aria2 reported an unclassified transport failure")


def _shutdown_aria2_rpc(client: Aria2Client) -> None:
    with suppress(Exception):
        client.shutdown()


def _wait_for_aria2_process(process: Aria2Process, timeout: float) -> bool:
    try:
        process.wait(timeout=max(0.0, timeout))
        return True
    except subprocess.TimeoutExpired:
        return False


def _shutdown_stage_timeout(
    deadline: float,
    monotonic: Monotonic,
    stages: int,
) -> float:
    return _remaining_deadline(deadline, monotonic) / stages


def _remaining_deadline(deadline: float, monotonic: Monotonic) -> float:
    return max(0.0, deadline - monotonic())


def _aria2_options(
    request: TransportRequest,
    settings: Aria2DownloadSettings,
) -> dict[str, str]:
    return {
        "dir": request.sink.aria2_directory,
        "out": request.sink.aria2_name,
        "split": str(settings.split),
        "max-connection-per-server": str(settings.max_connection_per_server),
        "min-split-size": settings.min_split_size,
        "continue": _aria2_bool(
            settings.resume_download and request.sink.resume_allowed
        ),
        "max-tries": "1",
        "always-resume": "true",
        "retry-wait": "0",
        "max-file-not-found": "0",
        "auto-file-renaming": "false",
        "allow-overwrite": "true",
    }


def _aria2_daemon_argv(settings: Aria2DownloadSettings, secret: str) -> list[str]:
    return [
        "aria2c",
        "--no-conf=true",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={settings.rpc_port}",
        f"--rpc-secret={secret}",
        "--disable-ipv6=true",
        "--auto-save-interval=0",
        "--console-log-level=notice",
    ]


def _aria2_bool(value: bool) -> str:
    return "true" if value else "false"
