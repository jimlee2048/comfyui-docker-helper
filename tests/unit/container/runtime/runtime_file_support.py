"""Shared runtime-file test data builders."""

from __future__ import annotations

import hashlib
from pathlib import Path

from comfyui_docker_helper.config.runtime.models import RuntimeConfig
from comfyui_docker_helper.container.runtime.files.models import (
    RuntimeFilePlan,
    RuntimeFilePlanItem,
)
from comfyui_docker_helper.container.runtime.files.planning import (
    build_runtime_file_plan,
    runtime_file_state_identity_digest,
)
from comfyui_docker_helper.container.runtime.state import (
    RuntimeDownloadEntry,
    RuntimeState,
)


def checksum(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def runtime_config(
    *,
    policy: str = "fail",
    attempts: int = 2,
    default: str = "httpx",
    resume: bool = False,
) -> RuntimeConfig:
    document = RuntimeConfig().model_dump(mode="python")
    document["cdh"]["download_failure_policy"] = policy
    document["cdh"]["download_max_attempts"] = attempts
    document["cdh"]["default_downloader"] = default
    document["cdh"]["downloader"]["aria2"]["resume_download"] = resume
    return RuntimeConfig.model_validate(document)


def runtime_file_plan(root: Path, *files: dict) -> RuntimeFilePlan:
    root.mkdir(parents=True, exist_ok=True)
    return build_runtime_file_plan(files, comfyui_path=root)


def runtime_file(
    name: str,
    *,
    downloader: str | None = None,
    overwrite: bool = False,
    checksum: str | None = None,
    mode: str | None = None,
) -> dict:
    item = {
        "type": "http",
        "url": f"https://example.test/{name}",
        "target": f"models/{name}",
        "overwrite": overwrite,
    }
    if downloader is not None:
        item["downloader"] = downloader
    if checksum is not None:
        item["checksum"] = checksum
    if mode is not None:
        item["download_mode"] = mode
    return item


def runtime_state(
    entries: dict[str, RuntimeDownloadEntry] | None = None,
    *,
    run_id: str = "run-1",
) -> RuntimeState:
    return RuntimeState(
        schema_version=1,
        run_id=run_id,
        downloads=entries or {},
    )


def runtime_entry(
    *,
    target: str = "models/a.bin",
    status: str = "completed",
    url: str = "https://example.test/a.bin",
    checksum: str | None = None,
    overwrite: bool = False,
    downloader: str = "httpx",
) -> RuntimeDownloadEntry:
    return RuntimeDownloadEntry(
        url=url,
        target=target,
        checksum=checksum,
        overwrite=overwrite,
        downloader=downloader,
        download_mode="sync",
        status=status,
    )


def runtime_entry_for_item(
    item: RuntimeFilePlanItem,
    *,
    status: str = "completed",
    downloader: str = "httpx",
) -> RuntimeDownloadEntry:
    return runtime_entry(
        url=item.url,
        target=item.relative_target,
        checksum=item.checksum,
        overwrite=item.overwrite,
        downloader=downloader,
        status=status,
    )


def runtime_state_digest(
    item: RuntimeFilePlanItem,
    *,
    default_downloader: str = "httpx",
) -> str:
    return runtime_file_state_identity_digest(
        item,
        default_downloader=default_downloader,
    )
