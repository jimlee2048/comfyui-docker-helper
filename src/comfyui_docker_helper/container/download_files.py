"""Container-side file download planning and common processing."""

from __future__ import annotations

import os
import secrets
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal, Protocol

import aria2p
import httpx
from pydantic import Field, ValidationError

from comfyui_docker_helper.config.models import ConfigModel
from comfyui_docker_helper.config.url_validation import (
    DownloaderName,
    is_http_url,
    require_downloader_name,
    validate_file_name,
    validate_relative_file_directory,
)
from comfyui_docker_helper.container.root_config import (
    ContainerRootConfigError,
    files_document,
    load_container_root_artifacts,
)
from comfyui_docker_helper.errors import ApplicationError


class DownloadFilesConfigError(ApplicationError):
    """A user-facing generated file-download config failure."""


class DownloadFilesError(ApplicationError):
    """A user-facing file-download processing failure."""


class TransferDownloadFilesError(DownloadFilesError):
    """A source or transport download failure eligible for retry/continue policy."""


class DownloadStatus(StrEnum):
    """Common per-item processing result."""

    SKIPPED = "skipped"
    DOWNLOADED = "downloaded"


@dataclass(frozen=True, slots=True)
class Aria2DownloadSettings:
    """Normalized aria2 helper settings."""

    rpc_port: int
    split: int
    max_connection_per_server: int
    min_split_size: str
    resume_download: bool


@dataclass(frozen=True, slots=True)
class HttpxDownloadSettings:
    """Normalized httpx helper settings."""

    timeout: int | float
    retries: int


@dataclass(frozen=True, slots=True)
class DownloaderSettings:
    """Normalized downloader settings for both backends."""

    default: DownloaderName
    aria2: Aria2DownloadSettings
    httpx: HttpxDownloadSettings


@dataclass(frozen=True, slots=True)
class FileDownloadItem:
    """One resolved container file download item."""

    url: str
    directory: str
    filename: str
    target: Path
    overwrite: bool
    downloader: DownloaderName


@dataclass(frozen=True, slots=True)
class FileDownloadPlan:
    """Ordered file downloads and backend settings."""

    downloader: DownloaderSettings
    items: tuple[FileDownloadItem, ...]
    download_max_attempts: int = 3
    download_failure_policy: Literal["continue", "fail"] = "fail"


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """One common processing result."""

    item: FileDownloadItem
    status: DownloadStatus


class DownloadBackend(Protocol):
    """Backend interface implemented by concrete downloaders."""

    def download(
        self,
        item: FileDownloadItem,
        settings: DownloaderSettings,
    ) -> None: ...


class HttpxDownloader:
    """HTTPX streaming downloader with retries and temporary-file replacement."""

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

    def download(
        self,
        item: FileDownloadItem,
        settings: DownloaderSettings,
    ) -> None:
        tmp_path = _tmp_path(item.target)
        _remove_stale_tmp(tmp_path)

        attempts = settings.httpx.retries + 1
        for attempt in range(attempts):
            try:
                self._download_once(item, settings, tmp_path)
                tmp_path.replace(item.target)
                return
            except _RetryableDownloadError as error:
                _cleanup_tmp(tmp_path)
                if attempt + 1 >= attempts:
                    raise TransferDownloadFilesError(str(error)) from error
                delay = _backoff_delay(attempt)
                self._log(
                    f"Retrying HTTP download in {delay}s after failure: {item.url}"
                )
                self._sleep(delay)
            except DownloadFilesError:
                _cleanup_tmp(tmp_path)
                raise
            except OSError as error:
                _cleanup_tmp(tmp_path)
                raise DownloadFilesError(
                    f"HTTP download failed while writing {item.target}: {error}"
                ) from error

    def _download_once(
        self,
        item: FileDownloadItem,
        settings: DownloaderSettings,
        tmp_path: Path,
    ) -> None:
        timeout = httpx.Timeout(settings.httpx.timeout)
        try:
            with (
                httpx.Client(
                    follow_redirects=True,
                    timeout=timeout,
                    transport=self._transport,
                ) as client,
                client.stream("GET", item.url) as response,
            ):
                _raise_for_http_status(response)
                self._write_response(response, tmp_path)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise _RetryableDownloadError(
                f"HTTP download failed for {item.url}: {error}"
            ) from error

    def _write_response(self, response: httpx.Response, tmp_path: Path) -> None:
        downloaded = 0
        last_log = self._monotonic()
        with tmp_path.open("wb") as output:
            for chunk in response.iter_bytes(chunk_size=self.chunk_size):
                if not chunk:
                    continue
                output.write(chunk)
                downloaded += len(chunk)
                now = self._monotonic()
                if now - last_log >= self.progress_interval_seconds:
                    self._log(f"Downloaded {downloaded} bytes to {tmp_path}")
                    last_log = now


class Aria2Downloader:
    """aria2 RPC downloader with one temporary local daemon per context."""

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

    def download(
        self,
        item: FileDownloadItem,
        settings: DownloaderSettings,
    ) -> None:
        api = self._ensure_started(settings)
        _remove_aria2_control_file(item)
        options = _aria2_options(item, settings.aria2)
        try:
            download = api.add_uris([item.url], options=options)
        except Exception as error:
            raise DownloadFilesError(
                f"aria2 RPC submit failed for {item.url}: {error}"
            ) from error

        self._wait_for_download(download, item)

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
        item: FileDownloadItem,
    ) -> None:
        while True:
            self._fail_if_daemon_exited(f"while downloading {item.url}")
            try:
                download.update()
            except Exception as error:
                raise DownloadFilesError(
                    f"aria2 RPC disconnected while downloading {item.url}: {error}"
                ) from error

            status = download.status
            if download.is_complete or status == "complete":
                self._log(f"aria2 download complete: {item.target}")
                return
            if download.is_removed or status == "removed":
                raise TransferDownloadFilesError(
                    f"aria2 download was removed: {item.url}"
                )
            if status == "error":
                message = download.error_message or "unknown aria2 error"
                raise TransferDownloadFilesError(
                    f"aria2 download failed for {item.url}: {message}"
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


class _Aria2Config(ConfigModel):
    rpc_port: int = Field(ge=1, le=65535)
    split: int = Field(gt=0)
    max_connection_per_server: int = Field(gt=0)
    min_split_size: str
    resume_download: bool


class _HttpxConfig(ConfigModel):
    timeout: int | float = Field(gt=0)
    retries: int = Field(ge=0)


class _DownloaderConfig(ConfigModel):
    default: DownloaderName
    aria2: _Aria2Config
    httpx: _HttpxConfig


class _CdhConfig(ConfigModel):
    download_max_attempts: int = Field(default=3, ge=1)
    download_failure_policy: Literal["continue", "fail"] = "fail"


class _FileConfig(ConfigModel):
    url: str
    dir: str
    filename: str
    overwrite: bool
    downloader: DownloaderName


class _FilesConfig(ConfigModel):
    cdh: _CdhConfig = Field(default_factory=_CdhConfig)
    downloader: _DownloaderConfig
    files: list[_FileConfig]


def load_file_download_plan(
    config_path: str | Path,
    lock_path: str | Path,
    *,
    comfyui_path: str | Path | None = None,
) -> FileDownloadPlan:
    """Load root artifacts and build a deterministic file-download plan."""

    try:
        artifacts = load_container_root_artifacts(config_path, lock_path)
    except ContainerRootConfigError as error:
        raise DownloadFilesConfigError(str(error)) from error
    document = files_document(artifacts.config)
    return build_file_download_plan(document, comfyui_path=comfyui_path)


def build_file_download_plan(
    document: Mapping[str, object],
    *,
    comfyui_path: str | Path | None = None,
) -> FileDownloadPlan:
    """Validate file-download helper data and derive container targets."""

    try:
        config = _FilesConfig.model_validate(document)
    except ValidationError as error:
        raise DownloadFilesConfigError(
            f"file-download config validation failed: {error}"
        ) from error

    downloader = DownloaderSettings(
        default=require_downloader_name(config.downloader.default),
        aria2=Aria2DownloadSettings(
            rpc_port=config.downloader.aria2.rpc_port,
            split=config.downloader.aria2.split,
            max_connection_per_server=(
                config.downloader.aria2.max_connection_per_server
            ),
            min_split_size=config.downloader.aria2.min_split_size,
            resume_download=config.downloader.aria2.resume_download,
        ),
        httpx=HttpxDownloadSettings(
            timeout=config.downloader.httpx.timeout,
            retries=config.downloader.httpx.retries,
        ),
    )
    resolved_comfyui_path = _resolve_comfyui_path(comfyui_path)
    items = tuple(
        _build_file_item(file, comfyui_path=resolved_comfyui_path)
        for file in config.files
    )
    return FileDownloadPlan(
        downloader=downloader,
        items=items,
        download_max_attempts=config.cdh.download_max_attempts,
        download_failure_policy=config.cdh.download_failure_policy,
    )


def process_file_downloads(
    plan: FileDownloadPlan,
    *,
    backends: Mapping[str, DownloadBackend],
    log: Logger = print,
) -> tuple[DownloadResult, ...]:
    """Process file downloads serially with common preflight semantics."""

    results: list[DownloadResult] = []
    for index, item in enumerate(plan.items, 1):
        log(f"Processing file {index}/{len(plan.items)}: {item.target}")
        if _preflight_file(item, log=log):
            results.append(DownloadResult(item=item, status=DownloadStatus.SKIPPED))
            continue

        try:
            backend = backends[item.downloader]
        except KeyError as error:
            raise DownloadFilesError(
                f"download backend is not configured: {item.downloader}"
            ) from error

        log(f"Downloading file with {item.downloader}: {item.url}")
        try:
            _download_with_policy(
                item,
                backend,
                plan,
                log=log,
            )
        except TransferDownloadFilesError as error:
            if plan.download_failure_policy == "fail":
                raise
            _cleanup_failed_target(item, log=log)
            log(
                "WARNING: download failed after "
                f"{plan.download_max_attempts} attempt(s), continuing: "
                f"{item.target}: {error}"
            )
            continue
        log(f"Downloaded file: {item.target}")
        results.append(DownloadResult(item=item, status=DownloadStatus.DOWNLOADED))

    return tuple(results)


def download_files(
    config_path: str | Path,
    lock_path: str | Path,
    *,
    comfyui_path: str | Path | None = None,
    httpx_downloader: DownloadBackend | None = None,
    aria2_downloader_factory: Aria2DownloaderFactory = Aria2Downloader,
    log: Logger = print,
) -> tuple[DownloadResult, ...]:
    """Download files from validated root artifacts with managed backends."""

    plan = load_file_download_plan(
        config_path,
        lock_path,
        comfyui_path=comfyui_path,
    )
    httpx_backend = httpx_downloader or HttpxDownloader(log=log)
    backends: dict[str, DownloadBackend] = {"httpx": httpx_backend}

    if not any(item.downloader == "aria2" for item in plan.items):
        return process_file_downloads(plan, backends=backends, log=log)

    with aria2_downloader_factory(log=log) as aria2_backend:
        backends["aria2"] = aria2_backend
        return process_file_downloads(plan, backends=backends, log=log)


class Logger(Protocol):
    """Minimal logger protocol used by the file helper."""

    def __call__(self, message: str) -> None: ...


class Sleep(Protocol):
    """Injectable sleep function for retry backoff."""

    def __call__(self, seconds: float) -> None: ...


class Monotonic(Protocol):
    """Injectable monotonic clock for progress logging."""

    def __call__(self) -> float: ...


class SecretFactory(Protocol):
    """Injectable aria2 RPC secret generator."""

    def __call__(self) -> str: ...


class Aria2Process(Protocol):
    """Minimal process interface used to manage aria2c."""

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class ProcessFactory(Protocol):
    """Injectable aria2c process factory."""

    def __call__(self, argv: Sequence[str]) -> Aria2Process: ...


class Aria2Client(Protocol):
    """Minimal aria2p client interface."""

    def get_version(self) -> object: ...

    def shutdown(self) -> object: ...


class Aria2ClientFactory(Protocol):
    """Injectable aria2p client factory."""

    def __call__(
        self,
        *,
        host: str,
        port: int,
        secret: str,
        timeout: float,
    ) -> Aria2Client: ...


class Aria2Download(Protocol):
    """Minimal aria2p download interface."""

    status: str
    is_complete: bool
    is_removed: bool
    error_message: str | None

    def update(self) -> None: ...


class Aria2Api(Protocol):
    """Minimal aria2p API interface."""

    def add_uris(
        self,
        uris: list[str],
        options: Mapping[str, str] | None = None,
    ) -> Aria2Download: ...


class Aria2ApiFactory(Protocol):
    """Injectable aria2p API factory."""

    def __call__(self, client: Aria2Client) -> Aria2Api: ...


class ManagedDownloadBackend(DownloadBackend, Protocol):
    """Download backend with context-managed cleanup."""

    def __enter__(self) -> ManagedDownloadBackend: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class Aria2DownloaderFactory(Protocol):
    """Factory for managed aria2 downloader instances."""

    def __call__(self, *, log: Logger) -> ManagedDownloadBackend: ...


class _RetryableDownloadError(Exception):
    """Internal retryable HTTP failure marker."""


def _build_file_item(
    file: _FileConfig,
    *,
    comfyui_path: Path,
) -> FileDownloadItem:
    _validate_url(file.url)
    directory = _validate_relative_path(file.dir, field="dir")
    filename = _validate_filename(file.filename)
    return FileDownloadItem(
        url=file.url,
        directory=file.dir,
        filename=file.filename,
        target=comfyui_path.joinpath(*directory.parts, filename.name),
        overwrite=file.overwrite,
        downloader=require_downloader_name(file.downloader),
    )


def _download_with_policy(
    item: FileDownloadItem,
    backend: DownloadBackend,
    plan: FileDownloadPlan,
    *,
    log: Logger,
) -> None:
    settings = _single_host_attempt_settings(plan.downloader)
    attempts = plan.download_max_attempts
    for attempt in range(1, attempts + 1):
        try:
            backend.download(item, settings)
            return
        except TransferDownloadFilesError as error:
            _cleanup_failed_target(item, log=log)
            if attempt >= attempts:
                raise
            log(
                f"Retrying file download after attempt {attempt}/{attempts} failed: "
                f"{item.target}: {error}"
            )
        except DownloadFilesError:
            _cleanup_failed_target(item, log=log)
            raise


def _single_host_attempt_settings(settings: DownloaderSettings) -> DownloaderSettings:
    """Keep host build policy attempts from multiplying backend HTTPX retries."""
    return DownloaderSettings(
        default=settings.default,
        aria2=settings.aria2,
        httpx=HttpxDownloadSettings(
            timeout=settings.httpx.timeout,
            retries=0,
        ),
    )


def _cleanup_failed_target(item: FileDownloadItem, *, log: Logger) -> None:
    _cleanup_failed_path(item.target, log=log)
    if item.downloader == "aria2":
        _cleanup_failed_path(_aria2_control_path(item.target), log=log)


def _cleanup_failed_path(path: Path, *, log: Logger) -> None:
    try:
        if path.exists() or path.is_symlink():
            path.unlink()
    except OSError as error:
        log(f"WARNING: failed download artifact could not be removed: {path}: {error}")


def _preflight_file(item: FileDownloadItem, *, log: Logger) -> bool:
    _validate_existing_target_parent_contained(item)
    try:
        item.target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DownloadFilesError(
            f"download target parent cannot be created: {item.target.parent}: {error}"
        ) from error

    _validate_target_parent_contained(item)

    try:
        mode = item.target.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as error:
        raise DownloadFilesError(
            f"download target cannot be inspected: {item.target}: {error}"
        ) from error

    if not stat.S_ISREG(mode):
        raise DownloadFilesError(
            f"download target exists but is not a regular file: {item.target}"
        )

    if not item.overwrite:
        log(f"Skipping existing file: {item.target}")
        return True

    log(f"Removing existing file before overwrite: {item.target}")
    try:
        item.target.unlink()
    except OSError as error:
        raise DownloadFilesError(
            f"download target cannot be removed for overwrite: {item.target}: {error}"
        ) from error
    return False


def _validate_target_parent_contained(item: FileDownloadItem) -> None:
    root = _item_comfyui_root(item)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_parent = item.target.parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
    except ValueError as error:
        raise DownloadFilesError(
            f"download target parent escapes COMFYUI_PATH: {item.target.parent}"
        ) from error
    except OSError as error:
        raise DownloadFilesError(
            f"download target parent cannot be resolved: {item.target.parent}: {error}"
        ) from error


def _validate_existing_target_parent_contained(item: FileDownloadItem) -> None:
    root = _item_comfyui_root(item)
    try:
        resolved_root = root.resolve(strict=False)
    except OSError as error:
        raise DownloadFilesError(
            f"COMFYUI_PATH cannot be resolved before download: {root}: {error}"
        ) from error

    current = root
    for part in PurePosixPath(item.directory).parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            return
        try:
            resolved_current = current.resolve(strict=True)
            resolved_current.relative_to(resolved_root)
        except ValueError as error:
            raise DownloadFilesError(
                f"download target parent escapes COMFYUI_PATH: {current}"
            ) from error
        except OSError as error:
            raise DownloadFilesError(
                f"download target parent cannot be resolved: {current}: {error}"
            ) from error


def _item_comfyui_root(item: FileDownloadItem) -> Path:
    directory = PurePosixPath(item.directory)
    return item.target.parents[len(directory.parts)]


def _resolve_comfyui_path(comfyui_path: str | Path | None) -> Path:
    if comfyui_path is not None:
        return Path(comfyui_path)
    return Path(os.environ.get("COMFYUI_PATH", "/workspace/ComfyUI"))


def _validate_url(value: str) -> None:
    if not is_http_url(value):
        raise DownloadFilesConfigError(
            f"url must be an HTTP(S) URL with a host: {value}"
        )


def _validate_relative_path(value: str, *, field: str) -> PurePosixPath:
    result = validate_relative_file_directory(value)
    if result.path is not None:
        return result.path
    if result.code == "absolute_directory":
        raise DownloadFilesConfigError(f"{field} must be relative: {value}")
    if result.code == "parent_directory_segment":
        raise DownloadFilesConfigError(f"{field} must not contain '..': {value}")
    message = result.message or "must be a valid relative path"
    raise DownloadFilesConfigError(f"{field} {message}: {value}")


def _validate_filename(value: str) -> PurePosixPath:
    result = validate_file_name(value)
    if result.filename is None:
        if "\\" in value:
            raise DownloadFilesConfigError(f"filename must not contain '\\': {value}")
        raise DownloadFilesConfigError(f"filename must be one path component: {value}")
    return PurePosixPath(result.filename)


def _raise_for_http_status(response: httpx.Response) -> None:
    status = response.status_code
    if status in {408, 429} or 500 <= status <= 599:
        raise _RetryableDownloadError(
            f"HTTP download got retryable status {status}: {response.url}"
        )
    if 400 <= status <= 599:
        raise TransferDownloadFilesError(
            f"HTTP download got non-retryable status {status}: {response.url}"
        )


def _tmp_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.tmp")


def _aria2_control_path(target: Path) -> Path:
    return Path(f"{target}.aria2")


def _remove_aria2_control_file(item: FileDownloadItem) -> None:
    if not item.overwrite:
        return
    control_path = _aria2_control_path(item.target)
    try:
        if control_path.exists() or control_path.is_symlink():
            control_path.unlink()
    except OSError as error:
        raise DownloadFilesError(
            f"aria2 control file cannot be removed for overwrite: {control_path}: "
            f"{error}"
        ) from error


def _aria2_options(
    item: FileDownloadItem,
    settings: Aria2DownloadSettings,
) -> dict[str, str]:
    return {
        "dir": str(item.target.parent),
        "out": item.target.name,
        "split": str(settings.split),
        "max-connection-per-server": str(settings.max_connection_per_server),
        "min-split-size": settings.min_split_size,
        "continue": _aria2_bool(settings.resume_download),
    }


def _aria2_daemon_argv(
    settings: Aria2DownloadSettings,
    secret: str,
) -> list[str]:
    return [
        "aria2c",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={settings.rpc_port}",
        f"--rpc-secret={secret}",
        "--disable-ipv6=true",
        "--console-log-level=notice",
    ]


def _aria2_bool(value: bool) -> str:
    return "true" if value else "false"


def _remove_stale_tmp(tmp_path: Path) -> None:
    try:
        if tmp_path.exists() or tmp_path.is_symlink():
            tmp_path.unlink()
    except OSError as error:
        raise DownloadFilesError(
            f"stale HTTP tmp file cannot be removed: {tmp_path}: {error}"
        ) from error


def _cleanup_tmp(tmp_path: Path) -> None:
    try:
        if tmp_path.exists() or tmp_path.is_symlink():
            tmp_path.unlink()
    except OSError:
        pass


def _backoff_delay(attempt: int) -> float:
    return float((1, 2, 4)[min(attempt, 2)])
