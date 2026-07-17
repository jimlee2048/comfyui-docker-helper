"""HTTPX supplied-staging transport adapter tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from comfyui_docker_helper.container.download_files import (
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    HttpxDownloader,
    HttpxDownloadSettings,
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
    TransportRequest,
)
from comfyui_docker_helper.container.transfer_core import (
    FileTransferRequest,
    StagingDisposition,
    transfer_file,
    transfer_staging_target,
)


class IteratorStream(httpx.SyncByteStream):
    """HTTPX byte stream backed by an iterator factory."""

    def __init__(self, chunks: Callable[[], Iterator[bytes]]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        return self._chunks()


class PathSink:
    """Test sink exposing only the safe adapter operations."""

    def __init__(self, path: Path) -> None:
        self.display_path = path

    def open_for_write(self):
        return self.display_path.open("wb")


def _settings(*, retries: int = 0) -> DownloaderSettings:
    return DownloaderSettings(
        default="httpx",
        aria2=Aria2DownloadSettings(
            rpc_port=6811,
            split=8,
            max_connection_per_server=4,
            min_split_size="2M",
            resume_download=False,
        ),
        httpx=HttpxDownloadSettings(timeout=30, retries=retries),
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
    sleeps: list[float] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> HttpxDownloader:
    return HttpxDownloader(
        transport=transport,
        log=(logs if logs is not None else []).append,
        sleep=(sleeps if sleeps is not None else []).append,
        monotonic=monotonic or (lambda: 0.0),
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

    assert result.length == len(b"downloaded")
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
    assert result.length == 5
    assert request.sink.display_path.read_bytes() == b"final"


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

    with pytest.raises(TransferDownloadFilesError, match="retryable status"):
        _downloader(httpx.MockTransport(handler)).download(request, _settings())

    assert calls == 1


# Public HTTPX retries execute clean attempts against the same safe sink.
def test_httpx_retries_clean_same_supplied_sink_before_success(tmp_path: Path) -> None:
    request = _request(tmp_path)
    sleeps: list[float] = []
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, content=b"partial")
        return httpx.Response(200, content=b"complete")

    result = _downloader(
        httpx.MockTransport(handler),
        sleeps=sleeps,
    ).download(request, _settings(retries=2))

    assert attempts == 3
    assert sleeps == [1.0, 2.0]
    assert result.length == len(b"complete")
    assert request.sink.display_path.read_bytes() == b"complete"


# Ordinary request failures are terminal so an orchestrator will not retry them.
@pytest.mark.parametrize("status", [400, 401, 404])
def test_httpx_non_retryable_response_is_terminal(
    tmp_path: Path,
    status: int,
) -> None:
    request = _request(tmp_path)

    with pytest.raises(TerminalTransferDownloadFilesError, match=str(status)):
        _downloader(
            httpx.MockTransport(lambda _: httpx.Response(status, content=b"terminal"))
        ).download(request, _settings())


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

    with pytest.raises(TransferDownloadFilesError, match="stream failed"):
        _downloader(transport).download(request, _settings())

    assert request.sink.display_path.is_file()


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
