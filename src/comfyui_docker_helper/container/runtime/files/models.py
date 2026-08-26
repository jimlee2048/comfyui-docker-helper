"""Runtime file plans, state observations, and transfer result models."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from comfyui_docker_helper.config import Diagnostic
from comfyui_docker_helper.config.validation.urls import DownloaderName
from comfyui_docker_helper.container.runtime.state import (
    RuntimeDownloadDigestKey,
    RuntimeDownloadEntry,
    RuntimeState,
)
from comfyui_docker_helper.container.transfer.core import (
    CancellableDownloadBackend,
    DownloadStatus,
    FileTransferOutcome,
    ResumeAuthority,
)

type RuntimeFilePath = tuple[str | int, ...]
type RuntimeDownloadStartupObserver = Callable[[], None]
type RuntimeDownloadCancelRequested = Callable[[], bool]
type RuntimeDownloadCancellationObserver = Callable[[], None]
type RuntimeDownloadBackendObserver = Callable[[CancellableDownloadBackend], None]

type RuntimeDownloadObservedStatus = Literal[
    "failed",
    "exhausted",
    "completed",
]


class RuntimeDownloadStateObserver(Protocol):
    """Optional observer for persisting runtime download state transitions."""

    def __call__(
        self,
        item: RuntimeFilePlanItem,
        status: RuntimeDownloadObservedStatus,
        *,
        error: object | None = None,
        resume_authority: ResumeAuthority | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeFilePlanItem:
    """One normalized runtime file target for download execution."""

    url: str
    directory: str
    filename: str
    relative_target: str
    target: Path
    overwrite: bool
    checksum: str | None
    download_mode: Literal["sync", "async"]
    downloader: DownloaderName | None
    resume_authority: ResumeAuthority | None = None


@dataclass(frozen=True, slots=True)
class RuntimeFilePlan:
    """Ordered normalized runtime file downloads."""

    items: tuple[RuntimeFilePlanItem, ...]


@dataclass(frozen=True, slots=True)
class RuntimeFileDownloadResult:
    """One runtime file backend transfer result."""

    item: RuntimeFilePlanItem
    backend: DownloaderName
    staging_target: Path
    status: DownloadStatus
    outcome: FileTransferOutcome


@dataclass(frozen=True, slots=True)
class RuntimeFileReconciliationItem:
    """One runtime file item reconciled against filesystem and state."""

    item: RuntimeFilePlanItem
    digest: RuntimeDownloadDigestKey
    status: Literal["pending", "completed"]
    scheduled: bool
    staging_target: Path
    previous_entry: RuntimeDownloadEntry | None


@dataclass(frozen=True, slots=True)
class RuntimeFileCleanupPending:
    """One stale entry whose exact cleanup failed during this reconciliation."""

    digest: RuntimeDownloadDigestKey
    reason: str


@dataclass(frozen=True, slots=True)
class RuntimeFileReconciliation:
    """Runtime state and execution plan after bounded indexed reconciliation."""

    state: RuntimeState
    download_plan: RuntimeFilePlan
    items: tuple[RuntimeFileReconciliationItem, ...]
    stale_entry_digests: frozenset[str]
    cleanup_pending: tuple[RuntimeFileCleanupPending, ...]


class RuntimeFilePlanError(ValueError):
    """Runtime file planning failure represented by stable diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("runtime file plan errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("runtime file plan is invalid")


class RuntimeFileDownloadError(ValueError):
    """Runtime file download failure represented by stable diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("runtime file download errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("runtime file download is invalid")


class RuntimeFileDownloadCancelled(Exception):
    """Runtime download work stopped after a cooperative cancellation request."""
