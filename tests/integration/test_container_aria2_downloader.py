"""aria2 file downloader tests."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from comfyui_docker_helper.container.download_files import (
    Aria2Downloader,
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    HttpxDownloadSettings,
    TransportCancelled,
    TransportOrdinaryTerminal,
    TransportRequest,
    TransportRetryable,
    TransportSuccess,
)
from comfyui_docker_helper.container.transfer_core import (
    DownloadCancelled,
    FileTransferRequest,
    StagingDisposition,
    transfer_file,
    transfer_staging_target,
)


class LifecycleSentinel(BaseException):
    """Non-Exception interruption used to prove lifecycle state publication."""


class FakeProcess:
    """Fake aria2c process with controllable daemon lifecycle."""

    def __init__(
        self,
        *,
        poll_values: list[int | None] | None = None,
        wait_timeouts: int = 0,
        on_wait: Callable[[float | None], None] | None = None,
        wait_errors: list[BaseException] | None = None,
    ) -> None:
        self.poll_values = poll_values or []
        self.wait_timeouts = wait_timeouts
        self.on_wait = on_wait
        self.wait_errors = wait_errors or []
        self.wait_calls = 0
        self.wait_timeout_values: list[float | None] = []
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        if self.poll_values:
            return self.poll_values.pop(0)
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeout_values.append(timeout)
        self.wait_calls += 1
        if self.on_wait is not None:
            self.on_wait(timeout)
        if self.wait_errors:
            raise self.wait_errors.pop(0)
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
        on_get_version: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.timeout = timeout
        self.ready_errors = ready_errors
        self.on_get_version = on_get_version
        self.shutdown_calls = 0

    def get_version(self) -> dict[str, str]:
        if self.on_get_version is not None:
            self.on_get_version()
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
        statuses: list[object],
        *,
        error_code: object | None = None,
        error_message: object | None = None,
        update_error: Exception | None = None,
        on_update: Callable[[], None] | None = None,
        after_update: Callable[[], None] | None = None,
    ) -> None:
        self._statuses: Iterator[object] = iter(statuses)
        self.status = "active"
        self.error_code = error_code
        self.error_message = error_message
        self.update_error = update_error
        self.on_update = on_update
        self.after_update = after_update

    def update(self) -> None:
        if self.on_update is not None:
            self.on_update()
        if self.update_error is not None:
            raise self.update_error
        self.status = next(self._statuses, self.status)
        if self.after_update is not None:
            self.after_update()


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


class Aria2TestSink:
    """Expose only the descriptor-derived aria2 adapter contract."""

    def __init__(
        self,
        target: Path,
        *,
        resume_allowed: bool = True,
        on_current_length: Callable[[], None] | None = None,
    ) -> None:
        self.display_path = target
        self.aria2_directory = "/proc/123/fd/9"
        self.aria2_name = target.name
        self.resume_allowed = resume_allowed
        self.on_current_length = on_current_length

    def current_length(self) -> int:
        if self.on_current_length is not None:
            self.on_current_length()
        return self.display_path.stat().st_size


def make_item(
    tmp_path: Path,
    *,
    resume_allowed: bool = True,
    on_current_length: Callable[[], None] | None = None,
) -> TransportRequest:
    """Return one core-supplied aria2 staging request."""
    target = tmp_path / "models" / ".cdh-staging" / "cdh-model.part"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"complete")
    return TransportRequest(
        url="https://example.test/model.safetensors",
        sink=Aria2TestSink(
            target,
            resume_allowed=resume_allowed,
            on_current_length=on_current_length,
        ),
    )


# Aria2 owns daemon/RPC mechanics and writes only to supplied staging.
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
        cancel_wait=lambda _: False,
        log=logs.append,
    )

    result = downloader.download(item, settings)

    assert isinstance(result, TransportSuccess)
    assert argv_calls == [
        [
            "aria2c",
            "--no-conf=true",
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
                "dir": item.sink.aria2_directory,
                "out": item.sink.aria2_name,
                "split": "8",
                "max-connection-per-server": "4",
                "min-split-size": "2M",
                "continue": "true",
                "auto-file-renaming": "false",
                "allow-overwrite": "true",
            },
        )
    ]
    assert result.length == len(b"complete")
    assert result.namespace == "aria2"
    assert result.http_status is None
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
        cancel_wait=lambda _: False,
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
    api = FakeApi(FakeDownload(["active"], update_error=ConnectionError("RPC failed")))
    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=lambda **_: client,
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="RPC disconnected"), downloader:
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
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    with downloader:
        downloader.download(make_item(tmp_path), make_settings())

    assert client.shutdown_calls == 1
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls == 2


# The adapter uses only documented machine codes for retry policy.
@pytest.mark.parametrize(
    ("code", "expected_type"),
    [
        ("2", TransportRetryable),
        ("6", TransportRetryable),
        ("19", TransportRetryable),
        ("29", TransportRetryable),
        ("3", TransportOrdinaryTerminal),
        ("4", TransportOrdinaryTerminal),
        ("23", TransportOrdinaryTerminal),
        ("24", TransportOrdinaryTerminal),
        ("22", TransportOrdinaryTerminal),
    ],
)
def test_aria2_error_code_maps_to_capability_aware_outcome(
    tmp_path: Path,
    code: str,
    expected_type: type[TransportRetryable] | type[TransportOrdinaryTerminal],
) -> None:
    api = FakeApi(
        FakeDownload(["error"], error_code=code, error_message="diagnostic only")
    )
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    result = downloader.download(make_item(tmp_path), make_settings())

    assert isinstance(result, expected_type)
    assert result.http_status is None
    assert "diagnostic only" not in result.diagnostic.summary


# Human-readable text is not even admitted as machine classification input.
@pytest.mark.parametrize("message", [None, "HTTP 503 retry later", object(), ...])
def test_aria2_error_message_is_never_read_for_classification(
    tmp_path: Path,
    message: object,
) -> None:
    download = FakeDownload(["error"], error_code="22", error_message=message)
    if message is ...:
        del download.error_message
    api = FakeApi(download)
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    result = downloader.download(make_item(tmp_path), make_settings())

    assert isinstance(result, TransportOrdinaryTerminal)
    assert result.http_status is None
    assert result.diagnostic.summary == "aria2 reported an indeterminate HTTP failure"


@pytest.mark.parametrize("code", [None, 2, "", "1", "5", "32", "999", "02"])
def test_aria2_missing_or_unclassified_error_code_fails_closed(
    tmp_path: Path,
    code: object | None,
) -> None:
    api = FakeApi(FakeDownload(["error"], error_code=code))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match=r"malformed|unclassified"):
        downloader.download(make_item(tmp_path), make_settings())


def test_aria2_downloader_reports_removed_status(tmp_path: Path) -> None:
    """Treat aria2 removed status as a failed transfer."""
    api = FakeApi(FakeDownload(["removed"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="removed"):
        downloader.download(make_item(tmp_path), make_settings())


def test_aria2_removed_after_cdh_cancellation_is_cancelled(tmp_path: Path) -> None:
    """A removed status is cancellation only after cdh requested it."""
    downloader: Aria2Downloader
    download = FakeDownload(["removed"])
    api = FakeApi(download)
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    download.on_update = downloader.cancel

    result = downloader.download(make_item(tmp_path), make_settings())

    assert isinstance(result, TransportCancelled)


def test_aria2_cancellation_during_readiness_returns_cancelled(
    tmp_path: Path,
) -> None:
    """Readiness waiting observes the same cooperative cancellation token."""
    entered_readiness = threading.Event()
    release_readiness = threading.Event()

    def block_readiness() -> None:
        entered_readiness.set()
        assert release_readiness.wait(1.0)

    client = FakeClient(
        host="http://localhost",
        port=6811,
        secret="s",
        timeout=5,
        on_get_version=block_readiness,
    )
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **_: client,
        api_factory=lambda _: FakeApi(FakeDownload(["complete"])),
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    results: list[object] = []
    download_worker = threading.Thread(
        target=lambda: results.append(
            downloader.download(make_item(tmp_path), make_settings())
        )
    )
    download_worker.start()
    assert entered_readiness.wait(1.0)
    cancel_worker = threading.Thread(target=downloader.cancel)
    cancel_worker.start()
    release_readiness.set()
    download_worker.join(1.0)
    cancel_worker.join(1.0)

    assert not download_worker.is_alive()
    assert not cancel_worker.is_alive()
    assert len(results) == 1
    assert isinstance(results[0], TransportCancelled)


def test_aria2_downloader_reports_rpc_disconnect(tmp_path: Path) -> None:
    """Treat update failures as RPC disconnects instead of silent success."""
    api = FakeApi(FakeDownload(["active"], update_error=ConnectionError("lost")))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
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
        cancel_wait=lambda _: False,
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
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="exited with code 1"):
        downloader.download(make_item(tmp_path), make_settings())


def test_aria2_downloader_does_not_own_control_file_cleanup(tmp_path: Path) -> None:
    """Leave supplied staging/control lifecycle to the shared transfer core."""
    item = make_item(tmp_path)
    control_file = Path(f"{item.sink.display_path}.aria2")
    control_file.write_text("partial\n", encoding="utf-8")
    api = FakeApi(FakeDownload(["complete"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    downloader.download(item, make_settings(resume_download=False))

    assert control_file.read_text(encoding="utf-8") == "partial\n"
    assert api.calls[0][1]["continue"] == "false"


def test_aria2_prepare_and_download_reuse_one_matching_daemon(tmp_path: Path) -> None:
    """Prepare and multiple items share one exact managed daemon context."""
    process_calls = 0
    process = FakeProcess()
    api = FakeApi(FakeDownload(["complete"]))

    def process_factory(_: list[str]) -> FakeProcess:
        nonlocal process_calls
        process_calls += 1
        return process

    downloader = Aria2Downloader(
        process_factory=process_factory,
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    settings = make_settings()

    with downloader:
        downloader.prepare(settings)
        first = downloader.download(make_item(tmp_path), settings)
        second = downloader.download(make_item(tmp_path), settings)

    assert isinstance(first, TransportSuccess)
    assert isinstance(second, TransportSuccess)
    assert process_calls == 1
    assert len(api.calls) == 2


def test_aria2_never_exposes_api_before_authenticated_readiness(
    tmp_path: Path,
) -> None:
    """A concurrent caller cannot bypass readiness through cached RPC state."""
    entered_readiness = threading.Event()
    release_readiness = threading.Event()
    settings = make_settings()
    process_calls = 0

    def block_readiness() -> None:
        entered_readiness.set()
        assert release_readiness.wait(1.0)

    def process_factory(_: list[str]) -> FakeProcess:
        nonlocal process_calls
        process_calls += 1
        return FakeProcess()

    client = FakeClient(
        host="http://localhost",
        port=6811,
        secret="s",
        timeout=5,
        on_get_version=block_readiness,
    )
    downloader = Aria2Downloader(
        process_factory=process_factory,
        client_factory=lambda **_: client,
        api_factory=lambda _: FakeApi(FakeDownload(["complete"])),
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    errors: list[BaseException] = []

    def prepare() -> None:
        try:
            downloader.prepare(settings)
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=prepare)
    second = threading.Thread(target=prepare)
    first.start()
    assert entered_readiness.wait(1.0)
    second.start()
    release_readiness.set()
    first.join(1.0)
    second.join(1.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert process_calls == 1
    downloader.close()


def test_aria2_rejects_incompatible_settings_for_live_daemon(tmp_path: Path) -> None:
    """A live adapter cannot silently reuse or replace a mismatched daemon."""
    api = FakeApi(FakeDownload(["complete"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    downloader.download(make_item(tmp_path), make_settings())

    with pytest.raises(DownloadFilesError, match="different settings"):
        downloader.download(make_item(tmp_path), make_settings(rpc_port=6812))


def test_aria2_close_is_idempotent_and_escalation_uses_one_total_bound(
    tmp_path: Path,
) -> None:
    """Shutdown escalates once and spends no more than one configured deadline."""
    clock = [100.0]

    def consume_wait(timeout: float | None) -> None:
        clock[0] += timeout or 0.0

    process = FakeProcess(wait_timeouts=2, on_wait=consume_wait)
    api = FakeApi(FakeDownload(["complete"]))
    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        monotonic=lambda: clock[0],
        log=lambda _: None,
    )
    downloader.download(make_item(tmp_path), make_settings())

    downloader.close()
    downloader.close()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 3
    assert sum(timeout or 0.0 for timeout in process.wait_timeout_values) <= 5.0


def test_aria2_cancel_before_complete_observation_prevents_core_placement(
    tmp_path: Path,
) -> None:
    """Cancellation wins the terminal race before a reported complete is admitted."""
    root = tmp_path / "ComfyUI"
    root.mkdir()
    request = FileTransferRequest(
        root=root,
        url="https://example.test/model.safetensors",
        target=root / "models" / "model.safetensors",
        overwrite=True,
        expected_checksum=None,
        staging_disposition=StagingDisposition.CLEAN,
    )
    status_ready = threading.Event()
    release_status = threading.Event()

    def hold_completed_status() -> None:
        status_ready.set()
        assert release_status.wait(1.0)

    api = FakeApi(FakeDownload(["complete"], after_update=hold_completed_status))
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: api,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    errors: list[BaseException] = []

    def transfer() -> None:
        try:
            transfer_file(request, backend=downloader, settings=make_settings())
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=transfer)
    worker.start()
    assert status_ready.wait(1.0)
    cancel_worker = threading.Thread(target=downloader.cancel)
    cancel_worker.start()
    cancel_worker.join(1.0)
    release_status.set()
    worker.join(1.0)

    assert not worker.is_alive()
    assert not cancel_worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], DownloadCancelled)
    assert not request.target.exists()
    assert not transfer_staging_target(request).exists()


def test_aria2_complete_observation_precedes_later_cancel(tmp_path: Path) -> None:
    """A conclusively observed complete remains success if cancellation follows."""
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: FakeApi(FakeDownload(["complete"])),
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    result = downloader.download(
        make_item(tmp_path, on_current_length=downloader.cancel),
        make_settings(),
    )

    assert isinstance(result, TransportSuccess)
    assert result.length == len(b"complete")


def test_aria2_close_waits_for_startup_and_reaps_the_spawned_child(
    tmp_path: Path,
) -> None:
    """A close racing startup cannot return while a late child escapes ownership."""
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    close_waiting = threading.Event()
    process = FakeProcess()

    def process_factory(_: list[str]) -> FakeProcess:
        spawn_entered.set()
        assert release_spawn.wait(1.0)
        return process

    downloader = Aria2Downloader(
        process_factory=process_factory,
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: FakeApi(FakeDownload(["complete"])),
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    original_wait = downloader._wait_for_lifecycle_change

    def observe_wait(deadline: float) -> None:
        close_waiting.set()
        original_wait(deadline)

    downloader._wait_for_lifecycle_change = observe_wait
    startup_errors: list[BaseException] = []
    close_errors: list[BaseException] = []
    startup = threading.Thread(
        target=lambda: _capture_error(
            startup_errors,
            lambda: downloader.prepare(make_settings()),
        )
    )
    startup.start()
    assert spawn_entered.wait(1.0)
    closer = threading.Thread(
        target=lambda: _capture_error(close_errors, downloader.close)
    )
    closer.start()
    assert close_waiting.wait(1.0)
    release_spawn.set()
    startup.join(1.0)
    closer.join(1.0)

    assert not startup.is_alive()
    assert not closer.is_alive()
    assert close_errors == []
    assert len(startup_errors) == 1
    assert isinstance(startup_errors[0], DownloadCancelled)
    assert process.wait_calls == 1


def test_aria2_interrupted_teardown_notifies_waiter_and_later_retries_reap(
    tmp_path: Path,
) -> None:
    """A BaseException publishes one shared failure and retains the exact child."""
    teardown_entered = threading.Event()
    release_teardown = threading.Event()
    waiter_entered = threading.Event()
    first_wait = True
    sentinel = LifecycleSentinel("teardown interrupted")

    def hold_first_wait(_: float | None) -> None:
        nonlocal first_wait
        if first_wait:
            first_wait = False
            teardown_entered.set()
            assert release_teardown.wait(1.0)

    process = FakeProcess(on_wait=hold_first_wait, wait_errors=[sentinel])
    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: FakeApi(FakeDownload(["complete"])),
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    downloader.prepare(make_settings())
    original_wait = downloader._wait_for_lifecycle_change

    def observe_wait(deadline: float) -> None:
        waiter_entered.set()
        original_wait(deadline)

    downloader._wait_for_lifecycle_change = observe_wait
    errors: list[BaseException] = []
    first = threading.Thread(target=lambda: _capture_error(errors, downloader.close))
    second = threading.Thread(target=lambda: _capture_error(errors, downloader.close))
    first.start()
    assert teardown_entered.wait(1.0)
    second.start()
    assert waiter_entered.wait(1.0)
    release_teardown.set()
    first.join(1.0)
    second.join(1.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 2
    assert errors == [sentinel, sentinel]
    assert process.kill_calls == 0

    process.on_wait = None
    downloader.close()
    assert process.wait_calls == 2


@pytest.mark.parametrize("failure_point", ["secret", "process"])
def test_aria2_pre_spawn_base_exception_releases_waiting_starter(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """A pre-spawn interruption restores NEW and lets one waiter make progress."""
    failure_entered = threading.Event()
    release_failure = threading.Event()
    waiter_entered = threading.Event()
    sentinel = LifecycleSentinel(f"{failure_point} interrupted")
    secret_calls = 0
    process_calls = 0
    process = FakeProcess()

    def secret_factory() -> str:
        nonlocal secret_calls
        secret_calls += 1
        if failure_point == "secret" and secret_calls == 1:
            failure_entered.set()
            assert release_failure.wait(1.0)
            raise sentinel
        return "s"

    def process_factory(_: list[str]) -> FakeProcess:
        nonlocal process_calls
        process_calls += 1
        if failure_point == "process" and process_calls == 1:
            failure_entered.set()
            assert release_failure.wait(1.0)
            raise sentinel
        return process

    downloader = Aria2Downloader(
        process_factory=process_factory,
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: FakeApi(FakeDownload(["complete"])),
        secret_factory=secret_factory,
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    original_wait = downloader._wait_for_lifecycle_change

    def observe_wait(deadline: float) -> None:
        waiter_entered.set()
        original_wait(deadline)

    downloader._wait_for_lifecycle_change = observe_wait
    owner_errors: list[BaseException] = []
    waiter_errors: list[BaseException] = []
    owner = threading.Thread(
        target=lambda: _capture_error(
            owner_errors, lambda: downloader.prepare(make_settings())
        )
    )
    waiter = threading.Thread(
        target=lambda: _capture_error(
            waiter_errors, lambda: downloader.prepare(make_settings())
        )
    )
    owner.start()
    assert failure_entered.wait(1.0)
    waiter.start()
    assert waiter_entered.wait(1.0)
    release_failure.set()
    owner.join(1.0)
    waiter.join(1.0)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert owner_errors == [sentinel]
    assert waiter_errors == []
    downloader.close()
    assert process.wait_calls == 1


@pytest.mark.parametrize("failure_point", ["client", "api", "readiness"])
def test_aria2_post_spawn_base_exception_cleans_child_and_notifies_waiter(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """A post-spawn interruption preserves its identity while cleanup completes."""
    failure_entered = threading.Event()
    release_failure = threading.Event()
    waiter_entered = threading.Event()
    sentinel = LifecycleSentinel(f"{failure_point} interrupted")
    process = FakeProcess()

    def interrupt() -> None:
        failure_entered.set()
        assert release_failure.wait(1.0)
        raise sentinel

    client = FakeClient(
        host="http://localhost",
        port=6811,
        secret="s",
        timeout=5,
        on_get_version=interrupt if failure_point == "readiness" else None,
    )

    def client_factory(**_: object) -> FakeClient:
        if failure_point == "client":
            interrupt()
        return client

    def api_factory(_: FakeClient) -> FakeApi:
        if failure_point == "api":
            interrupt()
        return FakeApi(FakeDownload(["complete"]))

    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=client_factory,
        api_factory=api_factory,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    original_wait = downloader._wait_for_lifecycle_change

    def observe_wait(deadline: float) -> None:
        waiter_entered.set()
        original_wait(deadline)

    downloader._wait_for_lifecycle_change = observe_wait
    owner_errors: list[BaseException] = []
    waiter_errors: list[BaseException] = []
    owner = threading.Thread(
        target=lambda: _capture_error(
            owner_errors, lambda: downloader.prepare(make_settings())
        )
    )
    waiter = threading.Thread(
        target=lambda: _capture_error(
            waiter_errors, lambda: downloader.prepare(make_settings())
        )
    )
    owner.start()
    assert failure_entered.wait(1.0)
    waiter.start()
    assert waiter_entered.wait(1.0)
    release_failure.set()
    owner.join(1.0)
    waiter.join(1.0)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert owner_errors == [sentinel]
    assert len(waiter_errors) == 1
    assert isinstance(waiter_errors[0], DownloadFilesError)
    assert process.wait_calls == 1
    assert downloader._process is None


@pytest.mark.parametrize("failure_point", ["client", "api", "readiness"])
def test_aria2_post_spawn_startup_failure_always_reaps_child(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """Every startup failure after spawn closes and reaps the exact child."""
    process = FakeProcess()
    client = FakeClient(host="http://localhost", port=6811, secret="s", timeout=5)
    if failure_point == "readiness":
        client.ready_errors = 1

    def client_factory(**_: object) -> FakeClient:
        if failure_point == "client":
            raise RuntimeError("client factory failed")
        return client

    def api_factory(_: FakeClient) -> FakeApi:
        if failure_point == "api":
            raise RuntimeError("API factory failed")
        return FakeApi(FakeDownload(["complete"]))

    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=client_factory,
        api_factory=api_factory,
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )
    if failure_point == "readiness":
        downloader.startup_timeout_seconds = 0.0

    with pytest.raises(DownloadFilesError):
        downloader.download(make_item(tmp_path), make_settings())

    assert process.wait_calls == 1


@pytest.mark.parametrize("failure_point", ["secret", "process"])
def test_aria2_pre_spawn_startup_failure_never_leaves_lifecycle_starting(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """Factory failures before a child exists leave no hidden startup owner."""
    process_calls = 0

    def secret_factory() -> str:
        if failure_point == "secret":
            raise RuntimeError("secret failed")
        return "s"

    def process_factory(_: list[str]) -> FakeProcess:
        nonlocal process_calls
        process_calls += 1
        raise OSError("spawn failed")

    downloader = Aria2Downloader(
        process_factory=process_factory,
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: FakeApi(FakeDownload(["complete"])),
        secret_factory=secret_factory,
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError):
        downloader.download(make_item(tmp_path), make_settings())
    downloader.close()

    assert process_calls == (0 if failure_point == "secret" else 1)


def test_aria2_rpc_submit_failure_fails_closed_and_context_reaps(
    tmp_path: Path,
) -> None:
    """RPC submission failure is a daemon boundary error, never an item outcome."""
    process = FakeProcess()
    downloader = Aria2Downloader(
        process_factory=lambda _: process,
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: FakeApi(
            FakeDownload(["active"]),
            submit_error=ConnectionError("RPC unavailable"),
        ),
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match="RPC submit"), downloader:
        downloader.download(make_item(tmp_path), make_settings())

    assert process.wait_calls == 1


@pytest.mark.parametrize("status", [None, 7, "", "paused", "Complete"])
def test_aria2_malformed_or_unknown_status_fails_closed(
    tmp_path: Path,
    status: object,
) -> None:
    """Unknown RPC state is never guessed into an ordinary transport outcome."""
    downloader = Aria2Downloader(
        process_factory=lambda _: FakeProcess(),
        client_factory=lambda **kwargs: FakeClient(**kwargs),
        api_factory=lambda _: FakeApi(FakeDownload([status])),
        secret_factory=lambda: "s",
        cancel_wait=lambda _: False,
        log=lambda _: None,
    )

    with pytest.raises(DownloadFilesError, match=r"malformed|unexpected"):
        downloader.download(make_item(tmp_path), make_settings())


def _capture_error(
    errors: list[BaseException],
    operation: Callable[[], object],
) -> None:
    try:
        operation()
    except BaseException as error:
        errors.append(error)
