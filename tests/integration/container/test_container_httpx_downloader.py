"""HTTPX supplied-staging transport adapter tests."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from comfyui_docker_helper.container import download_files as download_files_module
from comfyui_docker_helper.container.download_files import (
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    HttpxDownloader,
    HttpxDownloadSettings,
    TransportCancelled,
    TransportOrdinaryTerminal,
    TransportRequest,
    TransportRetryable,
    TransportSuccess,
)
from comfyui_docker_helper.container.downloader_credentials import (
    DownloaderCredentialError,
)
from comfyui_docker_helper.container.transfer_core import (
    FileTransferRequest,
    StagingDisposition,
    transfer_file,
    transfer_staging_target,
)


class IteratorStream(httpx.AsyncByteStream):
    """HTTPX byte stream backed by an iterator factory."""

    def __init__(self, chunks: Callable[[], Iterator[bytes]]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks():
            yield chunk


class PathSink:
    """Test sink exposing only the safe adapter operations."""

    def __init__(self, path: Path) -> None:
        self.display_path = path

    def open_for_write(self):
        return self.display_path.open("wb")


def _settings() -> DownloaderSettings:
    return DownloaderSettings(
        default="httpx",
        aria2=Aria2DownloadSettings(
            rpc_port=6811,
            split=8,
            max_connection_per_server=4,
            min_split_size="2M",
            resume_download=False,
        ),
        httpx=HttpxDownloadSettings(timeout=30),
    )


def _request(
    tmp_path: Path, *, url: str = "https://example.test/file.bin"
) -> TransportRequest:
    staging = tmp_path / "models" / ".cdh-staging" / "cdh-test.part"
    staging.parent.mkdir(parents=True)
    return TransportRequest(url=url, sink=PathSink(staging))


def _downloader(
    transport: httpx.MockTransport,
    *,
    logs: list[str] | None = None,
    monotonic: Callable[[], float] | None = None,
    wall_clock: Callable[[], float] | None = None,
) -> HttpxDownloader:
    return HttpxDownloader(
        transport=transport,
        log=(logs if logs is not None else []).append,
        monotonic=monotonic or (lambda: 0.0),
        wall_clock=wall_clock or time.time,
    )


def _core_request(root: Path) -> FileTransferRequest:
    return FileTransferRequest(
        root=root,
        url="https://example.test/file.bin",
        target=root / "models" / "file.bin",
        overwrite=True,
        expected_checksum=None,
        staging_disposition=StagingDisposition.CLEAN,
    )


# The adapter owns HTTP mechanics only; the shared core owns final placement.
def test_httpx_writes_only_supplied_staging_and_reports_length(tmp_path: Path) -> None:
    request = _request(tmp_path)
    final = tmp_path / "models" / "file.bin"
    downloader = _downloader(
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"downloaded"))
    )

    result = downloader.download(request, _settings())

    assert isinstance(result, TransportSuccess)
    assert result.length == len(b"downloaded")
    assert result.namespace == "httpx"
    assert result.http_status == 200
    assert request.sink.display_path.read_bytes() == b"downloaded"
    assert not final.exists()
    assert not request.sink.display_path.with_suffix(".tmp").exists()


def test_httpx_follows_redirects_without_changing_supplied_target(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, url="https://example.test/redirect")
    requests: list[str] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        requests.append(str(http_request.url))
        if http_request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, content=b"final")

    result = _downloader(httpx.MockTransport(handler)).download(request, _settings())

    assert requests == ["https://example.test/redirect", "https://example.test/final"]
    assert isinstance(result, TransportSuccess)
    assert result.length == 5
    assert result.http_status == 200
    assert request.sink.display_path.read_bytes() == b"final"


class _PathCredentialPolicy:
    def authorization_for(self, url: httpx.URL) -> bytes | None:
        if url.host == "example.test" and url.path.startswith("/private/"):
            return b"Bearer route-a"
        if url.host == "other.test" and url.path.startswith("/protected/"):
            return b"Bearer route-b"
        return None


def test_httpx_reselects_cdh_authorization_for_every_redirect(
    tmp_path: Path,
) -> None:
    observed: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((str(request.url), request.headers.get("Authorization")))
        locations = {
            "/private/start": "/private/next",
            "/private/next": "/public",
            "/public": "https://other.test/protected/final",
        }
        location = locations.get(request.url.path)
        if location is not None:
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, content=b"authenticated")

    result = HttpxDownloader(
        transport=httpx.MockTransport(handler),
        credential_policy=_PathCredentialPolicy(),
        log=lambda _: None,
    ).download(
        _request(tmp_path, url="https://example.test/private/start"),
        _settings(),
    )

    assert isinstance(result, TransportSuccess)
    assert observed == [
        ("https://example.test/private/start", "Bearer route-a"),
        ("https://example.test/private/next", "Bearer route-a"),
        ("https://example.test/public", None),
        ("https://other.test/protected/final", "Bearer route-b"),
    ]


def test_httpx_preserves_unowned_authorization_until_a_route_matches(
    tmp_path: Path,
) -> None:
    observed: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers.get("Authorization"))
        if request.url.path == "/public":
            return httpx.Response(302, headers={"Location": "/private/final"})
        return httpx.Response(200, content=b"authenticated")

    result = HttpxDownloader(
        transport=httpx.MockTransport(handler),
        credential_policy=_PathCredentialPolicy(),
        log=lambda _: None,
    ).download(
        _request(tmp_path, url="https://user:password@example.test/public"),
        _settings(),
    )

    assert isinstance(result, TransportSuccess)
    assert observed == ["Basic dXNlcjpwYXNzd29yZA==", "Bearer route-a"]


class _FailingCredentialPolicy:
    def authorization_for(self, url: httpx.URL) -> bytes | None:
        if url.path.startswith("/private/"):
            raise DownloaderCredentialError("Downloader credential is unavailable")
        return None


def test_httpx_reports_initial_credential_failure_before_network(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    downloader = HttpxDownloader(
        transport=httpx.MockTransport(handler),
        credential_policy=_FailingCredentialPolicy(),
        log=lambda _: None,
    )

    with pytest.raises(DownloaderCredentialError) as caught:
        downloader.download(
            _request(tmp_path, url="https://example.test/private/file"),
            _settings(),
        )

    assert calls == 0
    assert caught.value.network_attempted is False


def test_httpx_retains_network_attempt_when_redirect_enters_failing_route(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "/private/file"})

    downloader = HttpxDownloader(
        transport=httpx.MockTransport(handler),
        credential_policy=_FailingCredentialPolicy(),
        log=lambda _: None,
    )

    with pytest.raises(DownloaderCredentialError) as caught:
        downloader.download(
            _request(tmp_path, url="https://example.test/public"),
            _settings(),
        )

    assert calls == 1
    assert caught.value.network_attempted is True


def test_httpx_preserves_terminal_status_after_redirect(tmp_path: Path) -> None:
    """Redirect handling retains the exact final response status."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/missing"})
        return httpx.Response(404)

    result = _downloader(httpx.MockTransport(handler)).download(
        _request(tmp_path, url="https://example.test/redirect"),
        _settings(),
    )

    assert isinstance(result, TransportOrdinaryTerminal)
    assert result.http_status == 404


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_httpx_retryable_response_is_one_adapter_attempt(
    tmp_path: Path,
    status: int,
) -> None:
    request = _request(tmp_path)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=b"retry")

    result = _downloader(httpx.MockTransport(handler)).download(request, _settings())

    assert calls == 1
    assert isinstance(result, TransportRetryable)
    assert result.http_status == status


# Retry-After is normalized only from the exact retryable response boundary.
def test_httpx_normalizes_delta_and_http_date_retry_after(tmp_path: Path) -> None:
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    responses = iter(
        [
            httpx.Response(503, headers={"Retry-After": "10"}),
            httpx.Response(
                429,
                headers={"Retry-After": format_datetime(now + timedelta(seconds=20))},
            ),
        ]
    )
    downloader = _downloader(
        httpx.MockTransport(lambda _: next(responses)),
        wall_clock=now.timestamp,
    )

    delta = downloader.download(_request(tmp_path / "delta"), _settings())
    dated = downloader.download(_request(tmp_path / "dated"), _settings())

    assert isinstance(delta, TransportRetryable)
    assert delta.retry_after_seconds == 10
    assert isinstance(dated, TransportRetryable)
    assert dated.retry_after_seconds == 20


@pytest.mark.parametrize(
    ("headers", "oversized_value_size"),
    [
        pytest.param([(b"Retry-After", b"-1")], None, id="negative-delta"),
        pytest.param([(b"Retry-After", b"1.5")], None, id="fractional-delta"),
        pytest.param([(b"Retry-After", b"not-a-date")], None, id="invalid-date"),
        pytest.param(None, 10_000, id="oversized"),
        pytest.param(
            [(b"Retry-After", b"10"), (b"Retry-After", b"20")],
            None,
            id="duplicate",
        ),
        pytest.param(
            [(b"Retry-After", b"Wed, 01 Jan 2020 00:00:00 GMT")],
            None,
            id="past-date",
        ),
    ],
)
def test_httpx_ignores_invalid_ambiguous_or_past_retry_after(
    tmp_path: Path,
    headers: list[tuple[bytes, bytes]] | None,
    oversized_value_size: int | None,
) -> None:
    if oversized_value_size is not None:
        headers = [(b"Retry-After", b"9" * oversized_value_size)]
    assert headers is not None
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    downloader = _downloader(
        httpx.MockTransport(lambda _: httpx.Response(503, headers=headers)),
        wall_clock=now.timestamp,
    )

    result = downloader.download(_request(tmp_path), _settings())

    assert isinstance(result, TransportRetryable)
    assert result.retry_after_seconds is None


# Ordinary request failures are terminal so an orchestrator will not retry them.
@pytest.mark.parametrize("status", [400, 401, 404])
def test_httpx_non_retryable_response_is_terminal(
    tmp_path: Path,
    status: int,
) -> None:
    request = _request(tmp_path)

    result = _downloader(
        httpx.MockTransport(lambda _: httpx.Response(status, content=b"terminal"))
    ).download(request, _settings())

    assert isinstance(result, TransportOrdinaryTerminal)
    assert result.http_status == status


def test_httpx_stream_failure_leaves_exact_partial_for_core_disposition(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    def chunks() -> Iterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("stream failed")

    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=IteratorStream(chunks))
    )

    result = _downloader(transport).download(request, _settings())

    assert isinstance(result, TransportRetryable)
    assert result.http_status is None
    assert request.sink.display_path.is_file()


def test_httpx_local_protocol_error_fails_closed(tmp_path: Path) -> None:
    """Local request/protocol invariants never enter ordinary retry policy."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError("invalid local request")

    with pytest.raises(DownloadFilesError, match="transport invariant"):
        _downloader(httpx.MockTransport(handler)).download(
            _request(tmp_path),
            _settings(),
        )


def test_httpx_cancellation_before_start_returns_cancelled(tmp_path: Path) -> None:
    """Pre-start cancellation never opens the remote transport."""
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"late")

    downloader = _downloader(httpx.MockTransport(handler))
    downloader.cancel()

    result = downloader.download(_request(tmp_path), _settings())

    assert isinstance(result, TransportCancelled)
    assert calls == 0


def test_httpx_cancel_between_task_creation_and_registration_is_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation wins even before the top-level task registers itself."""
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"late")

    downloader = _downloader(httpx.MockTransport(handler))
    original_run = asyncio.run

    def cancel_then_run(coroutine):
        downloader.cancel()
        return original_run(coroutine)

    monkeypatch.setattr(download_files_module.asyncio, "run", cancel_then_run)

    result = downloader.download(_request(tmp_path), _settings())

    assert isinstance(result, TransportCancelled)
    assert calls == 0


def test_httpx_cancellation_during_stream_is_not_success(tmp_path: Path) -> None:
    """An active stream observes cancellation before writing later bytes."""
    request = _request(tmp_path)
    downloader: HttpxDownloader

    def chunks() -> Iterator[bytes]:
        yield b"partial"
        downloader.cancel()
        yield b"late"

    downloader = _downloader(
        httpx.MockTransport(
            lambda _: httpx.Response(200, stream=IteratorStream(chunks))
        )
    )

    result = downloader.download(request, _settings())

    assert isinstance(result, TransportCancelled)
    assert request.sink.display_path.read_bytes() in {b"", b"partial"}


def test_httpx_stalled_response_body_cancels_within_a_fixed_bound(
    tmp_path: Path,
) -> None:
    """A real partial HTTP body cannot strand the synchronous caller on cancel."""
    partial_sent = threading.Event()
    server_done = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2.0)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2.0)
                received = b""
                while b"\r\n\r\n" not in received:
                    received += connection.recv(4096)
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 1048576\r\n"
                    b"Content-Type: application/octet-stream\r\n"
                    b"Connection: close\r\n\r\n"
                    b"partial"
                )
                partial_sent.set()
                while connection.recv(4096):
                    pass
        except (OSError, TimeoutError):
            pass
        finally:
            listener.close()
            server_done.set()

    server = threading.Thread(target=serve, name="httpx-test-server")
    server.start()
    downloader = HttpxDownloader(log=lambda _: None)
    results: list[object] = []
    worker = threading.Thread(
        target=lambda: results.append(
            downloader.download(
                _request(tmp_path, url=f"http://127.0.0.1:{port}/stalled"),
                _settings(),
            )
        ),
        name="httpx-test-download",
    )
    worker.start()
    assert partial_sent.wait(1.0)

    started = time.monotonic()
    downloader.cancel()
    downloader.cancel()
    worker.join(1.0)
    elapsed = time.monotonic() - started
    server.join(2.0)

    assert elapsed < 1.0
    assert not worker.is_alive()
    assert server_done.is_set()
    assert len(results) == 1
    assert isinstance(results[0], TransportCancelled)
    assert not any(
        thread.name.startswith("cdh-httpx") for thread in threading.enumerate()
    )


def test_httpx_unrequested_task_cancellation_fails_closed(tmp_path: Path) -> None:
    """Only the adapter cancellation token may produce a cancelled outcome."""

    class UnexpectedCancellation(httpx.AsyncByteStream):
        async def __aiter__(self):
            if False:
                yield b""
            raise asyncio.CancelledError

    downloader = _downloader(
        httpx.MockTransport(
            lambda _: httpx.Response(200, stream=UnexpectedCancellation())
        )
    )

    with pytest.raises(DownloadFilesError, match="without a cdh cancellation"):
        downloader.download(_request(tmp_path), _settings())


def test_httpx_completed_result_precedes_later_repeated_cancel(tmp_path: Path) -> None:
    """Cancellation after conclusive completion cannot rewrite success."""
    downloader = _downloader(
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"complete"))
    )

    result = downloader.download(_request(tmp_path), _settings())
    downloader.cancel()
    downloader.cancel()

    assert isinstance(result, TransportSuccess)
    assert result.length == len(b"complete")


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection refused"),
        httpx.ConnectError("name resolution failed"),
    ],
)
def test_httpx_timeout_connection_and_dns_failures_are_retryable(
    tmp_path: Path,
    error: httpx.RequestError,
) -> None:
    """Observable transient transport failures stay policy-eligible."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise error

    result = _downloader(httpx.MockTransport(handler)).download(
        _request(tmp_path), _settings()
    )

    assert isinstance(result, TransportRetryable)
    assert result.http_status is None


def test_httpx_too_many_redirects_is_ordinary_terminal(tmp_path: Path) -> None:
    """Redirect exhaustion is terminal without inventing an HTTP status."""
    result = _downloader(
        httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "/again"})
        )
    ).download(_request(tmp_path), _settings())

    assert isinstance(result, TransportOrdinaryTerminal)
    assert result.http_status is None


def test_httpx_decoding_failure_fails_closed(tmp_path: Path) -> None:
    """Malformed response decoding is not guessed into remote retry policy."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("malformed content encoding")

    with pytest.raises(DownloadFilesError, match="transport invariant"):
        _downloader(httpx.MockTransport(handler)).download(
            _request(tmp_path), _settings()
        )


def test_httpx_progress_logs_name_supplied_staging(tmp_path: Path) -> None:
    request = _request(tmp_path)
    logs: list[str] = []
    clock_values = iter([0.0, 1.0, 4.0, 6.0])
    downloader = _downloader(
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"abc")),
        logs=logs,
        monotonic=lambda: next(clock_values),
    )
    downloader.chunk_size = 1

    downloader.download(request, _settings())

    assert logs == [f"Downloaded 3 bytes to {request.sink.display_path}"]


def test_httpx_write_failure_is_local_and_does_not_place_a_final(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request.sink.display_path.mkdir()

    with pytest.raises(DownloadFilesError, match="supplied staging"):
        _downloader(
            httpx.MockTransport(lambda _: httpx.Response(200, content=b"data"))
        ).download(request, _settings())

    assert not (tmp_path / "models" / "file.bin").exists()


# Root replacement after admission cannot redirect the real HTTPX adapter.
def test_httpx_root_replacement_race_cannot_escape_held_descriptor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _core_request(root)
    detached = tmp_path / "detached-root"
    outside = tmp_path / "outside"
    outside.mkdir()

    def handler(_: httpx.Request) -> httpx.Response:
        root.rename(detached)
        root.symlink_to(outside, target_is_directory=True)
        return httpx.Response(200, content=b"downloaded")

    with pytest.raises(DownloadFilesError, match="directory changed"):
        transfer_file(
            request,
            backend=_downloader(httpx.MockTransport(handler)),
            settings=_settings(),
        )

    assert tuple(outside.iterdir()) == ()
    assert not (detached / "models" / "file.bin").exists()


# Intermediate replacement likewise cannot redirect staged bytes or cleanup.
def test_httpx_intermediate_parent_race_cannot_escape_held_descriptor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    (root / "models").mkdir(parents=True)
    request = _core_request(root)
    detached = root / "detached-models"
    outside = tmp_path / "outside"
    outside.mkdir()

    def handler(_: httpx.Request) -> httpx.Response:
        (root / "models").rename(detached)
        (root / "models").symlink_to(outside, target_is_directory=True)
        return httpx.Response(200, content=b"downloaded")

    with pytest.raises(DownloadFilesError, match="directory changed"):
        transfer_file(
            request,
            backend=_downloader(httpx.MockTransport(handler)),
            settings=_settings(),
        )

    assert tuple(outside.iterdir()) == ()
    assert not (detached / "file.bin").exists()


# Replacing the staging leaf cannot redirect HTTPX through a symlink.
def test_httpx_staging_leaf_race_never_writes_external_target(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = _core_request(root)
    external = tmp_path / "external.bin"
    external.write_bytes(b"external")

    def handler(_: httpx.Request) -> httpx.Response:
        staging = transfer_staging_target(request)
        staging.unlink()
        staging.symlink_to(external)
        return httpx.Response(200, content=b"downloaded")

    with pytest.raises(DownloadFilesError, match="identity changed"):
        transfer_file(
            request,
            backend=_downloader(httpx.MockTransport(handler)),
            settings=_settings(),
        )

    assert external.read_bytes() == b"external"
    assert not request.target.exists()
