"""Tests for container-side root file download processing."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfyui_docker_helper.container.download_files import (
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    FileDownloadItem,
    FileDownloadPlan,
    HttpxDownloadSettings,
    TransferDownloadFilesError,
    process_file_downloads,
)


class FakeDownloadBackend:
    def __init__(
        self,
        *,
        error: DownloadFilesError,
        create_aria2_control: bool = False,
    ) -> None:
        self.error = error
        self.create_aria2_control = create_aria2_control
        self.calls: list[FileDownloadItem] = []

    def download(
        self,
        item: FileDownloadItem,
        settings: DownloaderSettings,
    ) -> None:
        del settings
        self.calls.append(item)
        item.target.write_bytes(b"partial")
        if self.create_aria2_control and item.downloader == "aria2":
            Path(f"{item.target}.aria2").write_bytes(b"aria2-control")
        raise self.error


def _settings() -> DownloaderSettings:
    return DownloaderSettings(
        default="httpx",
        aria2=Aria2DownloadSettings(
            rpc_port=6800,
            split=16,
            max_connection_per_server=16,
            min_split_size="1M",
            resume_download=True,
        ),
        httpx=HttpxDownloadSettings(timeout=30, retries=0),
    )


def _item(comfyui: Path, filename: str) -> FileDownloadItem:
    return FileDownloadItem(
        url=f"https://example.com/{filename}",
        directory="models",
        filename=filename,
        target=comfyui / "models" / filename,
        overwrite=False,
        downloader="httpx",
    )


def _aria2_item(comfyui: Path, filename: str) -> FileDownloadItem:
    return FileDownloadItem(
        url=f"https://example.com/{filename}",
        directory="models",
        filename=filename,
        target=comfyui / "models" / filename,
        overwrite=False,
        downloader="aria2",
    )


def test_file_download_continue_policy_keeps_plain_download_error_fatal(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    first = _item(comfyui, "a.bin")
    second = _item(comfyui, "b.bin")
    backend = FakeDownloadBackend(error=DownloadFilesError("local setup failed"))

    with pytest.raises(DownloadFilesError, match="local setup failed"):
        process_file_downloads(
            FileDownloadPlan(
                downloader=_settings(),
                items=(first, second),
                download_max_attempts=2,
                download_failure_policy="continue",
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert [item.filename for item in backend.calls] == ["a.bin"]
    assert not first.target.exists()
    assert not second.target.exists()


def test_file_download_continue_policy_handles_transfer_error(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    first = _item(comfyui, "a.bin")
    second = _item(comfyui, "b.bin")
    backend = FakeDownloadBackend(error=TransferDownloadFilesError("network failed"))

    results = process_file_downloads(
        FileDownloadPlan(
            downloader=_settings(),
            items=(first, second),
            download_max_attempts=2,
            download_failure_policy="continue",
        ),
        backends={"httpx": backend},
        log=lambda message: None,
    )

    assert results == ()
    assert [item.filename for item in backend.calls] == [
        "a.bin",
        "a.bin",
        "b.bin",
        "b.bin",
    ]
    assert not first.target.exists()
    assert not second.target.exists()


def test_file_download_continue_policy_cleans_failed_aria2_sidecar(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    first = _aria2_item(comfyui, "a.bin")
    second = _item(comfyui, "b.bin")
    backend = FakeDownloadBackend(
        error=TransferDownloadFilesError("network failed"),
        create_aria2_control=True,
    )

    results = process_file_downloads(
        FileDownloadPlan(
            downloader=_settings(),
            items=(first, second),
            download_max_attempts=1,
            download_failure_policy="continue",
        ),
        backends={"aria2": backend, "httpx": backend},
        log=lambda message: None,
    )

    assert results == ()
    assert [item.filename for item in backend.calls] == ["a.bin", "b.bin"]
    assert not first.target.exists()
    assert not Path(f"{first.target}.aria2").exists()
    assert not second.target.exists()
