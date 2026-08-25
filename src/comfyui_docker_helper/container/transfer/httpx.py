"""HTTPX transfer adapter."""

from __future__ import annotations

import asyncio
import email.utils
import math
import threading
import time
from contextlib import suppress

import httpx

from comfyui_docker_helper.container.transfer.core import (
    _MAX_TRANSFER_BYTES,
    DownloaderSettings,
    DownloadFilesError,
    Monotonic,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportOutcome,
    TransportRequest,
    TransportRetryable,
    TransportSuccess,
    _transport_cancelled,
)
from comfyui_docker_helper.container.transfer.credentials import (
    DownloaderCredentialError,
    DownloaderCredentialPolicy,
)
from comfyui_docker_helper.container.transfer.events import (
    DownloadRetryReason,
    DownloadTransferProgress,
)


class HttpxDownloader:
    """HTTPX adapter that writes one response to supplied staging."""

    chunk_size = 1024 * 1024

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Monotonic = time.monotonic,
        wall_clock: Monotonic = time.time,
        credential_policy: DownloaderCredentialPolicy | None = None,
    ) -> None:
        self._transport = transport
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._credential_policy = credential_policy
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
            outcome = await self._download_once(request, settings)
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
                    event_hooks={
                        "request": [self._apply_credential],
                        "response": [self._record_network_response],
                    },
                ) as client,
                client.stream("GET", request.url) as response,
            ):
                failure = _http_failure_outcome(
                    response,
                    wall_clock=self._wall_clock,
                )
                if failure is not None:
                    return failure
                length = await self._write_response(response, request)
                return TransportSuccess(
                    length=length,
                    namespace="httpx",
                    http_status=response.status_code,
                )
        except httpx.TooManyRedirects:
            return TransportOrdinaryTerminal(
                diagnostic=TransportDiagnostic(
                    namespace="httpx",
                    summary="HTTP download exceeded redirect limits",
                ),
                http_status=None,
            )
        except httpx.TimeoutException:
            if self._cancel_requested.is_set():
                return _transport_cancelled("httpx")
            return TransportRetryable(
                diagnostic=TransportDiagnostic(
                    namespace="httpx",
                    summary="HTTP transfer timed out",
                ),
                reason=DownloadRetryReason.TIMEOUT,
            )
        except (
            httpx.NetworkError,
            httpx.ProxyError,
            httpx.RemoteProtocolError,
        ):
            if self._cancel_requested.is_set():
                return _transport_cancelled("httpx")
            return TransportRetryable(
                diagnostic=TransportDiagnostic(
                    namespace="httpx",
                    summary="HTTP network transfer failed",
                ),
                reason=DownloadRetryReason.NETWORK,
            )
        except (httpx.TransportError, httpx.RequestError) as error:
            raise DownloadFilesError("HTTP transport invariant failed") from error

    async def _apply_credential(self, request: httpx.Request) -> None:
        policy = self._credential_policy
        if policy is None:
            return

        previous = request.extensions.pop("cdh.downloader.authorization", None)
        if isinstance(previous, bytes) and _authorization_value(request) == previous:
            request.headers.pop("Authorization", None)

        try:
            authorization = policy.authorization_for(request.url)
        except DownloaderCredentialError as error:
            if request.extensions.get("cdh.downloader.network-attempted") is True:
                error.network_attempted = True
            raise
        if authorization is None:
            return
        request.headers["Authorization"] = authorization.decode("ascii")
        request.extensions["cdh.downloader.authorization"] = authorization

    async def _record_network_response(self, response: httpx.Response) -> None:
        response.request.extensions["cdh.downloader.network-attempted"] = True

    async def _write_response(
        self,
        response: httpx.Response,
        request: TransportRequest,
    ) -> int:
        stored_bytes = 0
        total_bytes = _http_content_length(response)
        started_at = self._monotonic()
        _emit_transfer_progress(
            request,
            transferred_bytes=0,
            total_bytes=total_bytes,
            stored_bytes=0,
            reported_rate=None,
        )
        try:
            output = request.sink.open_for_write()
        except OSError as error:
            raise DownloadFilesError(
                "HTTP download failed while writing supplied staging"
            ) from error
        try:
            async for chunk in response.aiter_bytes(chunk_size=self.chunk_size):
                if self._cancel_requested.is_set():
                    raise asyncio.CancelledError
                if not chunk:
                    continue
                try:
                    output.write(chunk)
                except OSError as error:
                    raise DownloadFilesError(
                        "HTTP download failed while writing supplied staging"
                    ) from error
                stored_bytes += len(chunk)
                transferred_bytes = response.num_bytes_downloaded
                if total_bytes is not None and transferred_bytes > total_bytes:
                    total_bytes = None
                _emit_transfer_progress(
                    request,
                    transferred_bytes=transferred_bytes,
                    total_bytes=total_bytes,
                    stored_bytes=stored_bytes,
                    reported_rate=_average_transfer_rate(
                        transferred_bytes,
                        started_at=started_at,
                        now=self._monotonic(),
                    ),
                )
        except BaseException:
            with suppress(OSError):
                output.close()
            raise
        else:
            try:
                output.close()
            except OSError as error:
                raise DownloadFilesError(
                    "HTTP download failed while writing supplied staging"
                ) from error
        transferred_bytes = response.num_bytes_downloaded
        if total_bytes is not None and transferred_bytes > total_bytes:
            total_bytes = None
        _emit_transfer_progress(
            request,
            transferred_bytes=transferred_bytes,
            total_bytes=total_bytes,
            stored_bytes=stored_bytes,
            reported_rate=_average_transfer_rate(
                transferred_bytes,
                started_at=started_at,
                now=self._monotonic(),
            ),
        )
        return stored_bytes

    def cancel(self, *, deadline: float | None = None) -> None:
        del deadline
        with self._active_lock:
            if self._cancel_requested.is_set():
                return
            self._cancel_requested.set()
            active = self._active
        if active is not None:
            loop, task = active
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)

    def force_cancel(self) -> None:
        """Cancel the active request without introducing a new wait budget."""
        self.cancel()


def _authorization_value(request: httpx.Request) -> bytes | None:
    values = [
        value for name, value in request.headers.raw if name.lower() == b"authorization"
    ]
    return values[0] if len(values) == 1 else None


def _http_content_length(response: httpx.Response) -> int | None:
    values = [
        value
        for name, value in response.headers.raw
        if name.lower() == b"content-length"
    ]
    if len(values) != 1:
        return None
    value = values[0].strip(b" \t")
    if not value or any(byte < ord("0") or byte > ord("9") for byte in value):
        return None
    comparable_value = value.lstrip(b"0") or b"0"
    if len(comparable_value) > len(str(_MAX_TRANSFER_BYTES)):
        return None
    try:
        parsed = int(comparable_value)
    except (ValueError, OverflowError):
        return None
    return parsed if parsed <= _MAX_TRANSFER_BYTES else None


def _average_transfer_rate(
    transferred_bytes: int,
    *,
    started_at: float,
    now: float,
) -> float | None:
    elapsed = now - started_at
    if not math.isfinite(elapsed) or elapsed <= 0:
        return None
    rate = transferred_bytes / elapsed
    return rate if math.isfinite(rate) else None


def _emit_transfer_progress(
    request: TransportRequest,
    *,
    transferred_bytes: int,
    total_bytes: int | None,
    stored_bytes: int | None,
    reported_rate: int | float | None,
) -> None:
    if request.progress_sink is not None:
        request.progress_sink.emit(
            DownloadTransferProgress(
                transferred_bytes=transferred_bytes,
                total_bytes=total_bytes,
                stored_bytes=stored_bytes,
                reported_rate=reported_rate,
            )
        )


def _http_failure_outcome(
    response: httpx.Response,
    *,
    wall_clock: Monotonic = time.time,
) -> TransportRetryable | TransportOrdinaryTerminal | None:
    status = response.status_code
    if status in {408, 429} or 500 <= status <= 599:
        reason = (
            DownloadRetryReason.TIMEOUT
            if status == 408
            else (
                DownloadRetryReason.RATE_LIMITED
                if status == 429
                else DownloadRetryReason.TEMPORARY_SERVER
            )
        )
        return TransportRetryable(
            diagnostic=TransportDiagnostic(
                namespace="httpx",
                summary=f"HTTP download got retryable status {status}",
            ),
            http_status=status,
            retry_after_seconds=_normalized_retry_after(
                response,
                wall_clock=wall_clock,
            ),
            reason=reason,
        )
    if 400 <= status <= 599:
        return TransportOrdinaryTerminal(
            diagnostic=TransportDiagnostic(
                namespace="httpx",
                summary=f"HTTP download got non-retryable status {status}",
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
