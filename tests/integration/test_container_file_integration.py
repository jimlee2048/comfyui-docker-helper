"""File downloader integration tests."""

from __future__ import annotations

import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.container.download_files import (
    Aria2Downloader,
    FileDownloadItem,
    download_files,
)
from comfyui_docker_helper.rendering import has_valid_context_marker

MINIMAL_CONFIG = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"

[comfyui]
version = "latest"
"""


class RecordingHandler(BaseHTTPRequestHandler):
    """Serve byte routes from the test HTTP server."""

    server: RecordingHttpServer

    def do_GET(self) -> None:
        self.server.requests.append(self.path)
        try:
            body = self.server.routes[self.path]
        except KeyError:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


class RecordingHttpServer(ThreadingHTTPServer):
    """HTTP server carrying route and request state."""

    routes: dict[str, bytes]
    requests: list[str]


class InterruptingManagedBackend:
    """Managed backend that simulates an interrupted aria2 download."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> InterruptingManagedBackend:
        self.events.append("enter")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        self.events.append("exit")

    def download(self, item: FileDownloadItem, settings) -> None:
        del item, settings
        self.events.append("download")
        raise KeyboardInterrupt


class FakeAria2Process:
    """Fake aria2c process for integration-level aria2 tests."""

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


class FakeAria2Client:
    """Fake aria2p client for integration-level aria2 tests."""

    def get_version(self) -> dict[str, str]:
        return {"version": "fake"}

    def shutdown(self) -> str:
        return "OK"


class CompleteAria2Download:
    """Fake aria2p download that completes immediately."""

    status = "active"
    is_removed = False
    error_message = None

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    def update(self) -> None:
        self.status = "complete"


class ControlFileAssertingAria2Api:
    """Fake aria2p API asserting overwrite cleanup before submit."""

    def __init__(self, observed: list[str]) -> None:
        self.observed = observed

    def add_uris(
        self,
        uris: list[str],
        options: dict[str, str] | None = None,
    ) -> CompleteAria2Download:
        del uris
        assert options is not None
        target = Path(options["dir"]) / options["out"]
        control_file = Path(f"{target}.aria2")
        assert not target.exists()
        assert not control_file.exists()
        target.write_bytes(b"aria2-overwrite")
        self.observed.append("submitted-after-cleanup")
        return CompleteAria2Download()


@pytest.fixture()
def local_http_server() -> tuple[str, RecordingHttpServer]:
    """Run a local HTTP server for deterministic downloader integration tests."""
    server = RecordingHttpServer(("127.0.0.1", 0), RecordingHandler)
    server.routes = {}
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_rendered_files_context_downloads_from_local_http(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_http_server: tuple[str, RecordingHttpServer],
) -> None:
    """Render public config, then consume generated files.toml with HTTPX."""
    base_url, server = local_http_server
    server.routes = {
        "/first.bin": b"first",
        "/second.bin": b"second",
    }
    config = tmp_path / "config.toml"
    config.write_text(
        MINIMAL_CONFIG
        + f"""
[cdh]
default_downloader = "httpx"

[[files]]
url = "{base_url}/first.bin"
dir = "models/checkpoints"
filename = "first.bin"

[[files]]
url = "{base_url}/second.bin"
dir = "models/loras"
filename = "second.bin"
""",
        encoding="utf-8",
    )
    output = tmp_path / "context"

    render = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(output)],
    )

    assert render.exit_code == 0
    assert has_valid_context_marker(output)
    files_config = output / "config" / "files.toml"
    assert files_config.is_file()
    dockerfile = (output / "Dockerfile").read_text(encoding="utf-8")
    assert "cdh container download-files" in dockerfile

    comfyui_path = tmp_path / "runtime" / "ComfyUI"
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui_path))

    results = download_files(files_config, log=lambda _: None)

    assert [result.item.filename for result in results] == [
        "first.bin",
        "second.bin",
    ]
    assert (comfyui_path / "models" / "checkpoints" / "first.bin").read_bytes() == (
        b"first"
    )
    assert (comfyui_path / "models" / "loras" / "second.bin").read_bytes() == (
        b"second"
    )
    assert server.requests == ["/first.bin", "/second.bin"]


def test_download_files_overwrites_and_preserves_request_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_http_server: tuple[str, RecordingHttpServer],
) -> None:
    """Overwrite existing targets and request files in config order."""
    base_url, server = local_http_server
    server.routes = {
        "/a.bin": b"new-a",
        "/b.bin": b"new-b",
    }
    comfyui_path = tmp_path / "ComfyUI"
    target = comfyui_path / "models" / "a.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-a")
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui_path))
    config = tmp_path / "files.toml"
    config.write_text(
        f"""
[downloader]
default = "httpx"

[downloader.aria2]
rpc_port = 6811
split = 8
max_connection_per_server = 4
min_split_size = "2M"
resume_download = true

[downloader.httpx]
timeout = 30
retries = 1

[[files]]
url = "{base_url}/a.bin"
dir = "models"
filename = "a.bin"
overwrite = true
downloader = "httpx"

[[files]]
url = "{base_url}/b.bin"
dir = "models"
filename = "b.bin"
overwrite = false
downloader = "httpx"
""",
        encoding="utf-8",
    )

    download_files(config, log=lambda _: None)

    assert target.read_bytes() == b"new-a"
    assert (comfyui_path / "models" / "b.bin").read_bytes() == b"new-b"
    assert server.requests == ["/a.bin", "/b.bin"]


def test_download_files_aria2_overwrite_removes_target_and_control_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify aria2 overwrite cleanup through the download-files entry point."""
    comfyui_path = tmp_path / "ComfyUI"
    target = comfyui_path / "models" / "model.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    control_file = Path(f"{target}.aria2")
    control_file.write_text("partial\n", encoding="utf-8")
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui_path))
    config = tmp_path / "files.toml"
    config.write_text(
        """
[downloader]
default = "aria2"

[downloader.aria2]
rpc_port = 6811
split = 8
max_connection_per_server = 4
min_split_size = "2M"
resume_download = true

[downloader.httpx]
timeout = 30
retries = 1

[[files]]
url = "https://example.test/model.bin"
dir = "models"
filename = "model.bin"
overwrite = true
downloader = "aria2"
""",
        encoding="utf-8",
    )
    observed: list[str] = []

    download_files(
        config,
        aria2_downloader_factory=lambda *, log: Aria2Downloader(
            process_factory=lambda _: FakeAria2Process(),
            client_factory=lambda **_: FakeAria2Client(),
            api_factory=lambda _: ControlFileAssertingAria2Api(observed),
            secret_factory=lambda: "s",
            sleep=lambda _: None,
            log=log,
        ),
        log=lambda _: None,
    )

    assert observed == ["submitted-after-cleanup"]
    assert target.read_bytes() == b"aria2-overwrite"
    assert not control_file.exists()


def test_download_files_rejects_tampered_paths_without_writing_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep generated-helper containment even if files.toml is tampered."""
    comfyui_path = tmp_path / "ComfyUI"
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui_path))
    config = tmp_path / "files.toml"
    config.write_text(
        """
[downloader]
default = "httpx"

[downloader.aria2]
rpc_port = 6811
split = 8
max_connection_per_server = 4
min_split_size = "2M"
resume_download = true

[downloader.httpx]
timeout = 30
retries = 1

[[files]]
url = "https://example.test/escape.bin"
dir = "../escape"
filename = "escape.bin"
overwrite = false
downloader = "httpx"
""",
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="dir must not contain"):
        download_files(config, log=lambda _: None)

    assert not (tmp_path / "escape" / "escape.bin").exists()


def test_download_files_cleans_up_aria2_context_on_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run aria2 cleanup even when processing is interrupted."""
    comfyui_path = tmp_path / "ComfyUI"
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui_path))
    config = tmp_path / "files.toml"
    config.write_text(
        """
[downloader]
default = "aria2"

[downloader.aria2]
rpc_port = 6811
split = 8
max_connection_per_server = 4
min_split_size = "2M"
resume_download = true

[downloader.httpx]
timeout = 30
retries = 1

[[files]]
url = "https://example.test/a.bin"
dir = "models"
filename = "a.bin"
overwrite = false
downloader = "aria2"
""",
        encoding="utf-8",
    )
    events: list[str] = []

    with pytest.raises(KeyboardInterrupt):
        download_files(
            config,
            aria2_downloader_factory=lambda *, log: InterruptingManagedBackend(events),
            log=lambda _: None,
        )

    assert events == ["enter", "download", "exit"]


@pytest.mark.smoke
@pytest.mark.slow
def test_real_aria2_smoke_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_http_server: tuple[str, RecordingHttpServer],
) -> None:
    """Smoke real aria2c when the binary is available on the host."""
    if shutil.which("aria2c") is None:
        pytest.skip("real aria2 smoke skipped because aria2c is not installed")

    base_url, server = local_http_server
    rpc_port = _unused_local_port()
    server.routes = {"/aria2.bin": b"aria2"}
    comfyui_path = tmp_path / "ComfyUI"
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui_path))
    config = tmp_path / "files.toml"
    config.write_text(
        f"""
[downloader]
default = "aria2"

[downloader.aria2]
rpc_port = {rpc_port}
split = 1
max_connection_per_server = 1
min_split_size = "1M"
resume_download = true

[downloader.httpx]
timeout = 30
retries = 1

[[files]]
url = "{base_url}/aria2.bin"
dir = "models"
filename = "aria2.bin"
overwrite = false
downloader = "aria2"
""",
        encoding="utf-8",
    )

    download_files(config, log=lambda _: None)

    assert (comfyui_path / "models" / "aria2.bin").read_bytes() == b"aria2"
    assert server.requests == ["/aria2.bin"]


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
