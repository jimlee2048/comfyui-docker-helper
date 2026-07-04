"""Container download-files orchestration and CLI tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.artifact_helpers import write_root_artifacts
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.container.download_files import (
    DownloadFilesError,
    DownloadStatus,
    FileDownloadItem,
    download_files,
)


class RecordingBackend:
    """Fake download backend recording serial calls."""

    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail_on: str | None = None,
    ) -> None:
        self.name = name
        self.events = events
        self.fail_on = fail_on

    def download(self, item: FileDownloadItem, settings) -> None:
        del settings
        self.events.append(f"{self.name}:{item.filename}")
        if item.filename == self.fail_on:
            raise DownloadFilesError(f"{self.name} failed: {item.filename}")
        item.target.write_bytes(f"{self.name}:{item.filename}".encode())


class ManagedRecordingBackend(RecordingBackend):
    """Fake managed backend recording context cleanup."""

    def __enter__(self) -> ManagedRecordingBackend:
        self.events.append(f"{self.name}:enter")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        self.events.append(f"{self.name}:exit")


def write_files_config(
    tmp_path: Path,
    *,
    files: list[dict[str, object]],
    default: str = "httpx",
    download_max_attempts: int = 3,
    download_failure_policy: str = "fail",
) -> Path:
    """Write root config and lock artifacts for download tests."""
    lines = [
        "[comfyui]",
        'version = "latest"',
        "",
        "[cdh]",
        f'default_downloader = "{default}"',
        f"download_max_attempts = {download_max_attempts}",
        f'download_failure_policy = "{download_failure_policy}"',
        "",
        "[cdh.downloader.aria2]",
        "rpc_port = 6811",
        "split = 8",
        "max_connection_per_server = 4",
        'min_split_size = "2M"',
        "resume_download = true",
        "",
        "[cdh.downloader.httpx]",
        "timeout = 30",
        "retries = 2",
        "",
    ]
    for file in files:
        lines.extend(
            [
                "[[files]]",
                f'url = "{file["url"]}"',
                f'dir = "{file["dir"]}"',
                f'filename = "{file["filename"]}"',
                f"overwrite = {str(file['overwrite']).lower()}",
                f'downloader = "{file["downloader"]}"',
                "",
            ]
        )

    config_path, _ = write_root_artifacts(tmp_path, "\n".join(lines))
    return config_path


def file_entry(
    filename: str,
    *,
    downloader: str = "httpx",
    overwrite: bool = False,
) -> dict[str, object]:
    """Return one generated file entry."""
    return {
        "url": f"https://example.test/{filename}",
        "dir": "models",
        "filename": filename,
        "overwrite": overwrite,
        "downloader": downloader,
    }


def test_download_files_uses_httpx_without_starting_aria2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not construct aria2 when all items use HTTPX."""
    comfyui_path = tmp_path / "ComfyUI"
    comfyui_path.mkdir()
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui_path))
    config = write_files_config(tmp_path, files=[file_entry("a.bin")])
    events: list[str] = []

    def unexpected_aria2_factory(*, log):
        del log
        raise AssertionError("aria2 should not start")

    results = download_files(
        config,
        config.with_name("config.lock.toml"),
        httpx_downloader=RecordingBackend("httpx", events),
        aria2_downloader_factory=unexpected_aria2_factory,
        log=lambda _: None,
    )

    assert [result.status for result in results] == [DownloadStatus.DOWNLOADED]
    assert events == ["httpx:a.bin"]
    assert (comfyui_path / "models" / "a.bin").read_bytes() == b"httpx:a.bin"


def test_download_files_processes_mixed_backends_serially(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process mixed backend items one at a time in config order."""
    comfyui_path = tmp_path / "ComfyUI"
    comfyui_path.mkdir()
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui_path))
    config = write_files_config(
        tmp_path,
        files=[
            file_entry("a.bin", downloader="httpx"),
            file_entry("b.bin", downloader="aria2"),
            file_entry("c.bin", downloader="httpx"),
        ],
    )
    events: list[str] = []
    logs: list[str] = []

    results = download_files(
        config,
        config.with_name("config.lock.toml"),
        httpx_downloader=RecordingBackend("httpx", events),
        aria2_downloader_factory=lambda *, log: ManagedRecordingBackend(
            "aria2",
            events,
        ),
        log=logs.append,
    )

    assert [result.status for result in results] == [
        DownloadStatus.DOWNLOADED,
        DownloadStatus.DOWNLOADED,
        DownloadStatus.DOWNLOADED,
    ]
    assert events == [
        "aria2:enter",
        "httpx:a.bin",
        "aria2:b.bin",
        "httpx:c.bin",
        "aria2:exit",
    ]
    assert [line for line in logs if line.startswith("Downloaded file:")] == [
        f"Downloaded file: {comfyui_path / 'models' / 'a.bin'}",
        f"Downloaded file: {comfyui_path / 'models' / 'b.bin'}",
        f"Downloaded file: {comfyui_path / 'models' / 'c.bin'}",
    ]


def test_download_files_stops_on_failure_and_cleans_up_aria2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop on first backend failure and still exit the aria2 context."""
    comfyui_path = tmp_path / "ComfyUI"
    comfyui_path.mkdir()
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui_path))
    config = write_files_config(
        tmp_path,
        files=[
            file_entry("a.bin", downloader="aria2"),
            file_entry("b.bin", downloader="httpx"),
        ],
    )
    events: list[str] = []

    with pytest.raises(DownloadFilesError, match="aria2 failed"):
        download_files(
            config,
            config.with_name("config.lock.toml"),
            httpx_downloader=RecordingBackend("httpx", events),
            aria2_downloader_factory=lambda *, log: ManagedRecordingBackend(
                "aria2",
                events,
                fail_on="a.bin",
            ),
            log=lambda _: None,
        )

    assert events == [
        "aria2:enter",
        "aria2:a.bin",
        "aria2:a.bin",
        "aria2:a.bin",
        "aria2:exit",
    ]
    assert not (comfyui_path / "models" / "b.bin").exists()


def test_cli_exposes_download_files_and_passes_config(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invoke the supported container helper and pass --config through."""
    calls: list[tuple[Path, Path, Path | None]] = []

    def fake_download_files(
        config: Path,
        lock: Path,
        *,
        comfyui_path: Path | None = None,
    ) -> None:
        calls.append((config, lock, comfyui_path))

    monkeypatch.setattr(
        "comfyui_docker_helper.container.cli.download_files",
        fake_download_files,
    )
    config = tmp_path / "files.toml"
    lock = tmp_path / "config.lock.toml"
    config.write_text("", encoding="utf-8")
    lock.write_text("", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE", "/srv/work")
    monkeypatch.setenv("COMFYUI_PATH", "/opt/comfy")

    result = cli_runner.invoke(
        app,
        ["container", "download-files", "--config", str(config), "--lock", str(lock)],
    )

    assert result.exit_code == 0
    assert calls == [(config, lock, Path("/opt/comfy"))]


def test_cli_download_files_fails_without_container_paths(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require Docker-managed path environment before helper execution."""
    config = tmp_path / "files.toml"
    lock = tmp_path / "config.lock.toml"
    config.write_text("", encoding="utf-8")
    lock.write_text("", encoding="utf-8")
    monkeypatch.delenv("WORKSPACE", raising=False)
    monkeypatch.delenv("COMFYUI_PATH", raising=False)

    result = cli_runner.invoke(
        app,
        ["container", "download-files", "--config", str(config), "--lock", str(lock)],
    )

    assert result.exit_code == 1
    assert "missing required container environment" in result.output
