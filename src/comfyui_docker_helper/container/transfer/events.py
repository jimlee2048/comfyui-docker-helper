"""Immutable, presentation-neutral events for serial file downloads."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class DownloadBackendName(StrEnum):
    """Admitted transfer mechanisms safe to identify at debug detail."""

    HTTPX = "httpx"
    ARIA2 = "aria2"


class DownloadRetryReason(StrEnum):
    """Controlled retry categories safe for human-facing detail."""

    TIMEOUT = "timeout"
    NETWORK = "network"
    TEMPORARY_SERVER = "temporary-server"
    RATE_LIMITED = "rate-limited"
    RESUME_REJECTED = "resume-rejected"
    CHECKSUM_MISMATCH = "checksum-mismatch"
    UNKNOWN = "unknown"


class DownloadItemStatus(StrEnum):
    """Successful terminal result for one required file."""

    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class DownloadItemStarted:
    """Begin one item in an ordered, serial download batch."""

    index: int
    total: int
    target: str
    backend: DownloadBackendName
    max_attempts: int
    checksum_expected: bool

    def __post_init__(self) -> None:
        _require_positive_integer(self.index, "download item index")
        _require_positive_integer(self.total, "download item total")
        if self.index > self.total:
            raise ValueError("download item index must not exceed its total")
        _require_canonical_target(self.target)
        if not isinstance(self.backend, DownloadBackendName):
            raise ValueError("download backend must be an admitted backend")
        _require_positive_integer(self.max_attempts, "download attempt total")
        if not isinstance(self.checksum_expected, bool):
            raise ValueError("checksum expectation must be boolean")


@dataclass(frozen=True, slots=True)
class DownloadAttemptStarted:
    """Begin one counted transport attempt for the current item."""

    attempt: int

    def __post_init__(self) -> None:
        _require_positive_integer(self.attempt, "download attempt")


@dataclass(frozen=True, slots=True)
class DownloadTransferProgress:
    """Report same-domain transfer bytes and optional separately stored bytes."""

    transferred_bytes: int
    total_bytes: int | None
    stored_bytes: int | None
    reported_rate: int | float | None = None

    def __post_init__(self) -> None:
        _require_non_negative_integer(self.transferred_bytes, "transferred bytes")
        if self.total_bytes is not None:
            _require_non_negative_integer(self.total_bytes, "total transfer bytes")
            if self.transferred_bytes > self.total_bytes:
                raise ValueError(
                    "transferred bytes must not exceed total transfer bytes"
                )
        if self.stored_bytes is not None:
            _require_non_negative_integer(self.stored_bytes, "stored bytes")
        if self.reported_rate is not None:
            _require_non_negative_number(self.reported_rate, "reported transfer rate")


@dataclass(frozen=True, slots=True)
class DownloadRetryScheduled:
    """Schedule the next counted attempt after one controlled failure."""

    failed_attempt: int
    next_attempt: int
    delay_seconds: int | float
    reason: DownloadRetryReason
    http_status: int | None = None

    def __post_init__(self) -> None:
        _require_positive_integer(self.failed_attempt, "failed download attempt")
        _require_positive_integer(self.next_attempt, "next download attempt")
        if self.next_attempt != self.failed_attempt + 1:
            raise ValueError("next download attempt must follow the failed attempt")
        _require_non_negative_number(self.delay_seconds, "download retry delay")
        if not isinstance(self.reason, DownloadRetryReason):
            raise ValueError("download retry reason must be an admitted category")
        if self.http_status is not None:
            _require_integer(self.http_status, "download HTTP status")
            if not 100 <= self.http_status <= 599:
                raise ValueError("download HTTP status is outside the valid range")


@dataclass(frozen=True, slots=True)
class DownloadVerificationStarted:
    """Begin verification of the current admitted download bytes."""


@dataclass(frozen=True, slots=True)
class DownloadVerificationCompleted:
    """Complete verification of the current admitted download bytes."""


@dataclass(frozen=True, slots=True)
class DownloadPlacementStarted:
    """Begin atomic placement of the current admitted file."""


@dataclass(frozen=True, slots=True)
class DownloadPlacementCompleted:
    """Complete atomic placement of the current admitted file."""


@dataclass(frozen=True, slots=True)
class DownloadFinalVerificationStarted:
    """Begin the fresh batch-level recheck after every item completes."""

    item_count: int
    checksum_count: int

    def __post_init__(self) -> None:
        _require_non_negative_integer(self.item_count, "download item count")
        _require_non_negative_integer(self.checksum_count, "download checksum count")
        if self.checksum_count > self.item_count:
            raise ValueError("download checksum count must not exceed the item count")


@dataclass(frozen=True, slots=True)
class DownloadFinalVerificationCompleted:
    """Complete the fresh batch-level recheck."""


@dataclass(frozen=True, slots=True)
class DownloadItemCompleted:
    """Complete the current required file after its final postcondition."""

    status: DownloadItemStatus
    observed_bytes: int
    checksum_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.status, DownloadItemStatus):
            raise ValueError("download item status must be an admitted result")
        _require_non_negative_integer(self.observed_bytes, "observed file bytes")
        if not isinstance(self.checksum_verified, bool):
            raise ValueError("checksum verification result must be boolean")


@dataclass(frozen=True, slots=True)
class DownloadBatchCompleted:
    """Complete one serial required-file batch."""

    item_count: int
    checksum_verified_count: int

    def __post_init__(self) -> None:
        _require_non_negative_integer(self.item_count, "download item count")
        _require_non_negative_integer(
            self.checksum_verified_count,
            "checksum-verified item count",
        )
        if self.checksum_verified_count > self.item_count:
            raise ValueError(
                "checksum-verified item count must not exceed the item count"
            )


type DownloadEvent = (
    DownloadItemStarted
    | DownloadAttemptStarted
    | DownloadTransferProgress
    | DownloadRetryScheduled
    | DownloadVerificationStarted
    | DownloadVerificationCompleted
    | DownloadPlacementStarted
    | DownloadPlacementCompleted
    | DownloadFinalVerificationStarted
    | DownloadFinalVerificationCompleted
    | DownloadItemCompleted
    | DownloadBatchCompleted
)


def _require_canonical_target(target: object) -> None:
    if type(target) is not str or not target:
        raise ValueError("download target must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in target):
        raise ValueError("download target must not contain control characters")
    if "\\" in target:
        raise ValueError("download target must use canonical POSIX separators")
    path = PurePosixPath(target)
    if path.is_absolute() or path.as_posix() != target:
        raise ValueError("download target must be canonical and relative")
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("download target must be a strict relative path")


def _require_integer(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")


def _require_positive_integer(value: object, label: str) -> None:
    _require_integer(value, label)
    if value < 1:
        raise ValueError(f"{label} must be positive")


def _require_non_negative_integer(value: object, label: str) -> None:
    _require_integer(value, label)
    if value < 0:
        raise ValueError(f"{label} must not be negative")


def _require_non_negative_number(value: object, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{label} must be finite and non-negative")
