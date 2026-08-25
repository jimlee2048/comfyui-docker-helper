"""Shared local fixtures for transfer-core owner tests."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from comfyui_docker_helper.container.transfer.core import (
    Aria2DownloadSettings,
    DownloaderSettings,
    FileTransferRequest,
    HttpxDownloadSettings,
    ResumeAuthority,
    StagingDisposition,
    TransportOutcome,
    TransportRequest,
    TransportSuccess,
    transfer_staging_target,
)
from comfyui_docker_helper.container.transfer.events import DownloadEvent


class BytesBackend:
    """Write controlled bytes only to the staging path supplied by the core."""

    def __init__(
        self,
        content: bytes,
        *,
        error: Exception | None = None,
        outcome: TransportOutcome | None = None,
    ) -> None:
        self.content = content
        self.error = error
        self.outcome = outcome
        self.calls: list[TransportRequest] = []

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        del settings
        self.calls.append(request)
        with request.sink.open_for_write() as output:
            output.write(self.content)
        if self.error is not None:
            raise self.error
        if self.outcome is not None:
            return self.outcome
        return TransportSuccess(
            length=len(self.content), namespace="httpx", http_status=200
        )


class RecordingEventSink:
    def __init__(self) -> None:
        self.events: list[DownloadEvent] = []

    def emit(self, event: DownloadEvent, /) -> None:
        self.events.append(event)


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
        httpx=HttpxDownloadSettings(timeout=60),
    )


def _checksum(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _request(
    root: Path,
    *,
    overwrite: bool = False,
    checksum: str | None = None,
    disposition: StagingDisposition = StagingDisposition.CLEAN,
    resume_authority: ResumeAuthority | None = None,
) -> FileTransferRequest:
    return FileTransferRequest(
        root=root,
        url="https://example.test/model.bin",
        target=root / "models" / "model.bin",
        overwrite=overwrite,
        expected_checksum=checksum,
        staging_disposition=disposition,
        resume_authority=resume_authority,
    )


def _preserved_request(
    root: Path,
    *,
    checksum: str | None = None,
) -> FileTransferRequest:
    request = _request(root, overwrite=True, checksum=checksum)
    staging = transfer_staging_target(request)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"prior partial")
    metadata = staging.stat()
    control = Path(f"{staging}.aria2")
    control.write_bytes(b"aria2 control")
    control_metadata = control.stat()
    return replace(
        request,
        staging_disposition=StagingDisposition.PRESERVE,
        resume_authority=ResumeAuthority(
            identity_digest=f"sha256:{staging.name.removeprefix('cdh-').removesuffix('.part')}",
            staging_device=metadata.st_dev,
            staging_inode=metadata.st_ino,
            control_device=control_metadata.st_dev,
            control_inode=control_metadata.st_ino,
        ),
    )
