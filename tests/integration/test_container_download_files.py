"""Container-side file download planning and common processing tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.artifact_helpers import write_root_artifacts

from comfyui_docker_helper.container.download_files import (
    DownloadFilesConfigError,
    DownloadFilesError,
    DownloadStatus,
    FileDownloadItem,
    TransferDownloadFilesError,
    build_file_download_plan,
    load_file_download_plan,
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


def make_document() -> dict[str, object]:
    """Return a normalized extracted file-download view."""
    return {
        "cdh": {
            "download_max_attempts": 3,
            "download_failure_policy": "fail",
        },
        "downloader": {
            "default": "httpx",
            "aria2": {
                "rpc_port": 6811,
                "split": 8,
                "max_connection_per_server": 4,
                "min_split_size": "2M",
                "resume_download": False,
            },
            "httpx": {
                "timeout": 90.5,
                "retries": 5,
            },
        },
        "files": [
            {
                "url": "https://example.com/first.bin",
                "dir": "models/a",
                "filename": "first.bin",
                "overwrite": False,
                "downloader": "httpx",
            },
            {
                "url": "https://example.com/second.bin",
                "dir": "models/b",
                "filename": "second.bin",
                "overwrite": True,
                "downloader": "aria2",
            },
        ],
    }


def test_build_file_download_plan_preserves_order_and_derives_targets(
    tmp_path: Path,
) -> None:
    """Load normalized helper data and derive container target paths."""
    document = make_document()

    plan = build_file_download_plan(
        document,
        comfyui_path=tmp_path / "ComfyUI",
    )

    assert plan.downloader.default == "httpx"
    assert plan.downloader.aria2.rpc_port == 6811
    assert plan.downloader.aria2.resume_download is False
    assert plan.downloader.httpx.timeout == 90.5
    assert plan.downloader.httpx.retries == 5
    assert plan.download_max_attempts == 3
    assert plan.download_failure_policy == "fail"
    assert [item.downloader for item in plan.items] == ["httpx", "aria2"]
    assert [item.target for item in plan.items] == [
        tmp_path / "ComfyUI" / "models" / "a" / "first.bin",
        tmp_path / "ComfyUI" / "models" / "b" / "second.bin",
    ]


def test_build_file_download_plan_defaults_to_runtime_comfyui_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use COMFYUI_PATH when the caller does not pass an explicit runtime path."""
    runtime_comfyui = tmp_path / "runtime" / "ComfyUI"
    monkeypatch.setenv("COMFYUI_PATH", str(runtime_comfyui))

    plan = build_file_download_plan(make_document())

    assert plan.items[0].target == runtime_comfyui / "models" / "a" / "first.bin"


def test_load_file_download_plan_reports_missing_and_invalid_toml(
    tmp_path: Path,
) -> None:
    """Translate file and TOML errors into user-facing helper errors."""
    with pytest.raises(DownloadFilesConfigError, match="does not exist"):
        load_file_download_plan(tmp_path / "missing.toml", tmp_path / "missing.lock")

    config = tmp_path / "bad.toml"
    config.write_text("[downloader\n", encoding="utf-8")
    lock = tmp_path / "config.lock.toml"
    lock.write_text("", encoding="utf-8")

    with pytest.raises(DownloadFilesConfigError, match="not valid TOML"):
        load_file_download_plan(config, lock)


def test_load_file_download_plan_extracts_files_from_root_artifacts(
    tmp_path: Path,
) -> None:
    """Build a file download plan from root config.toml and config.lock.toml."""
    config, lock = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "latest"

[cdh]
default_downloader = "httpx"

[cdh.downloader.httpx]
timeout = 42
retries = 4

[[files]]
url = "https://example.com/model.bin"
dir = "models/checkpoints"
filename = "model.bin"
overwrite = true
""",
    )

    plan = load_file_download_plan(config, lock, comfyui_path=tmp_path / "ComfyUI")

    assert plan.downloader.default == "httpx"
    assert plan.downloader.httpx.timeout == 42
    assert plan.downloader.httpx.retries == 4
    assert plan.download_max_attempts == 3
    assert plan.download_failure_policy == "fail"
    assert plan.items[0].filename == "model.bin"
    assert plan.items[0].downloader == "httpx"
    assert plan.items[0].target == tmp_path / "ComfyUI/models/checkpoints/model.bin"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda doc: doc.pop("downloader"), "validation failed"),
        (
            lambda doc: doc["files"][0].__setitem__("downloader", "bad"),
            "validation failed",
        ),
        (lambda doc: doc["files"][0].__setitem__("dir", "../escape"), "dir must not"),
        (
            lambda doc: doc["files"][0].__setitem__("dir", "/absolute"),
            "dir must be relative",
        ),
        (
            lambda doc: doc["files"][0].__setitem__("dir", "models/"),
            "dir must not end with a slash",
        ),
        (
            lambda doc: doc["files"][0].__setitem__("dir", "models/."),
            "dir must not contain '.'",
        ),
        (
            lambda doc: doc["files"][0].__setitem__("dir", "models//a"),
            "dir must not contain empty path segments",
        ),
        (lambda doc: doc["files"][0].__setitem__("filename", "a/b"), "filename must"),
        (
            lambda doc: doc["files"][0].__setitem__("filename", "a\\b"),
            "filename must not contain",
        ),
        (lambda doc: doc["files"][0].__setitem__("url", "ftp://example.com/a"), "HTTP"),
        (
            lambda doc: doc["files"][0].__setitem__(
                "url",
                "https://example.com:bad/a",
            ),
            "HTTP",
        ),
        (lambda doc: doc["files"][0].__setitem__("extra", "bad"), "validation failed"),
        (lambda doc: doc.__setitem__("target", "/malicious"), "validation failed"),
        (
            lambda doc: doc["downloader"]["httpx"].__setitem__("timeout", 0),
            "validation failed",
        ),
        (
            lambda doc: doc["downloader"]["httpx"].__setitem__("retries", -1),
            "validation failed",
        ),
        (
            lambda doc: doc["downloader"]["aria2"].__setitem__("rpc_port", 0),
            "validation failed",
        ),
        (
            lambda doc: doc["downloader"]["aria2"].__setitem__("rpc_port", 65536),
            "validation failed",
        ),
        (
            lambda doc: doc["downloader"]["aria2"].__setitem__("split", 0),
            "validation failed",
        ),
        (
            lambda doc: doc["downloader"]["aria2"].__setitem__(
                "max_connection_per_server",
                0,
            ),
            "validation failed",
        ),
    ],
)
def test_build_file_download_plan_defensively_rejects_invalid_config(
    mutation,
    message: str,
    tmp_path: Path,
) -> None:
    """Reject malformed paths and downloader settings before filesystem writes."""
    document = make_document()
    mutation(document)

    with pytest.raises(DownloadFilesConfigError, match=message):
        build_file_download_plan(document, comfyui_path=tmp_path)


def test_process_file_downloads_selects_backends_and_preserves_order(
    tmp_path: Path,
) -> None:
    """Create target parents and dispatch to selected backends in file order."""
    plan = build_file_download_plan(make_document(), comfyui_path=tmp_path)
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
    plan = build_file_download_plan(make_document(), comfyui_path=comfyui_path)

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
    plan = build_file_download_plan(make_document(), comfyui_path=tmp_path)
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
    document = make_document()
    plan = build_file_download_plan(document, comfyui_path=tmp_path)
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


def test_process_file_downloads_rejects_non_regular_existing_target(
    tmp_path: Path,
) -> None:
    """Do not let directories or special files reach a backend as targets."""
    plan = build_file_download_plan(make_document(), comfyui_path=tmp_path)
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
    plan = build_file_download_plan(make_document(), comfyui_path=tmp_path)
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
    plan = build_file_download_plan(make_document(), comfyui_path=tmp_path / "ComfyUI")
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
    document = make_document()
    document["files"][0]["dir"] = "models/checkpoints"
    plan = build_file_download_plan(document, comfyui_path=tmp_path / "ComfyUI")
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
    plan = build_file_download_plan(make_document(), comfyui_path=tmp_path)
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
    plan = build_file_download_plan(make_document(), comfyui_path=tmp_path)
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


def test_process_file_downloads_stops_on_backend_failure(tmp_path: Path) -> None:
    """Fail policy exhausts attempts, removes partials, and stops later files."""
    plan = build_file_download_plan(make_document(), comfyui_path=tmp_path)
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
    document = make_document()
    document["cdh"]["download_max_attempts"] = 3
    plan = build_file_download_plan(document, comfyui_path=tmp_path)
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
    document = make_document()
    document["cdh"]["download_max_attempts"] = 2
    document["cdh"]["download_failure_policy"] = "continue"
    plan = build_file_download_plan(document, comfyui_path=tmp_path)
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
    plan = build_file_download_plan(make_document(), comfyui_path=tmp_path)

    with pytest.raises(DownloadFilesError, match="backend is not configured"):
        process_file_downloads(
            plan,
            backends={"httpx": RecordingBackend()},
            log=lambda _: None,
        )
