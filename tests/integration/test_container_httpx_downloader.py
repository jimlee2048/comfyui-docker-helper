"""HTTPX file downloader tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from comfyui_docker_helper.container.download_files import (
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    FileDownloadItem,
    HttpxDownloader,
    HttpxDownloadSettings,
)


class IteratorStream(httpx.SyncByteStream):
    """HTTPX byte stream backed by an iterator factory."""

    def __init__(self, chunks: Callable[[], Iterator[bytes]]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        return self._chunks()


def make_settings(*, retries: int = 2, timeout: int | float = 30) -> DownloaderSettings:
    """Return normalized downloader settings for HTTPX tests."""
    return DownloaderSettings(
        default="httpx",
        aria2=Aria2DownloadSettings(
            rpc_port=6811,
            split=8,
            max_connection_per_server=4,
            min_split_size="2M",
            resume_download=False,
        ),
        httpx=HttpxDownloadSettings(timeout=timeout, retries=retries),
    )


def make_item(
    tmp_path: Path,
    *,
    url: str = "https://example.test/file.bin",
) -> FileDownloadItem:
    """Return one resolved file item with an existing target parent."""
    target = tmp_path / "models" / "file.bin"
    target.parent.mkdir(parents=True)
    return FileDownloadItem(
        url=url,
        directory="models",
        filename="file.bin",
        target=target,
        overwrite=False,
        downloader="httpx",
    )


def make_downloader(
    transport: httpx.MockTransport,
    *,
    sleeps: list[float] | None = None,
    logs: list[str] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> HttpxDownloader:
    """Build an HTTPX downloader with test doubles for side effects."""
    return HttpxDownloader(
        transport=transport,
        sleep=(sleeps if sleeps is not None else []).append,
        log=(logs if logs is not None else []).append,
        monotonic=monotonic or (lambda: 0.0),
    )


def test_httpx_downloader_streams_to_tmp_then_renames(tmp_path: Path) -> None:
    """HTTPX writes through a tmp file before atomically publishing target."""
    item = make_item(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == item.url
        return httpx.Response(200, content=b"downloaded")

    downloader = make_downloader(httpx.MockTransport(handler))

    downloader.download(item, make_settings())

    assert item.target.read_bytes() == b"downloaded"
    assert not item.target.with_name("file.bin.tmp").exists()


def test_httpx_downloader_follows_redirects(tmp_path: Path) -> None:
    """Allow HTTP redirects while retaining one final target write."""
    item = make_item(tmp_path, url="https://example.test/redirect")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, content=b"final")

    downloader = make_downloader(httpx.MockTransport(handler))

    downloader.download(item, make_settings())

    assert requests == ["https://example.test/redirect", "https://example.test/final"]
    assert item.target.read_bytes() == b"final"


def test_httpx_downloader_removes_stale_tmp_before_download(tmp_path: Path) -> None:
    """Remove stale tmp files before beginning a new HTTPX attempt."""
    item = make_item(tmp_path)
    tmp_file = item.target.with_name("file.bin.tmp")
    tmp_file.write_bytes(b"stale")
    downloader = make_downloader(
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"fresh"))
    )

    downloader.download(item, make_settings())

    assert item.target.read_bytes() == b"fresh"
    assert not tmp_file.exists()


def test_httpx_downloader_removes_broken_tmp_symlink_before_write(
    tmp_path: Path,
) -> None:
    """Remove tmp symlinks before streaming so writes stay in target tree."""
    item = make_item(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "escaped.bin"
    tmp_file = item.target.with_name("file.bin.tmp")
    tmp_file.symlink_to(outside_file)

    def chunks() -> Iterator[bytes]:
        assert not outside_file.exists()
        assert tmp_file.exists()
        assert not tmp_file.is_symlink()
        yield b"fresh"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=IteratorStream(chunks))

    downloader = make_downloader(httpx.MockTransport(handler))

    assert not tmp_file.exists()
    assert tmp_file.is_symlink()

    downloader.download(item, make_settings())

    assert item.target.read_bytes() == b"fresh"
    assert not tmp_file.exists()
    assert not tmp_file.is_symlink()
    assert not outside_file.exists()


def test_httpx_downloader_does_not_retry_non_retryable_status(
    tmp_path: Path,
) -> None:
    """Ordinary 4xx responses are terminal and leave no partial file."""
    item = make_item(tmp_path)
    sleeps: list[float] = []
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(404, content=b"missing")

    downloader = make_downloader(httpx.MockTransport(handler), sleeps=sleeps)

    with pytest.raises(DownloadFilesError, match="non-retryable status 404"):
        downloader.download(item, make_settings(retries=3))

    assert requests == 1
    assert sleeps == []
    assert not item.target.exists()
    assert not item.target.with_name("file.bin.tmp").exists()


def test_httpx_downloader_retries_retryable_status_then_succeeds(
    tmp_path: Path,
) -> None:
    """Retry 408/429/5xx responses with deterministic backoff."""
    item = make_item(tmp_path)
    sleeps: list[float] = []
    statuses = [503, 429, 200]

    def handler(_: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(status, content=b"ok")
        return httpx.Response(status, content=b"retry")

    downloader = make_downloader(httpx.MockTransport(handler), sleeps=sleeps)

    downloader.download(item, make_settings(retries=3))

    assert item.target.read_bytes() == b"ok"
    assert sleeps == [1.0, 2.0]


def test_httpx_downloader_retries_timeout_and_transport_errors(
    tmp_path: Path,
) -> None:
    """Retry HTTPX timeout and transport exceptions before succeeding."""
    item = make_item(tmp_path)
    sleeps: list[float] = []
    errors: list[Exception | None] = [
        httpx.ConnectTimeout("timed out"),
        httpx.TransportError("connection reset"),
        None,
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        error = errors.pop(0)
        if error is not None:
            raise error
        return httpx.Response(200, content=b"ok")

    downloader = make_downloader(httpx.MockTransport(handler), sleeps=sleeps)

    downloader.download(item, make_settings(retries=2))

    assert item.target.read_bytes() == b"ok"
    assert sleeps == [1.0, 2.0]


def test_httpx_downloader_exhausts_retries_and_cleans_tmp(tmp_path: Path) -> None:
    """Surface retry exhaustion and remove partial HTTPX tmp state."""
    item = make_item(tmp_path)
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"server error")

    downloader = make_downloader(httpx.MockTransport(handler), sleeps=sleeps)

    with pytest.raises(DownloadFilesError, match="retryable status 500"):
        downloader.download(item, make_settings(retries=2))

    assert sleeps == [1.0, 2.0]
    assert not item.target.exists()
    assert not item.target.with_name("file.bin.tmp").exists()


def test_httpx_downloader_cleans_tmp_on_stream_failure(
    tmp_path: Path,
) -> None:
    """Remove tmp files when response streaming fails after bytes were written."""
    item = make_item(tmp_path)
    sleeps: list[float] = []

    def chunks() -> Iterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("stream failed")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=IteratorStream(chunks))

    downloader = make_downloader(httpx.MockTransport(handler), sleeps=sleeps)

    with pytest.raises(DownloadFilesError, match="stream failed"):
        downloader.download(item, make_settings(retries=1))

    assert sleeps == [1.0]
    assert not item.target.exists()
    assert not item.target.with_name("file.bin.tmp").exists()


def test_httpx_downloader_rate_limits_progress_logs(tmp_path: Path) -> None:
    """Emit progress logs only when the configured interval has elapsed."""
    item = make_item(tmp_path)
    logs: list[str] = []
    clock_values = iter([0.0, 1.0, 4.0, 6.0])

    downloader = make_downloader(
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"abc")),
        logs=logs,
        monotonic=lambda: next(clock_values),
    )
    downloader.chunk_size = 1

    downloader.download(item, make_settings())

    assert len(logs) == 1
    assert "Downloaded" in logs[0]
    assert item.target.read_bytes() == b"abc"
