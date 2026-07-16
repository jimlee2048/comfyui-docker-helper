"""aria2 file downloader tests."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from comfyui_docker_helper.container.download_files import (
    Aria2Downloader,
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    FileDownloadItem,
    HttpxDownloadSettings,
)


class FakeProcess:
    """Fake aria2c process with controllable daemon lifecycle."""

    def __init__(
        self,
        *,
        poll_values: list[int | None] | None = None,
        wait_timeouts: int = 0,
    ) -> None:
        self.poll_values = poll_values or []
        self.wait_timeouts = wait_timeouts
        self.wait_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        if self.poll_values:
            return self.poll_values.pop(0)
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_calls += 1
        if self.wait_timeouts > 0:
            self.wait_timeouts -= 1
            raise subprocess.TimeoutExpired("aria2c", timeout=5)
        return 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class FakeClient:
    """Fake aria2p client."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        secret: str,
        timeout: float,
        ready_errors: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.timeout = timeout
        self.ready_errors = ready_errors
        self.shutdown_calls = 0

    def get_version(self) -> dict[str, str]:
        if self.ready_errors > 0:
            self.ready_errors -= 1
            raise ConnectionError("not ready")
        return {"version": "fake"}

    def shutdown(self) -> str:
        self.shutdown_calls += 1
        return "OK"


class FakeDownload:
    """Fake aria2p Download with scripted update statuses."""

    def __init__(
        self,
        statuses: list[str],
        *,
        error_message: str | None = None,
        update_error: Exception | None = None,
    ) -> None:
        self._statuses: Iterator[str] = iter(statuses)
        self.status = "active"
        self.error_message = error_message
        self.update_error = update_error

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def is_removed(self) -> bool:
        return self.status == "removed"

    def update(self) -> None:
        if self.update_error is not None:
            raise self.update_error
        self.status = next(self._statuses, self.status)


class FakeApi:
    """Fake aria2p API recording add_uris calls."""

    def __init__(
        self,
        download: FakeDownload,
        *,
        submit_error: Exception | None = None,
    ) -> None:
        self.download = download
        self.submit_error = submit_error
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def add_uris(
        self,
        uris: list[str],
        options: dict[str, str] | None = None,
    ) -> FakeDownload:
        self.calls.append((uris, options))
        if self.submit_error is not None:
            raise self.submit_error
        return self.download


def make_settings(
    *,
    rpc_port: int = 6811,
    resume_download: bool = True,
) -> DownloaderSettings:
    """Return downloader settings for aria2 tests."""
    return DownloaderSettings(
        default="aria2",
        aria2=Aria2DownloadSettings(
            rpc_port=rpc_port,
            split=8,
            max_connection_per_server=4,
            min_split_size="2M",
            resume_download=resume_download,
        ),
        httpx=HttpxDownloadSettings(timeout=60, retries=3),
    )


def make_item(tmp_path: Path, *, overwrite: bool = False) -> FileDownloadItem:
    """Return one resolved aria2 item."""
    target = tmp_path / "models" / "model.safetensors"
    target.parent.mkdir(parents=True)
    return FileDownloadItem(
        url="https://example.test/model.safetensors",
        filename="model.safetensors",
        target=target,
        overwrite=overwrite,
        downloader="aria2",
    )


# Aria2 transfers own daemon lifecycle, typed failure mapping, and safe sidecar cleanup.
def test_aria2_downloader_starts_daemon_and_submits_options(
    tmp_path: Path,
) -> None:
    """Start aria2c on the configured port and submit sanitized options."""
    process = FakeProcess()
    argv_calls: list[list[str]] = []
    client_calls: list[FakeClient] = []
    api = FakeApi(FakeDownload(["complete"]))
    logs: list[str] = []
    secret = "test-secret"
    item = make_item(tmp_path)
    settings = make_settings()

    def process_factory(argv: list[str]) -> FakeProcess:
        argv_calls.append(argv)
        return process

    def client_factory(
        *,
        host: str,
        port: int,
        secret: str,
        timeout: float,
    ) -> FakeClient:
        client = FakeClient(host=host, port=port, secret=secret, timeout=timeout)
        client_calls.append(client)
        return client

    downloader = Aria2Downloader(
        process_factory=process_factory,
        client_factory=client_factory,
        api_factory=lambda _: api,
        secret_factory=lambda: secret,
        sleep=lambda _: None,
        log=logs.append,
    )

    downloader.download(item, settings)

    assert argv_calls == [
        [
            "aria2c",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            "--rpc-listen-port=6811",
            "--rpc-secret=test-secret",
            "--disable-ipv6=true",
            "--console-log-level=notice",
        ]
    ]
    assert client_calls[0].host == "http://localhost"
    assert client_calls[0].port == 6811
    assert client_calls[0].secret == secret
    assert api.calls == [
        (
            ["https://example.test/model.safetensors"],
            {
                "dir": str(item.target.parent),
                "out": "model.safetensors",
                "split": "8",
                "max-connection-per-server": "4",
                "min-split-size": "2M",
                "continue": "true",
            },
        )
    ]
    assert secret not in repr(settings)
    assert secret not in repr(item)
    assert secret not in repr(downloader)
    assert all(secret not in line for line in logs)


def test_aria2_downloader_context_always_shuts_down_daemon(tmp_path: Path) -> None:
    """Backend lifecycle always shuts down RPC and waits on context exit."""
    process = FakeProcess()
    client = FakeClient(host="http://localhost", port=6811, secret="s", timeout=5)
    api = FakeApi(FakeDownload(["complete"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=lambda **_: client,
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    with downloader:
        downloader.download(make_item(tmp_path), make_settings())

    assert client.shutdown_calls == 1
    assert process.wait_calls == 1


def test_aria2_downloader_context_cleans_up_after_download_failure(
    tmp_path: Path,
) -> None:
    """Download failures must not bypass aria2 context cleanup."""
    process = FakeProcess()
    client = FakeClient(host="http://localhost", port=6811, secret="s", timeout=5)
    api = FakeApi(FakeDownload(["error"], error_message="failed"))
    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=lambda **_: client,
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="failed"), downloader:
        downloader.download(make_item(tmp_path), make_settings())

    assert client.shutdown_calls == 1
    assert process.wait_calls == 1


def test_aria2_downloader_terminates_process_when_shutdown_does_not_exit(
    tmp_path: Path,
) -> None:
    """Escalate cleanup from shutdown wait to process terminate."""
    process = FakeProcess(wait_timeouts=1)
    client = FakeClient(host="http://localhost", port=6811, secret="s", timeout=5)
    api = FakeApi(FakeDownload(["complete"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=lambda **_: client,
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    with downloader:
        downloader.download(make_item(tmp_path), make_settings())

    assert client.shutdown_calls == 1
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls == 2


def test_aria2_downloader_reports_error_status(tmp_path: Path) -> None:
    """Treat aria2 terminal error status as a helper failure."""
    api = FakeApi(FakeDownload(["error"], error_message="checksum failed"))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="checksum failed"):
        downloader.download(make_item(tmp_path), make_settings())


def test_aria2_downloader_reports_removed_status(tmp_path: Path) -> None:
    """Treat aria2 removed status as a failed transfer."""
    api = FakeApi(FakeDownload(["removed"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="removed"):
        downloader.download(make_item(tmp_path), make_settings())


def test_aria2_downloader_reports_rpc_disconnect(tmp_path: Path) -> None:
    """Treat update failures as RPC disconnects instead of silent success."""
    api = FakeApi(FakeDownload(["active"], update_error=ConnectionError("lost")))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="RPC disconnected"):
        downloader.download(make_item(tmp_path), make_settings())


def test_aria2_downloader_reports_daemon_exit(tmp_path: Path) -> None:
    """Treat unexpected daemon exit before completion as a failure."""
    process = FakeProcess(poll_values=[None, 7])
    api = FakeApi(FakeDownload(["active"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="exited with code 7"):
        downloader.download(make_item(tmp_path), make_settings())


def test_aria2_downloader_reports_startup_port_failure(tmp_path: Path) -> None:
    """Fail explicitly if aria2 exits before RPC is ready."""
    process = FakeProcess(poll_values=[None, 1])
    client = FakeClient(
        host="http://localhost",
        port=6811,
        secret="s",
        timeout=5,
        ready_errors=1,
    )
    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=lambda **_: client,
        api_factory=lambda _: FakeApi(FakeDownload(["complete"])),
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="exited with code 1"):
        downloader.download(make_item(tmp_path), make_settings())


def test_aria2_downloader_removes_control_file_on_overwrite(tmp_path: Path) -> None:
    """Overwrite removes target.aria2 before submitting aria2 work."""
    item = make_item(tmp_path, overwrite=True)
    control_file = Path(f"{item.target}.aria2")
    control_file.write_text("partial\n", encoding="utf-8")
    api = FakeApi(FakeDownload(["complete"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    downloader.download(item, make_settings(resume_download=False))

    assert not control_file.exists()
    assert api.calls[0][1]["continue"] == "false"


def test_aria2_downloader_removes_symlinked_control_file_on_overwrite(
    tmp_path: Path,
) -> None:
    """Remove broken target.aria2 symlinks when overwrite=true."""
    item = make_item(tmp_path, overwrite=True)
    control_file = Path(f"{item.target}.aria2")
    control_file.symlink_to(tmp_path / "missing-control-state")
    api = FakeApi(FakeDownload(["complete"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        sleep=lambda _: None,
        log=lambda _: None,
    )

    assert not control_file.exists()
    assert control_file.is_symlink()

    downloader.download(item, make_settings(resume_download=False))

    assert not control_file.is_symlink()
    assert api.calls[0][1]["continue"] == "false"
