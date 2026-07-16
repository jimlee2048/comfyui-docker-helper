"""Container-side file download planning and common processing tests."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from comfyui_docker_helper.config.build_plan import (
    Aria2Plan,
    DownloaderPlan,
    FilePlan,
    FilesPhase,
    HttpxPlan,
)
from comfyui_docker_helper.container.download_files import (
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    DownloadStatus,
    FileDownloadItem,
    FileDownloadPlan,
    HttpxDownloadSettings,
    TransferDownloadFilesError,
    file_download_plan,
    process_file_downloads,
)


class RecordingBackend:
    """Fake backend that records orchestration order and retry settings."""

    def __init__(self, *, fail_on: str | None = None, fail_times: int = 0) -> None:
        self.fail_on = fail_on
        self.fail_times = fail_times
        self.calls: list[FileDownloadItem] = []
        self.retries_seen: list[int] = []

    def download(self, item, settings) -> None:
        self.retries_seen.append(settings.httpx.retries)
        self.calls.append(item)
        if item.filename == self.fail_on and self.fail_times:
            self.fail_times -= 1
            item.target.write_bytes(f"partial:{item.filename}".encode())
            raise TransferDownloadFilesError(f"backend failed: {item.filename}")
        item.target.write_bytes(f"downloaded:{item.filename}".encode())


def make_plan(
    comfyui_path: Path,
    *,
    download_max_attempts: int = 3,
    download_failure_policy: Literal["continue", "fail"] = "fail",
    first_directory: str = "models/a",
) -> FileDownloadPlan:
    """Build the typed runtime plan consumed by the downloader orchestrator."""
    return FileDownloadPlan(
        comfyui_root=comfyui_path,
        downloader=DownloaderSettings(
            default="httpx",
            aria2=Aria2DownloadSettings(
                rpc_port=6811,
                split=8,
                max_connection_per_server=4,
                min_split_size="2M",
                resume_download=False,
            ),
            httpx=HttpxDownloadSettings(timeout=90.5, retries=5),
        ),
        items=(
            FileDownloadItem(
                url="https://example.com/first.bin",
                filename="first.bin",
                target=comfyui_path / first_directory / "first.bin",
                overwrite=False,
                downloader="httpx",
            ),
            FileDownloadItem(
                url="https://example.com/second.bin",
                filename="second.bin",
                target=comfyui_path / "models/b/second.bin",
                overwrite=True,
                downloader="aria2",
            ),
        ),
        download_max_attempts=download_max_attempts,
        download_failure_policy=download_failure_policy,
    )


def test_file_download_plan_consumes_typed_phase_and_authoritative_root() -> None:
    """Build downloader inputs from one typed phase and its admitted root."""
    payload = FilesPhase(
        downloader=DownloaderPlan(
            default="httpx",
            aria2=Aria2Plan(
                rpc_port=6800,
                split=16,
                max_connection_per_server=16,
                min_split_size="1M",
                resume_download=True,
            ),
            httpx=HttpxPlan(timeout=42, retries=4),
        ),
        default_download_mode="sync",
        download_max_attempts=3,
        download_failure_policy="fail",
        files=(
            FilePlan(
                url="https://example.com/model.bin",
                target="/workspace/ComfyUI/models/checkpoints/model.bin",
                overwrite=True,
                downloader="httpx",
                download_mode="sync",
                downloader_explicit=True,
                download_mode_explicit=True,
            ),
        ),
    )
    plan = file_download_plan(payload, "/workspace/ComfyUI")

    assert plan.comfyui_root == Path("/workspace/ComfyUI")
    assert plan.downloader.default == "httpx"
    assert plan.downloader.httpx.timeout == 42
    assert plan.downloader.httpx.retries == 4
    assert plan.download_max_attempts == 3
    assert plan.download_failure_policy == "fail"
    assert plan.items[0].filename == "model.bin"
    assert plan.items[0].downloader == "httpx"
    assert plan.items[0].target == Path(
        "/workspace/ComfyUI/models/checkpoints/model.bin"
    )


def test_file_download_plan_rejects_target_outside_root_before_mutation(
    tmp_path: Path,
) -> None:
    """Reject a mismatched admitted root before creating paths or calling a backend."""
    root = tmp_path / "ComfyUI"
    outside_target = tmp_path / "outside" / "model.bin"
    payload = FilesPhase(
        downloader=DownloaderPlan(
            default="httpx",
            aria2=Aria2Plan(
                rpc_port=6800,
                split=16,
                max_connection_per_server=16,
                min_split_size="1M",
                resume_download=True,
            ),
            httpx=HttpxPlan(timeout=42, retries=4),
        ),
        default_download_mode="sync",
        download_max_attempts=3,
        download_failure_policy="fail",
        files=(
            FilePlan(
                url="https://example.com/model.bin",
                target=str(outside_target),
                overwrite=False,
                downloader="httpx",
                download_mode="sync",
                downloader_explicit=True,
                download_mode_explicit=True,
            ),
        ),
    )

    with pytest.raises(DownloadFilesError, match="escapes COMFYUI_PATH"):
        file_download_plan(payload, root)

    assert not root.exists()
    assert not outside_target.parent.exists()


# Backend dispatch preserves declaration order and explicit overwrite semantics.
def test_process_file_downloads_selects_backends_and_preserves_order(
    tmp_path: Path,
) -> None:
    """Create target parents and dispatch to selected backends in file order."""
    plan = make_plan(tmp_path)
    httpx = RecordingBackend()
    aria2 = RecordingBackend()

    results = process_file_downloads(
        plan,
        backends={"httpx": httpx, "aria2": aria2},
        log=lambda _: None,
    )

    assert [result.status for result in results] == [
        DownloadStatus.DOWNLOADED,
        DownloadStatus.DOWNLOADED,
    ]
    assert [item.filename for item in httpx.calls] == ["first.bin"]
    assert [item.filename for item in aria2.calls] == ["second.bin"]
    assert httpx.retries_seen == [0]
    assert aria2.retries_seen == [0]
    assert plan.items[0].target.read_bytes() == b"downloaded:first.bin"
    assert plan.items[1].target.read_bytes() == b"downloaded:second.bin"


def test_process_file_downloads_creates_missing_comfyui_path(
    tmp_path: Path,
) -> None:
    """Allow safe first-use creation of COMFYUI_PATH and target parents."""
    comfyui_path = tmp_path / "runtime" / "ComfyUI"
    plan = make_plan(comfyui_path)

    process_file_downloads(
        plan,
        backends={"httpx": RecordingBackend(), "aria2": RecordingBackend()},
        log=lambda _: None,
    )

    assert (comfyui_path / "models" / "a" / "first.bin").read_bytes() == (
        b"downloaded:first.bin"
    )
    assert (comfyui_path / "models" / "b" / "second.bin").read_bytes() == (
        b"downloaded:second.bin"
    )


def test_process_file_downloads_skips_existing_without_overwrite(
    tmp_path: Path,
) -> None:
    """Existing regular targets with overwrite=false are skipped."""
    plan = make_plan(tmp_path)
    plan.items[0].target.parent.mkdir(parents=True)
    plan.items[0].target.write_text("keep\n", encoding="utf-8")
    httpx = RecordingBackend()
    aria2 = RecordingBackend()
    logs: list[str] = []

    results = process_file_downloads(
        plan,
        backends={"httpx": httpx, "aria2": aria2},
        log=logs.append,
    )

    assert [result.status for result in results] == [
        DownloadStatus.SKIPPED,
        DownloadStatus.DOWNLOADED,
    ]
    assert httpx.calls == []
    assert plan.items[0].target.read_text(encoding="utf-8") == "keep\n"
    assert any("Skipping existing file" in line for line in logs)


def test_process_file_downloads_overwrites_existing_regular_file(
    tmp_path: Path,
) -> None:
    """Existing regular targets with overwrite=true are removed before backend."""
    plan = make_plan(tmp_path)
    target = plan.items[1].target
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    aria2 = RecordingBackend()

    process_file_downloads(
        plan,
        backends={"httpx": RecordingBackend(), "aria2": aria2},
        log=lambda _: None,
    )

    assert [item.filename for item in aria2.calls] == ["second.bin"]
    assert target.read_bytes() == b"downloaded:second.bin"


# Containment checks reject special targets and symlink escapes before mutation.
def test_process_file_downloads_rejects_non_regular_existing_target(
    tmp_path: Path,
) -> None:
    """Do not let directories or special files reach a backend as targets."""
    plan = make_plan(tmp_path)
    plan.items[0].target.mkdir(parents=True)

    with pytest.raises(DownloadFilesError, match="not a regular file"):
        process_file_downloads(
            plan,
            backends={"httpx": RecordingBackend(), "aria2": RecordingBackend()},
            log=lambda _: None,
        )


def test_process_file_downloads_rejects_broken_leaf_symlink(
    tmp_path: Path,
) -> None:
    """Reject leaf symlinks even when Path.exists() would report false."""
    plan = make_plan(tmp_path)
    target = plan.items[0].target
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "outside-missing.bin")

    with pytest.raises(DownloadFilesError, match="not a regular file"):
        process_file_downloads(
            plan,
            backends={"httpx": RecordingBackend(), "aria2": RecordingBackend()},
            log=lambda _: None,
        )


def test_process_file_downloads_rejects_symlink_parent_escape(
    tmp_path: Path,
) -> None:
    """Reject existing parent symlinks that resolve outside COMFYUI_PATH."""
    plan = make_plan(tmp_path / "ComfyUI")
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_parent = plan.items[0].target.parent
    symlink_parent.parent.mkdir(parents=True)
    symlink_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(DownloadFilesError, match="escapes COMFYUI_PATH"):
        process_file_downloads(
            plan,
            backends={"httpx": RecordingBackend(), "aria2": RecordingBackend()},
            log=lambda _: None,
        )

    assert not (outside / plan.items[0].filename).exists()


def test_process_file_downloads_rejects_symlink_escape_before_mutation(
    tmp_path: Path,
) -> None:
    """Reject existing symlink ancestors before creating escaped directories."""
    plan = make_plan(
        tmp_path / "ComfyUI",
        first_directory="models/checkpoints",
    )
    comfyui_path = tmp_path / "ComfyUI"
    outside = tmp_path / "outside"
    outside.mkdir()
    (comfyui_path / "models").parent.mkdir(parents=True)
    (comfyui_path / "models").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DownloadFilesError, match="escapes COMFYUI_PATH"):
        process_file_downloads(
            plan,
            backends={"httpx": RecordingBackend(), "aria2": RecordingBackend()},
            log=lambda _: None,
        )

    assert not (outside / "checkpoints").exists()


def test_process_file_downloads_wraps_parent_creation_failures(
    tmp_path: Path,
) -> None:
    """Report target parent creation failures as helper errors."""
    plan = make_plan(tmp_path)
    blocking_parent = plan.items[0].target.parent
    blocking_parent.parent.mkdir(parents=True)
    blocking_parent.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(DownloadFilesError, match="parent cannot be created"):
        process_file_downloads(
            plan,
            backends={"httpx": RecordingBackend(), "aria2": RecordingBackend()},
            log=lambda _: None,
        )


def test_process_file_downloads_wraps_overwrite_removal_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report overwrite cleanup failures as helper errors."""
    plan = make_plan(tmp_path)
    target = plan.items[1].target
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_unlink(self: Path) -> None:
        if self == target:
            raise PermissionError("blocked")
        original_unlink(self)

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(DownloadFilesError, match="cannot be removed"):
        process_file_downloads(
            plan,
            backends={"httpx": RecordingBackend(), "aria2": RecordingBackend()},
            log=lambda _: None,
        )


# Retry and failure policy applies consistently without losing later-file ordering.
def test_process_file_downloads_stops_on_backend_failure(tmp_path: Path) -> None:
    """Fail policy exhausts attempts, removes partials, and stops later files."""
    plan = make_plan(tmp_path)
    httpx = RecordingBackend(fail_on="first.bin", fail_times=3)
    aria2 = RecordingBackend()

    with pytest.raises(DownloadFilesError, match="backend failed"):
        process_file_downloads(
            plan,
            backends={"httpx": httpx, "aria2": aria2},
            log=lambda _: None,
        )

    assert [item.filename for item in httpx.calls] == [
        "first.bin",
        "first.bin",
        "first.bin",
    ]
    assert aria2.calls == []
    assert not plan.items[0].target.exists()


def test_process_file_downloads_retries_until_success(tmp_path: Path) -> None:
    """Retry a failed item up to the configured total attempt count."""
    plan = make_plan(tmp_path, download_max_attempts=3)
    httpx = RecordingBackend(fail_on="first.bin", fail_times=2)

    results = process_file_downloads(
        plan,
        backends={"httpx": httpx, "aria2": RecordingBackend()},
        log=lambda _: None,
    )

    assert [result.status for result in results] == [
        DownloadStatus.DOWNLOADED,
        DownloadStatus.DOWNLOADED,
    ]
    assert [item.filename for item in httpx.calls] == [
        "first.bin",
        "first.bin",
        "first.bin",
    ]
    assert plan.items[0].target.read_bytes() == b"downloaded:first.bin"


def test_process_file_downloads_continue_policy_logs_and_processes_later_files(
    tmp_path: Path,
) -> None:
    """Continue policy drops failed results but preserves later file processing."""
    plan = make_plan(
        tmp_path,
        download_max_attempts=2,
        download_failure_policy="continue",
    )
    httpx = RecordingBackend(fail_on="first.bin", fail_times=2)
    aria2 = RecordingBackend()
    logs: list[str] = []

    results = process_file_downloads(
        plan,
        backends={"httpx": httpx, "aria2": aria2},
        log=logs.append,
    )

    assert [result.item.filename for result in results] == ["second.bin"]
    assert [result.status for result in results] == [DownloadStatus.DOWNLOADED]
    assert [item.filename for item in httpx.calls] == ["first.bin", "first.bin"]
    assert [item.filename for item in aria2.calls] == ["second.bin"]
    assert not plan.items[0].target.exists()
    assert any("WARNING: download failed after 2 attempt(s)" in line for line in logs)


def test_process_file_downloads_requires_configured_backend(tmp_path: Path) -> None:
    """Report missing backend implementations as helper errors."""
    plan = make_plan(tmp_path)

    with pytest.raises(DownloadFilesError, match="backend is not configured"):
        process_file_downloads(
            plan,
            backends={"httpx": RecordingBackend()},
            log=lambda _: None,
        )
