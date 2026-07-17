"""Container-side file transport adapters and build orchestration."""

from __future__ import annotations

import secrets
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol

import aria2p
import httpx

from comfyui_docker_helper.config.build_plan import FilesPhase
from comfyui_docker_helper.config.url_validation import (
    DownloaderName,
    require_downloader_name,
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
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
    TransportRequest,
    TransportResult,
    TransportSink,
    transfer_file,
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
        transport: httpx.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
        log: Logger = print,
    ) -> None:
        self._transport = transport
        self._sleep = sleep
        self._monotonic = monotonic
        self._log = log
        self._cancel_requested = threading.Event()
        self._client_lock = threading.Lock()
        self._active_client: httpx.Client | None = None

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportResult:
        """Write HTTP attempts to the exact core-owned staging inode."""
        attempts = settings.httpx.retries + 1
        for attempt in range(attempts):
            self._raise_if_cancelled()
            try:
                length = self._download_once(request, settings)
                self._raise_if_cancelled()
                return TransportResult(length=length)
            except DownloadCancelled:
                raise
            except _RetryableDownloadError as error:
                if attempt + 1 >= attempts:
                    raise TransferDownloadFilesError(str(error)) from error
                delay = _backoff_delay(attempt)
                self._log(
                    f"Retrying HTTP download in {delay}s after failure: {request.url}"
                )
                self._sleep(delay)
            except DownloadFilesError:
                raise
            except OSError as error:
                raise DownloadFilesError(
                    "HTTP download failed while writing supplied staging "
                    f"{request.sink.display_path}: {error}"
                ) from error
        raise AssertionError("HTTPX attempt loop produced no outcome")

    def _download_once(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> int:
        timeout = httpx.Timeout(settings.httpx.timeout)
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=timeout,
                transport=self._transport,
            ) as client:
                self._set_active_client(client)
                with client.stream("GET", request.url) as response:
                    self._raise_if_cancelled()
                    _raise_for_http_status(response)
                    return self._write_response(response, request.sink)
        except DownloadCancelled:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as error:
            self._raise_if_cancelled()
            raise _RetryableDownloadError(
                f"HTTP download failed for {request.url}: {error}"
            ) from error
        finally:
            self._set_active_client(None)

    def _set_active_client(self, client: httpx.Client | None) -> None:
        with self._client_lock:
            self._active_client = client

    def _write_response(self, response: httpx.Response, sink: TransportSink) -> int:
        downloaded = 0
        last_log = self._monotonic()
        with sink.open_for_write() as output:
            for chunk in response.iter_bytes(chunk_size=self.chunk_size):
                self._raise_if_cancelled()
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
        self._cancel_requested.set()
        with self._client_lock:
            client = self._active_client
        if client is not None:
            with suppress(Exception):
                client.close()

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested.is_set():
            raise DownloadCancelled("download cancelled")


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
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
        log: Logger = print,
    ) -> None:
        self._process_factory = process_factory
        self._client_factory = client_factory
        self._api_factory = api_factory
        self._secret_factory = secret_factory
        self._sleep = sleep
        self._monotonic = monotonic
        self._log = log
        self._process: Aria2Process | None = None
        self._client: Aria2Client | None = None
        self._api: Aria2Api | None = None
        self._cancel_requested = threading.Event()

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
        self._raise_if_cancelled()
        self._ensure_started(settings)

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportResult:
        self._raise_if_cancelled()
        api = self._ensure_started(settings)
        options = _aria2_options(request, settings.aria2)
        try:
            download = api.add_uris([request.url], options=options)
        except Exception as error:
            raise DownloadFilesError(
                f"aria2 RPC submit failed for {request.url}: {error}"
            ) from error
        self._wait_for_download(download, request)
        try:
            length = request.sink.current_length()
        except DownloadFilesError:
            raise
        except OSError as error:
            raise DownloadFilesError(
                f"aria2 supplied staging cannot be inspected: {error}"
            ) from error
        return TransportResult(length=length)

    def close(self) -> None:
        client = self._client
        process = self._process
        self._api = None
        self._client = None
        self._process = None
        if client is not None:
            with suppress(Exception):
                client.shutdown()
        if process is None:
            return
        try:
            process.wait(timeout=self.shutdown_timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            return
        try:
            process.terminate()
            process.wait(timeout=self.shutdown_timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            return
        try:
            process.kill()
            process.wait(timeout=self.shutdown_timeout_seconds)
        except Exception:
            pass

    def cancel(self) -> None:
        self._cancel_requested.set()
        self.close()

    def _ensure_started(self, settings: DownloaderSettings) -> Aria2Api:
        if self._api is not None:
            return self._api
        secret = self._secret_factory()
        argv = _aria2_daemon_argv(settings.aria2, secret)
        try:
            process = self._process_factory(argv)
        except FileNotFoundError as error:
            raise DownloadFilesError("aria2c executable not found") from error
        except OSError as error:
            raise DownloadFilesError(f"aria2c failed to start: {error}") from error
        self._process = process
        try:
            client = self._client_factory(
                host="http://localhost",
                port=settings.aria2.rpc_port,
                secret=secret,
                timeout=self.rpc_timeout_seconds,
            )
            api = self._api_factory(client)
            self._client = client
            self._api = api
            self._wait_until_ready(client, settings.aria2.rpc_port)
        except Exception:
            self.close()
            raise
        self._log(f"aria2 RPC daemon started on port {settings.aria2.rpc_port}")
        return api

    def _wait_until_ready(self, client: Aria2Client, port: int) -> None:
        deadline = self._monotonic() + self.startup_timeout_seconds
        last_error: Exception | None = None
        while True:
            self._raise_if_cancelled()
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
            self._sleep(self.poll_interval_seconds)

    def _wait_for_download(
        self,
        download: Aria2Download,
        request: TransportRequest,
    ) -> None:
        while True:
            self._raise_if_cancelled()
            self._fail_if_daemon_exited(f"while downloading {request.url}")
            try:
                download.update()
            except Exception as error:
                raise DownloadFilesError(
                    f"aria2 RPC disconnected while downloading {request.url}: {error}"
                ) from error
            status = download.status
            if download.is_complete or status == "complete":
                self._log(f"aria2 download complete: {request.sink.display_path}")
                return
            if download.is_removed or status == "removed":
                raise TransferDownloadFilesError(
                    f"aria2 download was removed: {request.url}"
                )
            if status == "error":
                message = download.error_message or "unknown aria2 error"
                raise TransferDownloadFilesError(
                    f"aria2 download failed for {request.url}: {message}"
                )
            self._sleep(self.poll_interval_seconds)

    def _fail_if_daemon_exited(self, detail: str) -> None:
        process = self._process
        if process is None:
            raise DownloadFilesError("aria2 daemon is not running")
        returncode = process.poll()
        if returncode is not None:
            raise DownloadFilesError(
                f"aria2 daemon exited with code {returncode} {detail}"
            )

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested.is_set():
            raise DownloadCancelled("download cancelled")


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
                retries=payload.downloader.httpx.retries,
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
    for attempt in range(1, plan.download_max_attempts + 1):
        try:
            return transfer_file(request, backend=backend, settings=settings)
        except TransferDownloadFilesError as error:
            if attempt >= plan.download_max_attempts:
                raise
            log(
                f"Retrying required build file after attempt "
                f"{attempt}/{plan.download_max_attempts} failed: "
                f"{item.target}: {error}"
            )
    raise AssertionError("build transfer attempt loop produced no outcome")


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


class Sleep(Protocol):
    def __call__(self, seconds: float) -> None: ...


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
    is_complete: bool
    is_removed: bool
    error_message: str | None

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


class _RetryableDownloadError(Exception):
    """Internal retryable HTTP transport marker."""


def _raise_for_http_status(response: httpx.Response) -> None:
    status = response.status_code
    if status in {408, 429} or 500 <= status <= 599:
        raise _RetryableDownloadError(
            f"HTTP download got retryable status {status}: {response.url}"
        )
    if 400 <= status <= 599:
        raise TerminalTransferDownloadFilesError(
            f"HTTP download got non-retryable status {status}: {response.url}"
        )


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
        "--console-log-level=notice",
    ]


def _aria2_bool(value: bool) -> str:
    return "true" if value else "false"


def _backoff_delay(attempt: int) -> float:
    return float((1, 2, 4)[min(attempt, 2)])
