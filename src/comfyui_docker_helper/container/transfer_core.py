"""Descriptor-anchored integrity and atomic placement for file transfers."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from comfyui_docker_helper.config.file_checksum import (
    validate_canonical_file_checksum,
)
from comfyui_docker_helper.config.url_validation import (
    DownloaderName,
    is_reserved_file_target_name,
)
from comfyui_docker_helper.errors import ApplicationError


class DownloadFilesError(ApplicationError):
    """A terminal file-transfer or local-invariant failure."""


class TransferDownloadFilesError(DownloadFilesError):
    """A transport or integrity failure eligible for another attempt."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        resume_authority: ResumeAuthority | None = None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        self.resume_authority = resume_authority
        super().__init__(message)


class TerminalTransferDownloadFilesError(DownloadFilesError):
    """An ordinary item failure that must not consume another attempt."""


class DownloadCancelled(DownloadFilesError):
    """A cooperative transport cancellation request was observed."""

    def __init__(
        self,
        message: str,
        *,
        resume_authority: ResumeAuthority | None = None,
    ) -> None:
        self.resume_authority = resume_authority
        super().__init__(message)


class ResumeRejectedDownloadFilesError(DownloadFilesError):
    """An admitted resumable request was rejected after exact cleanup."""


class _PlacementUncertain(DownloadFilesError):
    """Placement may have mutated final state, so cleanup is forbidden."""


class DownloadStatus(StrEnum):
    """Shared target outcome used by build and runtime orchestrators."""

    SKIPPED = "skipped"
    DOWNLOADED = "downloaded"


class VerificationStatus(StrEnum):
    """Whether observed bytes were compared with trusted expected identity."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class StagingDisposition(StrEnum):
    """Caller policy for exact proven current staging after interruption."""

    CLEAN = "clean"
    PRESERVE = "preserve"


@dataclass(frozen=True, slots=True)
class Aria2DownloadSettings:
    """Normalized aria2 transport settings."""

    rpc_port: int
    split: int
    max_connection_per_server: int
    min_split_size: str
    resume_download: bool


@dataclass(frozen=True, slots=True)
class HttpxDownloadSettings:
    """Normalized HTTPX transport settings."""

    timeout: int | float


@dataclass(frozen=True, slots=True)
class DownloaderSettings:
    """Normalized settings supplied to either transport adapter."""

    default: DownloaderName
    aria2: Aria2DownloadSettings
    httpx: HttpxDownloadSettings


@dataclass(frozen=True, slots=True)
class TransferIdentity:
    """Shared desired identity and deterministic staging projection."""

    canonical_bytes: bytes
    digest: str
    relative_target: str
    staging_name: str
    staging_target: Path


def project_transfer_identity(
    *,
    root: Path,
    url: str,
    target: Path,
    expected_checksum: str | None,
) -> TransferIdentity:
    """Project the sole canonical URL transfer identity."""
    if expected_checksum is not None:
        validate_canonical_file_checksum(expected_checksum)
    relative = _relative_target(root, target)
    canonical = json.dumps(
        {
            "schema_version": 1,
            "checksum": expected_checksum,
            "source": url,
            "source_type": "url",
            "target": relative.as_posix(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    suffix = hashlib.sha256(canonical).hexdigest()
    staging_name = f"cdh-{suffix}.part"
    return TransferIdentity(
        canonical_bytes=canonical,
        digest=f"sha256:{suffix}",
        relative_target=relative.as_posix(),
        staging_name=staging_name,
        staging_target=target.parent / ".cdh-staging" / staging_name,
    )


@dataclass(frozen=True, slots=True)
class ResumeAuthority:
    """Persisted proof that one partial belongs to the same desired identity."""

    identity_digest: str
    staging_device: int
    staging_inode: int
    control_device: int | None = None
    control_inode: int | None = None


class TransportSink:
    """Core-owned exact staging inode exposed through safe adapter operations."""

    __slots__ = (
        "_directory_fd",
        "_display_path",
        "_fd",
        "_metadata",
        "_name",
        "_resume_allowed",
        "_target_fd",
        "_target_name",
    )

    def __init__(
        self,
        *,
        fd: int,
        directory_fd: int,
        name: str,
        display_path: Path,
        metadata: os.stat_result,
        target_fd: int,
        target_name: str,
        resume_allowed: bool,
    ) -> None:
        self._fd = fd
        self._directory_fd = directory_fd
        self._name = name
        self._display_path = display_path
        self._metadata = metadata
        self._target_fd = target_fd
        self._target_name = target_name
        self._resume_allowed = resume_allowed

    @property
    def display_path(self) -> Path:
        """Return a diagnostic-only path; adapters must not open it."""
        return self._display_path

    @property
    def aria2_directory(self) -> str:
        """Return a procfs path anchored to the core-held directory descriptor."""
        return f"/proc/{os.getpid()}/fd/{self._directory_fd}"

    @property
    def aria2_name(self) -> str:
        return self._name

    @property
    def resume_allowed(self) -> bool:
        return self._resume_allowed

    def open_for_write(self) -> BinaryIO:
        """Reset and return a duplicate of the exact admitted staging inode."""
        _require_same_leaf(self._directory_fd, self._name, self._metadata)
        current = os.fstat(self._fd)
        _require_safe_staging_metadata(current, self._display_path)
        target = _stat_leaf(self._target_fd, self._target_name)
        if target is not None and _same_inode(target, current):
            raise DownloadFilesError(
                f"download staging aliases the final target: {self._display_path}"
            )
        duplicate = os.dup(self._fd)
        try:
            os.lseek(duplicate, 0, os.SEEK_SET)
            os.ftruncate(duplicate, 0)
            return os.fdopen(duplicate, "wb")
        except Exception:
            os.close(duplicate)
            raise

    def current_length(self) -> int:
        """Return the length of the exact admitted staging inode."""
        _require_same_leaf(self._directory_fd, self._name, self._metadata)
        return os.fstat(self._fd).st_size


@dataclass(frozen=True, slots=True)
class TransportRequest:
    """Narrow adapter input containing only source and a safe supplied sink."""

    url: str
    sink: TransportSink


@dataclass(frozen=True, slots=True)
class TransportDiagnostic:
    """Backend-namespaced, non-policy diagnostic summary."""

    namespace: Literal["httpx", "aria2"]
    summary: str

    def __post_init__(self) -> None:
        _validate_transport_namespace(self.namespace)
        if type(self.summary) is not str or not self.summary.strip():
            raise ValueError("transport diagnostic summary must be non-empty")


@dataclass(frozen=True, slots=True)
class TransportSuccess:
    """Completed transport metadata checked against supplied staging."""

    length: int
    namespace: Literal["httpx", "aria2"]
    http_status: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.length, bool) or not isinstance(self.length, int):
            raise ValueError("transport length must be an integer")
        if self.length < 0:
            raise ValueError("transport length must not be negative")
        _validate_transport_namespace(self.namespace)
        _validate_transport_http_status(self.http_status)
        if self.http_status is not None and self.http_status >= 400:
            raise ValueError("successful transport cannot carry an error HTTP status")
        if self.namespace == "aria2" and self.http_status is not None:
            raise ValueError("aria2 transport outcomes cannot carry HTTP status")
        if self.namespace == "httpx" and self.http_status is None:
            raise ValueError("successful HTTPX transport must carry exact HTTP status")


@dataclass(frozen=True, slots=True)
class TransportRetryable:
    """Expected remote failure eligible for caller-owned retry policy."""

    diagnostic: TransportDiagnostic
    http_status: int | None = None
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        _validate_transport_diagnostic(self.diagnostic)
        _validate_transport_http_status(self.http_status)
        if self.retry_after_seconds is not None:
            if (
                isinstance(self.retry_after_seconds, bool)
                or not isinstance(self.retry_after_seconds, (int, float))
                or not math.isfinite(self.retry_after_seconds)
                or self.retry_after_seconds < 0
            ):
                raise ValueError(
                    "transport Retry-After must be finite and non-negative"
                )
            if self.diagnostic.namespace != "httpx" or self.http_status is None:
                raise ValueError(
                    "only an HTTPX response may carry normalized Retry-After"
                )
        _validate_failure_http_semantics(
            self.diagnostic,
            self.http_status,
            retryable=True,
        )


@dataclass(frozen=True, slots=True)
class TransportOrdinaryTerminal:
    """Expected remote failure ineligible for another transport attempt."""

    diagnostic: TransportDiagnostic
    http_status: int | None = None

    def __post_init__(self) -> None:
        _validate_transport_diagnostic(self.diagnostic)
        _validate_transport_http_status(self.http_status)
        _validate_failure_http_semantics(
            self.diagnostic,
            self.http_status,
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class TransportCancelled:
    """Cooperative cancellation observed before conclusive completion."""

    diagnostic: TransportDiagnostic

    def __post_init__(self) -> None:
        _validate_transport_diagnostic(self.diagnostic)


@dataclass(frozen=True, slots=True)
class TransportResumeRejected:
    """aria2 rejected one request that used exact admitted resume authority."""

    diagnostic: TransportDiagnostic

    def __post_init__(self) -> None:
        _validate_transport_diagnostic(self.diagnostic)
        if self.diagnostic.namespace != "aria2":
            raise ValueError("only aria2 may reject a resumed transport request")


type TransportOutcome = (
    TransportSuccess
    | TransportRetryable
    | TransportOrdinaryTerminal
    | TransportCancelled
    | TransportResumeRejected
)


def _validate_transport_http_status(status: int | None) -> None:
    if status is None:
        return
    if isinstance(status, bool) or not isinstance(status, int):
        raise ValueError("transport HTTP status must be an integer")
    if not 100 <= status <= 599:
        raise ValueError("transport HTTP status is outside the valid range")


def _validate_transport_diagnostic(diagnostic: TransportDiagnostic) -> None:
    if not isinstance(diagnostic, TransportDiagnostic):
        raise ValueError("transport diagnostic has an invalid type")
    TransportDiagnostic.__post_init__(diagnostic)


def _validate_transport_namespace(namespace: object) -> None:
    if type(namespace) is not str or namespace not in {"httpx", "aria2"}:
        raise ValueError("transport namespace is invalid")


def _validate_failure_http_semantics(
    diagnostic: TransportDiagnostic,
    status: int | None,
    *,
    retryable: bool,
) -> None:
    if diagnostic.namespace == "aria2":
        if status is not None:
            raise ValueError("aria2 transport outcomes cannot carry HTTP status")
        return
    if status is None:
        return
    if retryable:
        if status not in {408, 429} and not 500 <= status <= 599:
            raise ValueError("HTTPX retryable outcome has a terminal HTTP status")
        return
    if not 400 <= status <= 499 or status in {408, 429}:
        raise ValueError("HTTPX terminal outcome has a retryable HTTP status")


class DownloadBackend(Protocol):
    """Backend adapter that writes only through its supplied staging sink."""

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome: ...


@dataclass(frozen=True, slots=True)
class FileTransferRequest:
    """One admitted target operation with caller-owned staging disposition."""

    root: Path
    url: str
    target: Path
    overwrite: bool
    expected_checksum: str | None
    staging_disposition: StagingDisposition
    resume_authority: ResumeAuthority | None = None
    preserve_on_retryable: bool = False
    preserve_on_cancellation: bool = False


@dataclass(frozen=True, slots=True)
class FileTransferOutcome:
    """Typed expected/observed handoff for consumers and later manifest work."""

    status: DownloadStatus
    target: Path
    staging_target: Path
    expected_checksum: str | None
    observed_checksum: str | None
    observed_length: int
    verification: VerificationStatus


class Logger(Protocol):
    """Minimal logger protocol used by transfer orchestration."""

    def __call__(self, message: str) -> None: ...


@dataclass(slots=True)
class _DirectoryAnchor:
    path: Path
    fd: int
    metadata: os.stat_result


@dataclass(frozen=True, slots=True)
class _CreatedDirectoryEntry:
    parent_fd: int
    display_path: Path


class _TargetAnchor:
    def __init__(self, *, root: Path, target: Path, create: bool) -> None:
        relative = _relative_target(root, target)
        self.root = root
        self.target = target
        self.target_name = relative.parts[-1]
        self.directories: list[_DirectoryAnchor] = []
        self.created_directories: list[_CreatedDirectoryEntry] = []
        root_fd = _open_directory_path(root, "COMFYUI_PATH")
        self.directories.append(_DirectoryAnchor(root, root_fd, os.fstat(root_fd)))
        current_path = root
        try:
            for part in relative.parts[:-1]:
                parent_fd = self.directories[-1].fd
                child_fd, created = _open_child_directory(
                    parent_fd,
                    part,
                    create=create,
                )
                current_path /= part
                if created:
                    self.created_directories.append(
                        _CreatedDirectoryEntry(parent_fd, current_path)
                    )
                self.directories.append(
                    _DirectoryAnchor(current_path, child_fd, os.fstat(child_fd))
                )
            self._target_parent = self.directories[-1]
        except Exception:
            self.close()
            raise

    @property
    def parent(self) -> _DirectoryAnchor:
        return self._target_parent

    def add_staging_directory(self, *, create: bool = True) -> _DirectoryAnchor:
        fd, created = _open_child_directory(
            self.parent.fd,
            ".cdh-staging",
            create=create,
        )
        anchor = _DirectoryAnchor(
            self.target.parent / ".cdh-staging",
            fd,
            os.fstat(fd),
        )
        if created:
            self.created_directories.append(
                _CreatedDirectoryEntry(self.parent.fd, anchor.path)
            )
        self.directories.append(anchor)
        return anchor

    def make_created_directories_durable(self) -> None:
        """Persist each new directory entry from the deepest parent outward."""
        for created in reversed(self.created_directories):
            try:
                os.fsync(created.parent_fd)
            except OSError as error:
                raise DownloadFilesError(
                    "download target directory could not be made durable: "
                    f"{created.display_path}: {error}"
                ) from error
        self.created_directories.clear()

    def verify_visible(self) -> None:
        for anchor in self.directories:
            try:
                observed = anchor.path.lstat()
            except OSError as error:
                raise DownloadFilesError(
                    f"download directory changed during transfer: {anchor.path}"
                ) from error
            if not stat.S_ISDIR(observed.st_mode) or not _same_inode(
                anchor.metadata, observed
            ):
                raise DownloadFilesError(
                    f"download directory changed during transfer: {anchor.path}"
                )

    def close(self) -> None:
        for anchor in reversed(self.directories):
            with suppress(OSError):
                os.close(anchor.fd)
        self.directories.clear()


@dataclass(slots=True)
class _OwnedLeaf:
    directory_fd: int
    name: str
    fd: int
    metadata: os.stat_result
    display_path: Path

    def close(self) -> None:
        with suppress(OSError):
            os.close(self.fd)


def transfer_file(
    request: FileTransferRequest,
    *,
    backend: DownloadBackend,
    settings: DownloaderSettings,
) -> FileTransferOutcome:
    """Perform one descriptor-anchored transfer, verification, and placement."""
    identity = project_transfer_identity(
        root=request.root,
        url=request.url,
        target=request.target,
        expected_checksum=request.expected_checksum,
    )
    anchor = _TargetAnchor(root=request.root, target=request.target, create=True)
    staging: _OwnedLeaf | None = None
    control: _OwnedLeaf | None = None
    try:
        initial = _stat_leaf(anchor.parent.fd, anchor.target_name)
        existing = _existing_target_outcome(
            request,
            initial,
            parent_fd=anchor.parent.fd,
            staging_target=identity.staging_target,
        )
        if existing is not None:
            _require_same_safe_final_leaf(
                anchor.parent.fd,
                anchor.target_name,
                initial,
                request.target,
            )
            anchor.verify_visible()
            return existing

        staging_anchor = anchor.add_staging_directory()
        anchor.verify_visible()
        anchor.make_created_directories_durable()
        staging, control = _admit_staging(
            request,
            identity,
            staging_anchor=staging_anchor,
            target_parent_fd=anchor.parent.fd,
            target_name=anchor.target_name,
            initial=initial,
        )
        sink = TransportSink(
            fd=staging.fd,
            directory_fd=staging.directory_fd,
            name=staging.name,
            display_path=staging.display_path,
            metadata=staging.metadata,
            target_fd=anchor.parent.fd,
            target_name=anchor.target_name,
            resume_allowed=request.resume_authority is not None,
        )
        transport_request = TransportRequest(url=request.url, sink=sink)
        try:
            raw_transport = backend.download(transport_request, settings)
            validated_transport = _validate_transport_outcome(raw_transport)
        except Exception:
            _cleanup_after_unquiescent_transport_failure(staging, control)
            raise
        try:
            control = _reconcile_control_after_transport(
                request,
                validated_transport,
                anchor=anchor,
                staging=staging,
                control=control,
                initial=initial,
            )
        except Exception:
            # A failed lineage proof never authorizes touching a possibly replaced
            # control name. The still-held staging inode remains exact.
            _cleanup_owned_transfer(staging, None)
            raise
        try:
            transport = _require_transport_success(validated_transport)
            outcome = _verify_and_place(
                request,
                transport,
                anchor=anchor,
                staging=staging,
                initial=initial,
            )
        except _PlacementUncertain:
            raise
        except _TransportRetryableObserved as observed:
            authority = None
            if request.preserve_on_retryable:
                _require_owned_leaf_visible(staging)
                if control is None:
                    raise DownloadFilesError(
                        "resumable aria2 failure did not leave exact control state"
                    ) from None
                _require_owned_leaf_visible(control)
                authority = _resume_authority(identity, staging, control)
            else:
                _cleanup_owned_transfer(staging, control)
            raise TransferDownloadFilesError(
                observed.outcome.diagnostic.summary,
                retry_after_seconds=observed.outcome.retry_after_seconds,
                resume_authority=authority,
            ) from None
        except _TransportCancelledObserved as observed:
            authority = None
            if request.preserve_on_cancellation:
                _require_owned_leaf_visible(staging)
                if control is None:
                    raise DownloadFilesError(
                        "cancelled aria2 transfer did not leave exact control state"
                    ) from None
                _require_owned_leaf_visible(control)
                authority = _resume_authority(identity, staging, control)
            else:
                _cleanup_owned_transfer(staging, control)
            raise DownloadCancelled(
                observed.outcome.diagnostic.summary,
                resume_authority=authority,
            ) from None
        except _TransportResumeRejectedObserved as observed:
            if request.resume_authority is None or not sink.resume_allowed:
                _cleanup_owned_transfer(staging, control)
                raise DownloadFilesError(
                    "transport rejected resume without exact admitted authority"
                ) from None
            _cleanup_owned_transfer(staging, control)
            raise ResumeRejectedDownloadFilesError(
                observed.outcome.diagnostic.summary
            ) from None
        except _ChecksumMismatch:
            _cleanup_owned_transfer(staging, control)
            raise TransferDownloadFilesError(
                f"download checksum does not match expected identity: {request.target}"
            ) from None
        except Exception:
            _cleanup_owned_transfer(staging, control)
            raise

        if control is not None:
            _unlink_owned_leaf(control)
            try:
                os.fsync(control.directory_fd)
            except OSError as error:
                raise DownloadFilesError(
                    f"aria2 control cleanup could not be made durable: {error}"
                ) from error
        return outcome
    finally:
        if control is not None:
            control.close()
        if staging is not None:
            staging.close()
        anchor.close()


def transfer_staging_target(request: FileTransferRequest) -> Path:
    """Return the shared desired-identity staging projection."""
    return project_transfer_identity(
        root=request.root,
        url=request.url,
        target=request.target,
        expected_checksum=request.expected_checksum,
    ).staging_target


def discard_preserved_transfer(request: FileTransferRequest) -> None:
    """Remove one exact authority-proven partial without touching foreign leaves."""
    if (
        request.staging_disposition is not StagingDisposition.PRESERVE
        or request.resume_authority is None
    ):
        raise DownloadFilesError(
            "preserved download cleanup requires exact resume authority"
        )
    identity = project_transfer_identity(
        root=request.root,
        url=request.url,
        target=request.target,
        expected_checksum=request.expected_checksum,
    )
    anchor = _TargetAnchor(root=request.root, target=request.target, create=False)
    staging: _OwnedLeaf | None = None
    control: _OwnedLeaf | None = None
    try:
        staging_anchor = anchor.add_staging_directory(create=False)
        anchor.verify_visible()
        staging, control = _admit_staging(
            request,
            identity,
            staging_anchor=staging_anchor,
            target_parent_fd=anchor.parent.fd,
            target_name=anchor.target_name,
            initial=_stat_leaf(anchor.parent.fd, anchor.target_name),
        )
        _cleanup_owned_transfer(staging, control)
    finally:
        if control is not None:
            control.close()
        if staging is not None:
            staging.close()
        anchor.close()


def admitted_regular_final(root: Path, target: Path) -> bool:
    """Descriptor-safely admit an existing regular final without mutation."""
    try:
        anchor = _TargetAnchor(root=root, target=target, create=False)
    except _MissingDirectory:
        return False
    try:
        observed = _stat_leaf(anchor.parent.fd, anchor.target_name)
        if observed is None:
            return False
        _require_safe_final_metadata(observed, target)
        _require_same_safe_final_leaf(
            anchor.parent.fd,
            anchor.target_name,
            observed,
            target,
        )
        anchor.verify_visible()
        return True
    finally:
        anchor.close()


def verify_required_final(
    *,
    root: Path,
    target: Path,
    expected_checksum: str | None,
) -> None:
    """Prove a required final through a fresh descriptor-anchored admission."""
    try:
        anchor = _TargetAnchor(root=root, target=target, create=False)
    except _MissingDirectory as error:
        raise DownloadFilesError(
            f"required download target is missing: {target}"
        ) from error
    try:
        observed = _stat_leaf(anchor.parent.fd, anchor.target_name)
        if observed is None:
            raise DownloadFilesError(f"required download target is missing: {target}")
        _require_safe_final_metadata(
            observed,
            target,
            label="required download target",
        )
        if expected_checksum is not None:
            digest, _, metadata = _hash_leaf(anchor.parent.fd, anchor.target_name)
            if digest != expected_checksum:
                raise DownloadFilesError(
                    f"required download target checksum does not match: {target}"
                )
            observed = metadata
        _require_same_safe_final_leaf(
            anchor.parent.fd,
            anchor.target_name,
            observed,
            target,
            label="required download target",
        )
        anchor.verify_visible()
    finally:
        anchor.close()


def _existing_target_outcome(
    request: FileTransferRequest,
    initial: os.stat_result | None,
    *,
    parent_fd: int,
    staging_target: Path,
) -> FileTransferOutcome | None:
    if initial is None:
        return None
    _require_safe_final_metadata(initial, request.target)
    if request.expected_checksum is None:
        if request.overwrite:
            return None
        return FileTransferOutcome(
            status=DownloadStatus.SKIPPED,
            target=request.target,
            staging_target=staging_target,
            expected_checksum=None,
            observed_checksum=None,
            observed_length=initial.st_size,
            verification=VerificationStatus.UNVERIFIED,
        )

    observed_checksum, observed_length, observed_stat = _hash_leaf(
        parent_fd,
        request.target.name,
    )
    _require_same_safe_final_leaf(
        parent_fd,
        request.target.name,
        observed_stat,
        request.target,
    )
    if observed_checksum == request.expected_checksum:
        return FileTransferOutcome(
            status=DownloadStatus.SKIPPED,
            target=request.target,
            staging_target=staging_target,
            expected_checksum=request.expected_checksum,
            observed_checksum=observed_checksum,
            observed_length=observed_length,
            verification=VerificationStatus.VERIFIED,
        )
    if not request.overwrite:
        raise TerminalTransferDownloadFilesError(
            f"existing download target checksum does not match: {request.target}"
        )
    return None


def _admit_staging(
    request: FileTransferRequest,
    identity: TransferIdentity,
    *,
    staging_anchor: _DirectoryAnchor,
    target_parent_fd: int,
    target_name: str,
    initial: os.stat_result | None,
) -> tuple[_OwnedLeaf, _OwnedLeaf | None]:
    control_name = f"{identity.staging_name}.aria2"
    temp_name = f"{control_name}__temp"
    if _stat_leaf(staging_anchor.fd, temp_name) is not None:
        raise DownloadFilesError(
            "foreign aria2 temporary control artifact exists: "
            f"{identity.staging_target}.aria2__temp"
        )
    if request.staging_disposition is StagingDisposition.PRESERVE:
        authority = request.resume_authority
        if authority is None or authority.identity_digest != identity.digest:
            raise DownloadFilesError(
                "preserved download staging lacks same-identity ownership authority"
            )
        staging = _open_owned_leaf(
            staging_anchor.fd,
            identity.staging_name,
            identity.staging_target,
        )
        if (staging.metadata.st_dev, staging.metadata.st_ino) != (
            authority.staging_device,
            authority.staging_inode,
        ):
            staging.close()
            raise DownloadFilesError(
                "preserved download staging identity does not match authority"
            )
        control_metadata = _stat_leaf(staging_anchor.fd, control_name)
        expected_control = (authority.control_device, authority.control_inode)
        control: _OwnedLeaf | None = None
        if expected_control == (None, None) or control_metadata is None:
            staging.close()
            raise DownloadFilesError("preserved aria2 control state is missing")
        else:
            control = _open_owned_leaf(
                staging_anchor.fd,
                control_name,
                Path(f"{identity.staging_target}.aria2"),
            )
            if control.metadata.st_uid != os.geteuid():
                control.close()
                staging.close()
                raise DownloadFilesError(
                    "preserved aria2 control is not owned by the effective user"
                )
            if (
                expected_control == (None, None)
                or (
                    control.metadata.st_dev,
                    control.metadata.st_ino,
                )
                != expected_control
            ):
                control.close()
                staging.close()
                raise DownloadFilesError(
                    "preserved aria2 control identity does not match authority"
                )
    else:
        control = None
        if request.resume_authority is not None:
            raise DownloadFilesError("resume authority requires preserve disposition")
        if _stat_leaf(staging_anchor.fd, identity.staging_name) is not None:
            raise DownloadFilesError(
                f"foreign download staging artifact exists: {identity.staging_target}"
            )
        if _stat_leaf(staging_anchor.fd, control_name) is not None:
            raise DownloadFilesError(
                "foreign aria2 control artifact exists: "
                f"{identity.staging_target}.aria2"
            )
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            fd = os.open(identity.staging_name, flags, 0o600, dir_fd=staging_anchor.fd)
        except OSError as error:
            raise DownloadFilesError(
                "download staging could not be created safely: "
                f"{identity.staging_target}"
            ) from error
        metadata = os.fstat(fd)
        try:
            _require_safe_staging_metadata(metadata, identity.staging_target)
            staging = _OwnedLeaf(
                staging_anchor.fd,
                identity.staging_name,
                fd,
                metadata,
                identity.staging_target,
            )
        except Exception:
            os.close(fd)
            raise

    target = initial or _stat_leaf(target_parent_fd, target_name)
    if target is not None and _same_inode(target, staging.metadata):
        staging.close()
        if control is not None:
            control.close()
        raise DownloadFilesError(
            f"download staging aliases the final target: {identity.staging_target}"
        )
    return staging, control


def _validate_transport_outcome(outcome: TransportOutcome) -> TransportOutcome:
    """Validate one typed terminal adapter result before filesystem admission."""
    try:
        if isinstance(outcome, TransportSuccess):
            TransportSuccess.__post_init__(outcome)
        elif isinstance(outcome, TransportRetryable):
            TransportRetryable.__post_init__(outcome)
        elif isinstance(outcome, TransportOrdinaryTerminal):
            TransportOrdinaryTerminal.__post_init__(outcome)
        elif isinstance(outcome, TransportCancelled):
            TransportCancelled.__post_init__(outcome)
        elif isinstance(outcome, TransportResumeRejected):
            TransportResumeRejected.__post_init__(outcome)
    except (TypeError, ValueError) as error:
        raise DownloadFilesError(
            "transport adapter returned an invalid outcome"
        ) from error
    if not isinstance(
        outcome,
        (
            TransportSuccess,
            TransportRetryable,
            TransportOrdinaryTerminal,
            TransportCancelled,
            TransportResumeRejected,
        ),
    ):
        raise DownloadFilesError("transport adapter returned an invalid outcome")
    return outcome


def _require_transport_success(outcome: TransportOutcome) -> TransportSuccess:
    """Project already-validated adapter semantics without backend policy."""
    if isinstance(outcome, TransportSuccess):
        return outcome
    if isinstance(outcome, TransportRetryable):
        raise _TransportRetryableObserved(outcome)
    if isinstance(outcome, TransportOrdinaryTerminal):
        raise TerminalTransferDownloadFilesError(outcome.diagnostic.summary)
    if isinstance(outcome, TransportCancelled):
        raise _TransportCancelledObserved(outcome)
    if isinstance(outcome, TransportResumeRejected):
        raise _TransportResumeRejectedObserved(outcome)
    raise DownloadFilesError("transport adapter returned an invalid outcome")


class _TransportRetryableObserved(Exception):
    """Internal typed handoff before core-owned cleanup or preservation."""

    def __init__(self, outcome: TransportRetryable) -> None:
        self.outcome = outcome
        super().__init__(outcome.diagnostic.summary)


class _TransportCancelledObserved(Exception):
    """Internal typed handoff before core-owned cleanup or preservation."""

    def __init__(self, outcome: TransportCancelled) -> None:
        self.outcome = outcome
        super().__init__(outcome.diagnostic.summary)


class _TransportResumeRejectedObserved(Exception):
    """Internal typed handoff before core-owned exact resume cleanup."""

    def __init__(self, outcome: TransportResumeRejected) -> None:
        self.outcome = outcome
        super().__init__(outcome.diagnostic.summary)


def _resume_authority(
    identity: TransferIdentity,
    staging: _OwnedLeaf,
    control: _OwnedLeaf | None,
) -> ResumeAuthority:
    return ResumeAuthority(
        identity_digest=identity.digest,
        staging_device=staging.metadata.st_dev,
        staging_inode=staging.metadata.st_ino,
        control_device=control.metadata.st_dev if control is not None else None,
        control_inode=control.metadata.st_ino if control is not None else None,
    )


def _verify_and_place(
    request: FileTransferRequest,
    transport: TransportSuccess,
    *,
    anchor: _TargetAnchor,
    staging: _OwnedLeaf,
    initial: os.stat_result | None,
) -> FileTransferOutcome:
    observed_checksum, observed_length, staged_stat = _inspect_staged_file(
        staging,
        expected_checksum=request.expected_checksum,
    )
    if transport.length < 0 or transport.length != observed_length:
        raise TransferDownloadFilesError(
            f"download transport length does not match staged bytes: {request.target}"
        )
    if (
        request.expected_checksum is not None
        and observed_checksum != request.expected_checksum
    ):
        raise _ChecksumMismatch

    anchor.verify_visible()
    current = _stat_leaf(anchor.parent.fd, anchor.target_name)
    if not _same_optional_stat(initial, current):
        raise DownloadFilesError(
            f"download target changed before atomic placement: {request.target}"
        )
    _claim_verified_inode(staging, staged_stat)
    try:
        if initial is None:
            _place_missing_target(staging, anchor, staged_stat)
        else:
            _place_replacement(staging, anchor, staged_stat, initial)
        try:
            os.fsync(staging.directory_fd)
            os.fsync(anchor.parent.fd)
        except OSError:
            _rollback_placement(staging, anchor, staged_stat, initial)
            raise
        placed = _stat_leaf(anchor.parent.fd, anchor.target_name)
        if placed is None or not _same_inode(staged_stat, placed):
            if initial is not None:
                _rollback_exchange_or_preserve(
                    staging,
                    anchor,
                    initial=initial,
                )
            raise _PlacementUncertain(
                "download target identity is uncertain after placement: "
                f"{request.target}"
            )
        anchor.verify_visible()
        if initial is not None:
            displaced = _stat_leaf(staging.directory_fd, staging.name)
            if displaced is None or not _same_stat(initial, displaced):
                raise _PlacementUncertain(
                    f"displaced download target identity is uncertain: {request.target}"
                )
            _unlink_exact_leaf(
                directory_fd=staging.directory_fd,
                name=staging.name,
                expected=initial,
                display_path=staging.display_path,
                uncertain_on_mismatch=True,
            )
            os.fsync(staging.directory_fd)
        return FileTransferOutcome(
            status=DownloadStatus.DOWNLOADED,
            target=request.target,
            staging_target=staging.display_path,
            expected_checksum=request.expected_checksum,
            observed_checksum=observed_checksum,
            observed_length=observed_length,
            verification=(
                VerificationStatus.VERIFIED
                if request.expected_checksum is not None
                else VerificationStatus.UNVERIFIED
            ),
        )
    except OSError as error:
        raise DownloadFilesError(
            f"download staging could not be durably placed: {request.target}: {error}"
        ) from error


def _claim_verified_inode(staging: _OwnedLeaf, staged_stat: os.stat_result) -> None:
    if not _same_stat(staged_stat, os.fstat(staging.fd)):
        raise DownloadFilesError(
            f"download staging changed before atomic claim: {staging.display_path}"
        )
    claim_name = f".{staging.name}.commit-{secrets.token_hex(32)}"
    _link_fd_noreplace(staging.fd, staging.directory_fd, claim_name)
    claim = _stat_leaf(staging.directory_fd, claim_name)
    original = _stat_leaf(staging.directory_fd, staging.name)
    if claim is None or not _same_inode(staged_stat, claim):
        raise _PlacementUncertain(
            f"download staging claim identity is uncertain: {staging.display_path}"
        )
    if original is None or not _same_inode(staged_stat, original):
        _unlink_exact_leaf(
            directory_fd=staging.directory_fd,
            name=claim_name,
            expected=claim,
            display_path=staging.display_path,
            uncertain_on_mismatch=True,
        )
        raise DownloadFilesError(
            f"download staging changed before atomic claim: {staging.display_path}"
        )
    original_name = staging.name
    staging.name = claim_name
    _unlink_exact_leaf(
        directory_fd=staging.directory_fd,
        name=original_name,
        expected=staged_stat,
        display_path=staging.display_path,
        uncertain_on_mismatch=False,
    )
    staging.metadata = os.fstat(staging.fd)
    if not _same_stat(staged_stat, staging.metadata):
        raise _PlacementUncertain(
            f"download staging changed during atomic claim: {staging.display_path}"
        )


def _place_missing_target(
    staging: _OwnedLeaf,
    anchor: _TargetAnchor,
    staged_stat: os.stat_result,
) -> None:
    try:
        _renameat2(
            staging.directory_fd,
            staging.name,
            anchor.parent.fd,
            anchor.target_name,
            flags=1,
        )
    except FileExistsError as error:
        raise DownloadFilesError(
            f"download target appeared before atomic placement: {anchor.target}"
        ) from error
    except OSError as error:
        final = _stat_leaf(anchor.parent.fd, anchor.target_name)
        claim = _stat_leaf(staging.directory_fd, staging.name)
        if final is not None and _same_inode(staged_stat, final) and claim is None:
            return
        if claim is not None and _same_inode(staged_stat, claim):
            raise DownloadFilesError(
                f"atomic download placement failed: {anchor.target}: {error}"
            ) from error
        raise _PlacementUncertain(
            f"atomic download placement is uncertain: {anchor.target}: {error}"
        ) from error


def _place_replacement(
    staging: _OwnedLeaf,
    anchor: _TargetAnchor,
    staged_stat: os.stat_result,
    initial: os.stat_result,
) -> None:
    try:
        _renameat2(
            staging.directory_fd,
            staging.name,
            anchor.parent.fd,
            anchor.target_name,
            flags=2,
        )
    except OSError as error:
        raise _PlacementUncertain(
            f"atomic download replacement is uncertain: {anchor.target}: {error}"
        ) from error
    final = _stat_leaf(anchor.parent.fd, anchor.target_name)
    displaced = _stat_leaf(staging.directory_fd, staging.name)
    if (
        final is not None
        and _same_inode(staged_stat, final)
        and displaced is not None
        and _same_stat(initial, displaced)
    ):
        return
    _rollback_exchange_or_preserve(staging, anchor, initial=initial)
    raise DownloadFilesError(
        f"download target changed during atomic placement: {anchor.target}"
    )


def _rollback_placement(
    staging: _OwnedLeaf,
    anchor: _TargetAnchor,
    staged_stat: os.stat_result,
    initial: os.stat_result | None,
) -> None:
    if initial is None:
        final = _stat_leaf(anchor.parent.fd, anchor.target_name)
        claim = _stat_leaf(staging.directory_fd, staging.name)
        if final is None or not _same_inode(staged_stat, final) or claim is not None:
            raise _PlacementUncertain(
                f"new download target cannot be safely rolled back: {anchor.target}"
            )
        _renameat2(
            anchor.parent.fd,
            anchor.target_name,
            staging.directory_fd,
            staging.name,
            flags=1,
        )
    else:
        final = _stat_leaf(anchor.parent.fd, anchor.target_name)
        displaced = _stat_leaf(staging.directory_fd, staging.name)
        if (
            final is None
            or not _same_inode(staged_stat, final)
            or displaced is None
            or not _same_stat(initial, displaced)
        ):
            raise _PlacementUncertain(
                "replaced download target cannot be safely rolled back: "
                f"{anchor.target}"
            )
        _renameat2(
            staging.directory_fd,
            staging.name,
            anchor.parent.fd,
            anchor.target_name,
            flags=2,
        )
    try:
        os.fsync(staging.directory_fd)
        os.fsync(anchor.parent.fd)
    except OSError as error:
        raise _PlacementUncertain(
            f"download placement rollback durability is uncertain: {anchor.target}"
        ) from error


def _rollback_exchange_or_preserve(
    staging: _OwnedLeaf,
    anchor: _TargetAnchor,
    *,
    initial: os.stat_result,
) -> None:
    final = _stat_leaf(anchor.parent.fd, anchor.target_name)
    displaced = _stat_leaf(staging.directory_fd, staging.name)
    if displaced is None or not _same_inode(initial, displaced) or final is None:
        raise _PlacementUncertain(
            f"download replacement cannot be safely rolled back: {anchor.target}"
        )
    try:
        _renameat2(
            staging.directory_fd,
            staging.name,
            anchor.parent.fd,
            anchor.target_name,
            flags=2,
        )
    except OSError as error:
        raise _PlacementUncertain(
            f"download replacement rollback failed: {anchor.target}: {error}"
        ) from error
    restored = _stat_leaf(anchor.parent.fd, anchor.target_name)
    if restored is None or not _same_inode(initial, restored):
        raise _PlacementUncertain(
            f"download replacement rollback identity is uncertain: {anchor.target}"
        )
    try:
        os.fsync(staging.directory_fd)
        os.fsync(anchor.parent.fd)
    except OSError as error:
        raise _PlacementUncertain(
            f"download replacement rollback durability is uncertain: {anchor.target}"
        ) from error


def _inspect_staged_file(
    staging: _OwnedLeaf,
    *,
    expected_checksum: str | None,
) -> tuple[str | None, int, os.stat_result]:
    _require_owned_leaf_visible(staging)
    metadata = os.fstat(staging.fd)
    _require_safe_staging_metadata(metadata, staging.display_path)
    os.lseek(staging.fd, 0, os.SEEK_SET)
    digest = _hash_fd(staging.fd) if expected_checksum is not None else None
    after = os.fstat(staging.fd)
    if not _same_stat(metadata, after):
        raise DownloadFilesError(
            f"download staging changed during verification: {staging.display_path}"
        )
    os.fsync(staging.fd)
    _require_owned_leaf_visible(staging)
    return digest, after.st_size, after


def _hash_leaf(directory_fd: int, name: str) -> tuple[str, int, os.stat_result]:
    fd = _open_regular_leaf(directory_fd, name, "download target")
    try:
        before = os.fstat(fd)
        digest = _hash_fd(fd)
        after = os.fstat(fd)
        if not _same_stat(before, after):
            raise DownloadFilesError("download target changed during verification")
        return digest, after.st_size, after
    finally:
        os.close(fd)


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _cleanup_after_unquiescent_transport_failure(
    staging: _OwnedLeaf,
    control: _OwnedLeaf | None,
) -> None:
    """Clean only identities still exact before a typed quiescent result exists."""
    exact_control = None
    if control is not None:
        observed = _stat_leaf(control.directory_fd, control.name)
        current = os.fstat(control.fd)
        if observed is not None and _same_inode(observed, current):
            exact_control = control
    _cleanup_owned_transfer(staging, exact_control)


def _reconcile_control_after_transport(
    request: FileTransferRequest,
    outcome: TransportOutcome,
    *,
    anchor: _TargetAnchor,
    staging: _OwnedLeaf,
    control: _OwnedLeaf | None,
    initial: os.stat_result | None,
) -> _OwnedLeaf | None:
    """Admit one aria2 atomic-save generation after typed item quiescence."""
    control_name = f"{staging.name}.aria2"
    temp_name = f"{control_name}__temp"
    mutation_authorized = _transport_namespace(outcome) == "aria2"
    target = _stat_leaf(anchor.parent.fd, anchor.target_name)
    _require_transfer_path_stable(anchor, staging, initial=initial, target=target)

    temp_metadata = _stat_leaf(staging.directory_fd, temp_name)
    if temp_metadata is not None:
        if not mutation_authorized:
            raise DownloadFilesError(
                "non-aria2 transport left an unauthorized temporary control artifact"
            )
        current_control = _stat_leaf(staging.directory_fd, control_name)
        protected = [os.fstat(staging.fd)]
        if target is not None:
            protected.append(target)
        if current_control is not None:
            protected.append(current_control)
        if control is not None:
            protected.append(os.fstat(control.fd))
        temp = _capture_managed_control_leaf(
            staging.directory_fd,
            temp_name,
            Path(f"{staging.display_path}.aria2__temp"),
            anchor=anchor,
            staging=staging,
            initial=initial,
            protected=protected,
        )
        try:
            _unlink_owned_leaf(temp)
            os.fsync(temp.directory_fd)
        except OSError as error:
            raise DownloadFilesError(
                f"aria2 temporary control cleanup is not durable: {error}"
            ) from error
        finally:
            temp.close()
        raise DownloadFilesError(
            "aria2 left a temporary control artifact after the item became quiescent"
        )

    observed = _stat_leaf(staging.directory_fd, control_name)
    preserve_required = (
        isinstance(outcome, TransportRetryable) and request.preserve_on_retryable
    ) or (isinstance(outcome, TransportCancelled) and request.preserve_on_cancellation)
    if observed is None:
        if control is None:
            if preserve_required:
                raise DownloadFilesError(
                    "resumable aria2 failure did not leave exact control state"
                )
            return None
        old = os.fstat(control.fd)
        if old.st_nlink != 0:
            raise DownloadFilesError(
                "aria2 control disappeared without unlinking the held generation"
            )
        if preserve_required:
            raise DownloadFilesError(
                "resumable aria2 failure lost its exact control generation"
            )
        control.close()
        return None

    if not mutation_authorized:
        raise DownloadFilesError(
            "non-aria2 transport left an unauthorized control artifact"
        )

    if control is not None and _same_inode(observed, os.fstat(control.fd)):
        _stabilize_managed_control_leaf(
            control,
            anchor=anchor,
            staging=staging,
            initial=initial,
            protected=[os.fstat(staging.fd), *([target] if target is not None else [])],
            expected=control.metadata,
        )
        _require_control_temp_absent(staging.directory_fd, temp_name)
        return control

    old_metadata = os.fstat(control.fd) if control is not None else None
    if old_metadata is not None and old_metadata.st_nlink != 0:
        raise DownloadFilesError(
            "aria2 replaced control without unlinking the held generation"
        )
    protected = [os.fstat(staging.fd)]
    if target is not None:
        protected.append(target)
    if old_metadata is not None:
        protected.append(old_metadata)
    successor = _capture_managed_control_leaf(
        staging.directory_fd,
        control_name,
        Path(f"{staging.display_path}.aria2"),
        anchor=anchor,
        staging=staging,
        initial=initial,
        protected=protected,
    )
    if control is not None and os.fstat(control.fd).st_nlink != 0:
        successor.close()
        raise DownloadFilesError(
            "aria2 old control generation was relinked during successor admission"
        )
    try:
        _require_control_temp_absent(staging.directory_fd, temp_name)
    except Exception:
        successor.close()
        raise
    if control is not None:
        control.close()
    return successor


def _transport_namespace(outcome: TransportOutcome) -> Literal["httpx", "aria2"]:
    if isinstance(outcome, TransportSuccess):
        return outcome.namespace
    return outcome.diagnostic.namespace


def _capture_managed_control_leaf(
    directory_fd: int,
    name: str,
    display: Path,
    *,
    anchor: _TargetAnchor,
    staging: _OwnedLeaf,
    initial: os.stat_result | None,
    protected: list[os.stat_result],
) -> _OwnedLeaf:
    leaf = _open_owned_leaf(directory_fd, name, display)
    try:
        _stabilize_managed_control_leaf(
            leaf,
            anchor=anchor,
            staging=staging,
            initial=initial,
            protected=protected,
        )
    except Exception:
        leaf.close()
        raise
    return leaf


def _stabilize_managed_control_leaf(
    leaf: _OwnedLeaf,
    *,
    anchor: _TargetAnchor,
    staging: _OwnedLeaf,
    initial: os.stat_result | None,
    protected: list[os.stat_result],
    expected: os.stat_result | None = None,
) -> None:
    before = os.fstat(leaf.fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise DownloadFilesError(
            f"aria2 control is not an unaliased regular file: {leaf.display_path}"
        )
    if before.st_uid != os.geteuid():
        raise DownloadFilesError(
            f"aria2 control is not owned by the effective user: {leaf.display_path}"
        )
    if expected is not None and not _same_control_metadata(expected, before):
        raise DownloadFilesError(
            f"aria2 held control changed during transport: {leaf.display_path}"
        )
    if any(_same_inode(before, metadata) for metadata in protected):
        raise DownloadFilesError(
            f"aria2 control aliases protected data: {leaf.display_path}"
        )
    _require_transfer_path_stable(
        anchor,
        staging,
        initial=initial,
        target=_stat_leaf(anchor.parent.fd, anchor.target_name),
    )
    _require_same_leaf(leaf.directory_fd, leaf.name, before)
    try:
        os.fsync(leaf.directory_fd)
    except OSError as error:
        raise DownloadFilesError(
            f"aria2 control generation is not durable: {leaf.display_path}: {error}"
        ) from error
    after = os.fstat(leaf.fd)
    observed = _stat_leaf(leaf.directory_fd, leaf.name)
    if (
        observed is None
        or not _same_control_metadata(before, after)
        or not _same_control_metadata(before, observed)
        or after.st_uid != os.geteuid()
        or observed.st_uid != os.geteuid()
    ):
        raise DownloadFilesError(
            f"aria2 control changed during generation admission: {leaf.display_path}"
        )
    _require_transfer_path_stable(
        anchor,
        staging,
        initial=initial,
        target=_stat_leaf(anchor.parent.fd, anchor.target_name),
    )


def _same_control_metadata(
    expected: os.stat_result,
    observed: os.stat_result,
) -> bool:
    return (
        expected.st_dev,
        expected.st_ino,
        expected.st_mode,
        expected.st_nlink,
        expected.st_uid,
        expected.st_gid,
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    ) == (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_nlink,
        observed.st_uid,
        observed.st_gid,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _require_control_temp_absent(directory_fd: int, temp_name: str) -> None:
    if _stat_leaf(directory_fd, temp_name) is not None:
        raise DownloadFilesError(
            "aria2 temporary control appeared during generation admission"
        )


def _require_transfer_path_stable(
    anchor: _TargetAnchor,
    staging: _OwnedLeaf,
    *,
    initial: os.stat_result | None,
    target: os.stat_result | None,
) -> None:
    anchor.verify_visible()
    _require_owned_leaf_visible(staging)
    current_staging = os.fstat(staging.fd)
    _require_safe_staging_metadata(current_staging, staging.display_path)
    if not _same_optional_stat(initial, target):
        raise DownloadFilesError(
            f"download target changed during transport: {anchor.target}"
        )


def _open_owned_leaf(directory_fd: int, name: str, display: Path) -> _OwnedLeaf:
    flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise DownloadFilesError(
            f"download staging cannot be opened safely: {name}"
        ) from error
    metadata = os.fstat(fd)
    try:
        _require_safe_staging_metadata(metadata, display)
        _require_same_leaf(directory_fd, name, metadata)
    except Exception:
        os.close(fd)
        raise
    return _OwnedLeaf(directory_fd, name, fd, metadata, display)


def _open_regular_leaf(directory_fd: int, name: str, label: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise DownloadFilesError(f"{label} cannot be opened safely: {name}") from error
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(fd)
        raise DownloadFilesError(f"{label} is not a regular file: {name}")
    if metadata.st_nlink != 1:
        os.close(fd)
        raise DownloadFilesError(f"{label} is not an unaliased regular file: {name}")
    return fd


def _cleanup_owned_transfer(
    staging: _OwnedLeaf,
    control: _OwnedLeaf | None,
) -> None:
    if control is not None:
        _unlink_owned_leaf(control)
    _unlink_owned_leaf(staging)
    try:
        os.fsync(staging.directory_fd)
    except OSError as error:
        raise DownloadFilesError(
            f"download staging cleanup could not be made durable: {error}"
        ) from error


def _unlink_owned_leaf(leaf: _OwnedLeaf) -> None:
    _unlink_exact_leaf(
        directory_fd=leaf.directory_fd,
        name=leaf.name,
        expected=os.fstat(leaf.fd),
        display_path=leaf.display_path,
        uncertain_on_mismatch=False,
    )


def _unlink_exact_leaf(
    *,
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    display_path: Path,
    uncertain_on_mismatch: bool,
) -> None:
    quarantine_name = f".{name}.cleanup-{secrets.token_hex(32)}"
    try:
        _renameat2(
            directory_fd,
            name,
            directory_fd,
            quarantine_name,
            flags=1,
        )
    except OSError as error:
        raise DownloadFilesError(
            f"owned download artifact could not be quarantined: {display_path}"
        ) from error
    moved = _stat_leaf(directory_fd, quarantine_name)
    if moved is None:
        raise _PlacementUncertain(
            f"quarantined download artifact identity is uncertain: {display_path}"
        )
    if not _same_inode(expected, moved):
        _restore_or_preserve_quarantined_leaf(
            directory_fd=directory_fd,
            original_name=name,
            quarantine_name=quarantine_name,
            display_path=display_path,
        )
        error_type = (
            _PlacementUncertain if uncertain_on_mismatch else DownloadFilesError
        )
        raise error_type(f"owned download artifact identity changed: {display_path}")
    try:
        os.unlink(quarantine_name, dir_fd=directory_fd)
    except OSError as error:
        raise DownloadFilesError(
            f"owned download artifact could not be cleaned: {display_path}"
        ) from error


def _restore_or_preserve_quarantined_leaf(
    *,
    directory_fd: int,
    original_name: str,
    quarantine_name: str,
    display_path: Path,
) -> None:
    try:
        _renameat2(
            directory_fd,
            quarantine_name,
            directory_fd,
            original_name,
            flags=1,
        )
    except FileExistsError:
        pass
    except OSError as error:
        raise _PlacementUncertain(
            f"foreign download artifact could not be safely restored: {display_path}"
        ) from error
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise _PlacementUncertain(
            f"foreign download artifact preservation is uncertain: {display_path}"
        ) from error


def _require_owned_leaf_visible(leaf: _OwnedLeaf) -> None:
    observed = _stat_leaf(leaf.directory_fd, leaf.name)
    current = os.fstat(leaf.fd)
    if observed is None or not _same_inode(observed, current):
        raise DownloadFilesError(
            f"owned download artifact identity changed: {leaf.display_path}"
        )


def _require_safe_staging_metadata(metadata: os.stat_result, display: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise DownloadFilesError(
            f"download staging is not an unaliased regular file: {display}"
        )


def _require_safe_final_metadata(
    metadata: os.stat_result,
    display: Path,
    *,
    label: str = "download target",
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise DownloadFilesError(f"{label} is not a regular file: {display}")
    if metadata.st_nlink != 1:
        raise DownloadFilesError(f"{label} is not an unaliased regular file: {display}")


def _relative_target(root: Path, target: Path) -> Path:
    if not root.is_absolute() or not target.is_absolute():
        raise DownloadFilesError("download root and target must be absolute paths")
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise DownloadFilesError(
            f"download target escapes COMFYUI_PATH: {target}"
        ) from error
    if not relative.parts or ".." in relative.parts:
        raise DownloadFilesError(
            f"download target must be a strict descendant of COMFYUI_PATH: {target}"
        )
    if is_reserved_file_target_name(relative.name):
        raise DownloadFilesError(
            f"download target uses the reserved staging filename: {target}"
        )
    return relative


def _open_directory_path(path: Path, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as error:
        raise DownloadFilesError(
            f"{label} must be an existing real directory: {path}"
        ) from error


def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    create: bool,
) -> tuple[int, bool]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd), False
    except FileNotFoundError:
        if not create:
            raise _MissingDirectory from None
    except OSError as error:
        raise DownloadFilesError(
            f"download target parent is not a real directory: {name}"
        ) from error
    created = False
    try:
        os.mkdir(name, mode=0o755, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    except OSError as error:
        raise DownloadFilesError(
            f"download target parent cannot be created: {name}"
        ) from error
    try:
        return os.open(name, flags, dir_fd=parent_fd), created
    except OSError as error:
        raise DownloadFilesError(
            f"download target parent is not a real directory: {name}"
        ) from error


def _stat_leaf(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DownloadFilesError(
            f"download path cannot be inspected safely: {name}"
        ) from error


def _require_same_leaf(directory_fd: int, name: str, expected: os.stat_result) -> None:
    observed = _stat_leaf(directory_fd, name)
    if observed is None or not _same_inode(expected, observed):
        raise DownloadFilesError(f"download path changed during operation: {name}")


def _require_same_safe_final_leaf(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    display: Path,
    *,
    label: str = "download target",
) -> None:
    observed = _stat_leaf(directory_fd, name)
    if observed is None or not _same_inode(expected, observed):
        raise DownloadFilesError(f"download path changed during operation: {name}")
    _require_safe_final_metadata(observed, display, label=label)


def _same_optional_stat(
    expected: os.stat_result | None,
    observed: os.stat_result | None,
) -> bool:
    if expected is None or observed is None:
        return expected is observed
    return _same_stat(expected, observed)


def _same_inode(expected: os.stat_result, observed: os.stat_result) -> bool:
    return (expected.st_dev, expected.st_ino) == (observed.st_dev, observed.st_ino)


def _same_stat(expected: os.stat_result, observed: os.stat_result) -> bool:
    return (
        expected.st_dev,
        expected.st_ino,
        expected.st_mode,
        expected.st_nlink,
        expected.st_size,
        expected.st_mtime_ns,
    ) == (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_nlink,
        observed.st_size,
        observed.st_mtime_ns,
    )


def _link_fd_noreplace(source_fd: int, target_fd: int, target_name: str) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        linkat = libc.linkat
    except (OSError, AttributeError) as error:  # pragma: no cover - Ubuntu owns it.
        raise DownloadFilesError("exact staging inode claim is unavailable") from error
    linkat.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    linkat.restype = ctypes.c_int
    result = linkat(
        source_fd,
        b"",
        target_fd,
        os.fsencode(target_name),
        0x1000,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    raise DownloadFilesError(
        f"exact staging inode could not be claimed: {os.strerror(error_number)}"
    )


def _renameat2(
    source_fd: int,
    source_name: str,
    target_fd: int,
    target_name: str,
    *,
    flags: int,
) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError) as error:  # pragma: no cover - Ubuntu owns it.
        raise DownloadFilesError("atomic file placement is unavailable") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_fd,
        os.fsencode(source_name),
        target_fd,
        os.fsencode(target_name),
        flags,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), target_name)
    raise OSError(error_number, os.strerror(error_number), target_name)


class _MissingDirectory(Exception):
    """An admitted target parent is absent without create authority."""


class _ChecksumMismatch(Exception):
    """Internal marker requiring invalid staging cleanup before retry."""
