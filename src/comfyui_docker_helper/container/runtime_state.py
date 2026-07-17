"""Canonical runtime state persistence for container startup coordination."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from comfyui_docker_helper.config.file_checksum import (
    validate_canonical_file_checksum,
)
from comfyui_docker_helper.config.model_base import ConfigModel
from comfyui_docker_helper.config.url_validation import (
    DownloaderName,
    is_http_url,
    is_reserved_file_target_name,
)
from comfyui_docker_helper.config.value_validation import (
    has_control_characters,
    replace_control_characters,
)
from comfyui_docker_helper.container.transfer_core import ResumeAuthority

RUNTIME_STATE_PATH = Path("/var/lib/cdh/runtime/state.json")
RUNTIME_STATE_SCHEMA_VERSION = 1

_DIGEST_KEY_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ERROR_DOWNLOAD_STATUSES = frozenset({"failed", "exhausted", "cleanup_pending"})
_RESUMABLE_DOWNLOAD_STATUSES = frozenset(
    {"pending", "downloading", "failed", "exhausted", "cleanup_pending"}
)

type RuntimeDownloadDigestKey = Annotated[
    str, Field(pattern=_DIGEST_KEY_PATTERN.pattern)
]
type RuntimeDownloadStatus = Literal[
    "pending",
    "downloading",
    "completed",
    "failed",
    "exhausted",
    "cleanup_pending",
]


def runtime_download_desired_identity_digest(
    *,
    source: str,
    target: str,
    checksum: str | None,
    overwrite: bool,
    downloader: DownloaderName,
) -> RuntimeDownloadDigestKey:
    """Return the canonical runtime desired identity, separate from staging."""
    document = {
        "schema_version": 1,
        "checksum": checksum,
        "downloader": downloader,
        "overwrite": overwrite,
        "source": source,
        "target": target,
    }
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class RuntimeStateError(ValueError):
    """Runtime state file cannot be loaded, validated, or persisted."""


class RuntimeResumeState(ConfigModel):
    """Exact inode authority for one quiescent resumable aria2 transfer."""

    staging_device: int = Field(ge=0)
    staging_inode: int = Field(gt=0)
    control_device: int = Field(ge=0)
    control_inode: int = Field(gt=0)

    @classmethod
    def from_authority(cls, authority: ResumeAuthority) -> Self:
        if authority.control_device is None or authority.control_inode is None:
            raise ValueError("runtime resume authority requires aria2 control identity")
        return cls(
            staging_device=authority.staging_device,
            staging_inode=authority.staging_inode,
            control_device=authority.control_device,
            control_inode=authority.control_inode,
        )

    def as_authority(self, identity_digest: str) -> ResumeAuthority:
        return ResumeAuthority(
            identity_digest=identity_digest,
            staging_device=self.staging_device,
            staging_inode=self.staging_inode,
            control_device=self.control_device,
            control_inode=self.control_inode,
        )


class RuntimeDownloadEntry(ConfigModel):
    """Canonical persisted authority for one runtime file desired identity."""

    source: str
    target: str
    checksum: str | None
    overwrite: bool
    downloader: DownloaderName
    download_mode: Literal["sync", "async"]
    status: RuntimeDownloadStatus
    attempts: int = Field(ge=0)
    attempt_run_id: str = Field(min_length=1)
    resume: RuntimeResumeState | None = None
    last_error: str | None = None
    updated_at: datetime

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if not is_http_url(value):
            raise ValueError("source must be an HTTP(S) URL with a host")
        return value

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        if not value:
            raise ValueError("target must be a non-empty relative POSIX path")
        if "\\" in value:
            raise ValueError("target must use POSIX separators")
        if has_control_characters(value):
            raise ValueError("target must not contain control characters")

        raw_parts = value.split("/")
        if any(part in ("", ".", "..") for part in raw_parts):
            raise ValueError("target must not contain empty, dot, or dotdot segments")

        path = PurePosixPath(value)
        if path.is_absolute():
            raise ValueError("target must be a relative POSIX path")
        if is_reserved_file_target_name(path.name):
            raise ValueError("target uses the reserved staging filename")
        return path.as_posix()

    @field_validator("checksum")
    @classmethod
    def _validate_checksum(cls, value: str | None) -> str | None:
        if value is not None:
            validate_canonical_file_checksum(value)
        return value

    @field_validator("attempt_run_id")
    @classmethod
    def _validate_attempt_run_id(cls, value: str) -> str:
        if not value:
            raise ValueError("attempt_run_id must be non-empty")
        return value

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "updated_at")

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> Self:
        if self.resume is not None:
            if self.downloader != "aria2":
                raise ValueError("runtime resume authority requires aria2")
            if self.status not in _RESUMABLE_DOWNLOAD_STATUSES:
                raise ValueError(
                    "runtime resume authority is invalid for this download status"
                )
        if self.status not in _ERROR_DOWNLOAD_STATUSES:
            self.last_error = None
        elif self.last_error is not None:
            self.last_error = summarize_runtime_error(self.last_error)
        return self


class RuntimeDownloadsState(ConfigModel):
    """Persisted runtime file entries keyed by canonical desired identity."""

    entries: dict[RuntimeDownloadDigestKey, RuntimeDownloadEntry] = Field(
        default_factory=dict
    )


class RuntimeState(ConfigModel):
    """Sole canonical runtime-state schema v1."""

    schema_version: Literal[1]
    updated_at: datetime
    run_id: str = Field(min_length=1)
    downloads: RuntimeDownloadsState = Field(default_factory=RuntimeDownloadsState)

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, value: str) -> str:
        if not value:
            raise ValueError("run_id must be non-empty")
        return value

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "updated_at")


@dataclass(slots=True)
class _StateLeaf:
    fd: int
    metadata: os.stat_result


class RuntimeStateStore:
    """Descriptor-anchored reader/writer for one runtime state leaf."""

    def __init__(self, path: Path, parent_fd: int, parent: os.stat_result) -> None:
        self.path = path
        self._parent_fd = parent_fd
        self._parent = parent
        self._leaf: _StateLeaf | None = None
        self._loaded = False

    @classmethod
    def open(cls, path: Path, *, create_parent: bool) -> RuntimeStateStore | None:
        if create_parent:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise RuntimeStateError(
                    f"failed to create runtime state parent: {path}"
                ) from error
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            parent_fd = os.open(path.parent, flags)
        except FileNotFoundError:
            if not create_parent:
                return None
            raise
        except OSError as error:
            raise RuntimeStateError(
                f"runtime state parent cannot be opened safely: {path.parent}"
            ) from error
        parent = os.fstat(parent_fd)
        store = cls(path, parent_fd, parent)
        try:
            store._verify_parent()
        except Exception:
            store.close()
            raise
        return store

    def read(self) -> RuntimeState | None:
        if self._loaded:
            raise RuntimeStateError("runtime state store was already loaded")
        self._verify_parent()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(self.path.name, flags, dir_fd=self._parent_fd)
        except FileNotFoundError:
            self._loaded = True
            return None
        except OSError as error:
            raise RuntimeStateError(
                f"runtime state cannot be opened safely: {self.path}"
            ) from error
        metadata = os.fstat(fd)
        try:
            _require_safe_state_leaf(metadata, self.path)
            payload = _read_fd(fd)
            _require_same_state_leaf(
                self._parent_fd,
                self.path.name,
                fd,
                metadata,
                self.path,
            )
            self._verify_parent()
            state = _parse_runtime_state(payload, self.path)
        except Exception:
            os.close(fd)
            raise
        self._leaf = _StateLeaf(fd=fd, metadata=metadata)
        self._loaded = True
        return state

    def write(self, state: RuntimeState) -> None:
        if not self._loaded:
            raise RuntimeStateError("runtime state store must be loaded before write")
        payload = _runtime_state_json(state).encode("utf-8")
        self._verify_current()
        temp_name = f".{self.path.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
        temp_fd: int | None = None
        temp_metadata: os.stat_result | None = None
        committed = False
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            temp_fd = os.open(temp_name, flags, 0o600, dir_fd=self._parent_fd)
            temp_metadata = os.fstat(temp_fd)
            _require_safe_state_leaf(temp_metadata, self.path.with_name(temp_name))
            _write_fd(temp_fd, payload)
            os.fsync(temp_fd)
            temp_metadata = os.fstat(temp_fd)
            _require_safe_state_leaf(temp_metadata, self.path.with_name(temp_name))
            _require_same_state_leaf(
                self._parent_fd,
                temp_name,
                temp_fd,
                temp_metadata,
                self.path.with_name(temp_name),
            )
            self._verify_parent()
            self._verify_current()
            if self._leaf is None:
                _renameat2(
                    self._parent_fd,
                    temp_name,
                    self._parent_fd,
                    self.path.name,
                    flags=1,
                )
                committed = True
            else:
                _renameat2(
                    self._parent_fd,
                    temp_name,
                    self._parent_fd,
                    self.path.name,
                    flags=2,
                )
                committed = True
                self._verify_exchange_or_rollback(temp_name, temp_metadata)
                _unlink_exact_state_leaf(
                    self._parent_fd,
                    temp_name,
                    self._leaf.metadata,
                    self.path.with_name(temp_name),
                )
            os.fsync(self._parent_fd)
            self._verify_parent()
            new_leaf = _open_state_leaf(
                self._parent_fd,
                self.path.name,
                self.path,
            )
            if not _same_inode(new_leaf.metadata, temp_metadata):
                os.close(new_leaf.fd)
                raise RuntimeStateError(
                    f"runtime state replacement identity is uncertain: {self.path}"
                )
            if self._leaf is not None:
                os.close(self._leaf.fd)
            self._leaf = new_leaf
        except Exception as error:
            if committed:
                with suppress(Exception):
                    self._refresh_committed_leaf(temp_metadata)
            if isinstance(error, RuntimeStateError):
                raise
            raise RuntimeStateError(
                f"failed to write runtime state: {self.path}"
            ) from error
        finally:
            cleanup_metadata = temp_metadata
            if temp_fd is not None:
                current_temp = os.fstat(temp_fd)
                if temp_metadata is None or _same_inode(current_temp, temp_metadata):
                    cleanup_metadata = current_temp
                os.close(temp_fd)
            if cleanup_metadata is not None:
                with suppress(Exception):
                    _unlink_exact_state_leaf(
                        self._parent_fd,
                        temp_name,
                        cleanup_metadata,
                        self.path.with_name(temp_name),
                    )

    def _verify_exchange_or_rollback(
        self,
        temp_name: str,
        temp_metadata: os.stat_result,
    ) -> None:
        assert self._leaf is not None
        current = _stat_leaf(self._parent_fd, self.path.name)
        displaced = _stat_leaf(self._parent_fd, temp_name)
        if (
            current is not None
            and _same_inode(current, temp_metadata)
            and displaced is not None
            and _same_inode(displaced, self._leaf.metadata)
        ):
            return
        try:
            _renameat2(
                self._parent_fd,
                temp_name,
                self._parent_fd,
                self.path.name,
                flags=2,
            )
            os.fsync(self._parent_fd)
        except OSError as error:
            raise RuntimeStateError(
                f"runtime state replacement rollback is uncertain: {self.path}"
            ) from error
        raise RuntimeStateError(
            f"runtime state changed during atomic replacement: {self.path}"
        )

    def _refresh_committed_leaf(self, expected: os.stat_result) -> None:
        current = _open_state_leaf(self._parent_fd, self.path.name, self.path)
        if not _same_inode(current.metadata, expected):
            os.close(current.fd)
            return
        if self._leaf is not None:
            os.close(self._leaf.fd)
        self._leaf = current

    def _verify_current(self) -> None:
        self._verify_parent()
        observed = _stat_leaf(self._parent_fd, self.path.name)
        if self._leaf is None:
            if observed is not None:
                raise RuntimeStateError(
                    f"runtime state appeared during operation: {self.path}"
                )
            return
        current = os.fstat(self._leaf.fd)
        _require_safe_state_leaf(current, self.path)
        if (
            observed is None
            or not _same_inode(observed, self._leaf.metadata)
            or not _same_stat(current, self._leaf.metadata)
        ):
            raise RuntimeStateError(
                f"runtime state changed during operation: {self.path}"
            )

    def _verify_parent(self) -> None:
        try:
            observed = self.path.parent.lstat()
        except OSError as error:
            raise RuntimeStateError(
                f"runtime state parent changed during operation: {self.path.parent}"
            ) from error
        current = os.fstat(self._parent_fd)
        if (
            not stat.S_ISDIR(observed.st_mode)
            or not _same_inode(observed, self._parent)
            or not _same_parent(current, self._parent)
        ):
            raise RuntimeStateError(
                f"runtime state parent changed during operation: {self.path.parent}"
            )

    def close(self) -> None:
        if self._leaf is not None:
            os.close(self._leaf.fd)
            self._leaf = None
        os.close(self._parent_fd)


@contextmanager
def open_runtime_state_store(
    path: Path = RUNTIME_STATE_PATH,
    *,
    create_parent: bool,
) -> Iterator[RuntimeStateStore | None]:
    store = RuntimeStateStore.open(path, create_parent=create_parent)
    try:
        yield store
    finally:
        if store is not None:
            store.close()


def load_runtime_state(path: Path = RUNTIME_STATE_PATH) -> RuntimeState:
    """Load and strictly validate the canonical runtime state file."""
    with open_runtime_state_store(path, create_parent=False) as store:
        if store is None:
            raise FileNotFoundError(path)
        state = store.read()
        if state is None:
            raise FileNotFoundError(path)
        return state


def write_runtime_state(path: Path, state: RuntimeState) -> None:
    """Durably replace runtime state through one descriptor-anchored store."""
    with open_runtime_state_store(path, create_parent=True) as store:
        assert store is not None
        store.read()
        store.write(state)


def prepare_runtime_state_for_start(
    source: Path | RuntimeStateStore = RUNTIME_STATE_PATH,
    *,
    desired_downloads: bool,
    run_id: str,
    now: datetime,
) -> RuntimeState | None:
    """Load state and reset per-start attempt accounting without writing it."""
    _validate_aware_datetime(now, "now")
    if not run_id:
        raise RuntimeStateError("run_id must be non-empty")

    if isinstance(source, RuntimeStateStore):
        state = source.read()
    else:
        try:
            state = load_runtime_state(source)
        except FileNotFoundError:
            state = None
    if state is None:
        if not desired_downloads:
            return None
        return RuntimeState(
            schema_version=RUNTIME_STATE_SCHEMA_VERSION,
            updated_at=now,
            run_id=run_id,
            downloads=RuntimeDownloadsState(),
        )

    entries = {
        digest_key: _entry_for_run(entry, run_id=run_id, now=now)
        for digest_key, entry in state.downloads.entries.items()
    }
    return RuntimeState(
        schema_version=RUNTIME_STATE_SCHEMA_VERSION,
        updated_at=now,
        run_id=run_id,
        downloads=RuntimeDownloadsState(entries=entries),
    )


def summarize_runtime_error(value: object, *, max_length: int = 512) -> str:
    """Return bounded, single-line runtime error text without classifying it."""
    text = "" if value is None else str(value)
    text = replace_control_characters(text)
    text = " ".join(text.split())

    if max_length < 3:
        return text[:max_length]
    if len(text) > max_length:
        return f"{text[: max_length - 3]}..."
    return text


def failed_runtime_download_entry(
    entry: RuntimeDownloadEntry,
    *,
    status: Literal["failed", "exhausted", "cleanup_pending"],
    last_error: object,
    updated_at: datetime,
    resume_authority: ResumeAuthority | None = None,
) -> RuntimeDownloadEntry:
    """Return an error entry with bounded diagnostics and exact resume state."""
    resume = (
        RuntimeResumeState.from_authority(resume_authority)
        if resume_authority is not None
        else None
    )
    return RuntimeDownloadEntry.model_validate(
        {
            **entry.model_dump(),
            "status": status,
            "resume": resume,
            "last_error": summarize_runtime_error(last_error),
            "updated_at": updated_at,
        }
    )


def _runtime_state_json(state: RuntimeState) -> str:
    normalized = RuntimeState.model_validate(state.model_dump())
    data = normalized.model_dump(mode="json")
    return (
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    )


def _entry_for_run(
    entry: RuntimeDownloadEntry, *, run_id: str, now: datetime
) -> RuntimeDownloadEntry:
    if entry.attempt_run_id == run_id:
        return entry
    return entry.model_copy(
        update={"attempts": 0, "attempt_run_id": run_id, "updated_at": now}
    )


def _validate_aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _parse_runtime_state(payload: bytes, path: Path) -> RuntimeState:
    try:
        return RuntimeState.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise RuntimeStateError(
            f"runtime state is invalid; remove {path} and restart"
        ) from error


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_fd(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError("runtime state write made no progress")
        offset += written


def _open_state_leaf(directory_fd: int, name: str, display: Path) -> _StateLeaf:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise RuntimeStateError(
            f"runtime state cannot be opened safely: {display}"
        ) from error
    metadata = os.fstat(fd)
    try:
        _require_safe_state_leaf(metadata, display)
        _require_same_state_leaf(directory_fd, name, fd, metadata, display)
    except Exception:
        os.close(fd)
        raise
    return _StateLeaf(fd=fd, metadata=metadata)


def _require_safe_state_leaf(metadata: os.stat_result, display: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeStateError(f"runtime state is not a regular file: {display}")
    if metadata.st_nlink != 1:
        raise RuntimeStateError(f"runtime state is not an unaliased file: {display}")
    if metadata.st_uid != os.geteuid():
        raise RuntimeStateError(f"runtime state has an unexpected owner: {display}")


def _require_same_state_leaf(
    directory_fd: int,
    name: str,
    fd: int,
    expected: os.stat_result,
    display: Path,
) -> None:
    observed = _stat_leaf(directory_fd, name)
    current = os.fstat(fd)
    if (
        observed is None
        or not _same_inode(observed, expected)
        or not _same_stat(current, expected)
    ):
        raise RuntimeStateError(f"runtime state changed during operation: {display}")


def _unlink_exact_state_leaf(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    display: Path,
) -> None:
    observed = _stat_leaf(directory_fd, name)
    if observed is None:
        return
    quarantine_name = f".{name}.cleanup-{secrets.token_hex(32)}"
    try:
        _renameat2(
            directory_fd,
            name,
            directory_fd,
            quarantine_name,
            flags=1,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeStateError(
            f"runtime state leaf could not be quarantined: {display}"
        ) from error
    quarantined = _stat_leaf(directory_fd, quarantine_name)
    if quarantined is None:
        raise RuntimeStateError(
            f"runtime state quarantined identity is uncertain: {display}"
        )
    if not _same_stat(quarantined, expected):
        _restore_or_preserve_state_leaf(
            directory_fd,
            name,
            quarantine_name,
            display,
        )
        raise RuntimeStateError(f"runtime state temporary changed: {display}")
    try:
        os.unlink(quarantine_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as error:
        with suppress(Exception):
            _restore_or_preserve_state_leaf(
                directory_fd,
                name,
                quarantine_name,
                display,
            )
        raise RuntimeStateError(
            f"runtime state leaf cleanup could not be made durable: {display}"
        ) from error


def _restore_or_preserve_state_leaf(
    directory_fd: int,
    original_name: str,
    quarantine_name: str,
    display: Path,
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
        raise RuntimeStateError(
            f"runtime state foreign leaf could not be preserved: {display}"
        ) from error
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise RuntimeStateError(
            f"runtime state foreign leaf preservation is uncertain: {display}"
        ) from error


def _stat_leaf(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_uid,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_uid,
        right.st_size,
        right.st_mtime_ns,
    )


def _same_parent(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
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
        raise RuntimeStateError(
            "atomic runtime state replacement is unavailable"
        ) from error
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
