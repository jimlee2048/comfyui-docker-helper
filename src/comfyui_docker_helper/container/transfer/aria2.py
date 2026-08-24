"""aria2 transfer adapter and lifecycle contracts."""

from __future__ import annotations

import math
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from enum import Enum, auto
from types import TracebackType
from typing import Literal, Protocol

import aria2p

from comfyui_docker_helper.container.transfer.core import (
    Aria2DownloadSettings,
    DownloadBackend,
    DownloadCancelled,
    DownloaderSettings,
    DownloadFilesError,
    TransportCancelled,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportOutcome,
    TransportRequest,
    TransportResumeRejected,
    TransportRetryable,
    TransportSuccess,
)
from comfyui_docker_helper.container.transfer.events import (
    DownloadRetryReason,
    DownloadTransferProgress,
)

_MAX_TRANSFER_BYTES = sys.maxsize


def _aria2_transfer_progress(
    download: Aria2Download,
) -> DownloadTransferProgress | None:
    completed = _aria2_non_negative_integer(download, "completed_length")
    if completed is None:
        return None
    total = _aria2_non_negative_integer(download, "total_length")
    if total is not None and (total <= 0 or total < completed):
        total = None
    rate = _aria2_non_negative_number(download, "download_speed")
    return DownloadTransferProgress(
        transferred_bytes=completed,
        total_bytes=total,
        stored_bytes=None,
        reported_rate=rate,
    )


def _aria2_non_negative_integer(
    download: Aria2Download,
    attribute: str,
) -> int | None:
    try:
        value = getattr(download, attribute)
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value if value <= _MAX_TRANSFER_BYTES else None


def _aria2_non_negative_number(
    download: Aria2Download,
    attribute: str,
) -> int | float | None:
    try:
        value = getattr(download, attribute)
    except Exception:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_TRANSFER_BYTES else None
    if not isinstance(value, float):
        return None
    if not math.isfinite(value) or value < 0 or value > _MAX_TRANSFER_BYTES:
        return None
    return value


class _Aria2LifecycleState(Enum):
    NEW = auto()
    STARTING = auto()
    READY = auto()
    CLOSING = auto()
    CLEANUP_FAILED = auto()
    CLOSED = auto()


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
    completed_length: int
    total_length: int
    download_speed: int

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
    def __call__(self) -> ManagedDownloadBackend: ...


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
    ) -> None:
        self._process_factory = process_factory
        self._client_factory = client_factory
        self._api_factory = api_factory
        self._secret_factory = secret_factory
        self._monotonic = monotonic
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
            raise DownloadFilesError("aria2 RPC submit failed") from error
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
                "aria2 supplied staging cannot be inspected"
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
            try:
                self._shutdown_process(client, process, deadline=deadline)
            except DownloadFilesError:
                raise
            except Exception as error:
                raise DownloadFilesError("aria2 daemon shutdown failed") from error
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

    def cancel(self, *, deadline: float | None = None) -> None:
        with self._lifecycle:
            self._cancel_requested.set()
            self._lifecycle.notify_all()
        if deadline is None:
            self.close()
        else:
            self._close_until(deadline)

    def force_cancel(self) -> None:
        """Kill the exact active daemon while its cancellation owner reaps it."""
        with self._lifecycle:
            self._cancel_requested.set()
            self._close_requested = True
            process = self._process
            self._lifecycle.notify_all()
        if process is not None and process.poll() is None:
            with suppress(OSError):
                process.kill()

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
                raise DownloadFilesError("aria2c failed to start") from error
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
                self._fail_if_daemon_exited_locked("during active download")
            try:
                download.update()
            except Exception as error:
                with self._lifecycle:
                    if self._cancel_requested.is_set():
                        return _transport_cancelled("aria2")
                    raise DownloadFilesError(
                        "aria2 RPC disconnected during an active download"
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
                if status == "removed":
                    raise DownloadFilesError(
                        "aria2 unexpectedly removed an active download"
                    )
                if status == "error":
                    return _classify_aria2_error(
                        download,
                        resumed=resumed,
                    )
                if status not in {"active", "waiting"} and status != "complete":
                    raise DownloadFilesError(
                        "aria2 RPC returned an unexpected download status"
                    )
            progress = _aria2_transfer_progress(download)
            if progress is not None and request.progress_sink is not None:
                request.progress_sink.emit(progress)
            if status == "complete":
                return None
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

    retryable_facts = {
        "2": ("aria2 reported a timeout", DownloadRetryReason.TIMEOUT),
        "6": ("aria2 reported a network failure", DownloadRetryReason.NETWORK),
        "19": (
            "aria2 reported a name-resolution failure",
            DownloadRetryReason.NETWORK,
        ),
        "29": (
            "aria2 reported temporary server unavailability",
            DownloadRetryReason.TEMPORARY_SERVER,
        ),
    }
    terminal_summaries = {
        "3": "aria2 reported that the remote resource was not found",
        "4": "aria2 reported that the remote resource was not found",
        "23": "aria2 reported too many redirects",
        "24": "aria2 reported an HTTP authorization failure",
        "22": "aria2 reported an indeterminate HTTP failure",
    }
    if code in retryable_facts:
        summary, reason = retryable_facts[code]
        return TransportRetryable(
            diagnostic=TransportDiagnostic(
                namespace="aria2",
                summary=summary,
            ),
            http_status=None,
            reason=reason,
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
        "--quiet=true",
    ]


def _aria2_bool(value: bool) -> str:
    return "true" if value else "false"
