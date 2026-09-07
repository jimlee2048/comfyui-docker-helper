"""Private rotating raw files and their optional bounded asynchronous writer.

The active file is runtime.log. Archives use a monotonic, zero-padded sequence
(runtime.00000000000000000001.log); rotation renames only the active file.
Old file bytes have negative offsets ending at zero. Current publications start
at zero, with each physical file represented by one contiguous byte span.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

from comfyui_docker_helper.container.runtime.log_history import (
    LOG_BLOCK_BYTES,
    LOG_READ_BYTES,
    LogHistoryUnavailableError,
    LogStorageFailure,
)

LOG_FILE_QUEUE_BYTES = 256 * 1024
LOG_FILE_QUEUE_ITEMS = 1024
LOG_MAX_CATALOG_FILES = 4096
_ACTIVE = "runtime.log"
_MARKER = ".cdh-logs.lock"
_MAGIC = b"cdh raw log store v1\n"
_ARCHIVE = re.compile(r"runtime\.([0-9]{20})\.log\Z", re.ASCII)


class LogStorageError(RuntimeError):
    def __init__(self, reason: LogStorageFailure) -> None:
        self.reason = reason
        super().__init__(f"Local log storage failed ({reason.value}).")


@dataclass(frozen=True, slots=True)
class FileSpan:
    name: str
    device: int
    inode: int
    start: int
    end: int
    offset: int = 0


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    spans: tuple[FileSpan, ...]
    acknowledged: int


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _check_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise LogStorageError(LogStorageFailure.ADMISSION)


def _open_directory(path: Path, *, create: bool) -> int | None:
    if not path.is_absolute() or path == Path("/"):
        raise LogStorageError(LogStorageFailure.ADMISSION)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(path.parts[1:]):
            if part in (".", ".."):
                raise LogStorageError(LogStorageFailure.ADMISSION)
            final = index == len(path.parts) - 2
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                if not create:
                    return None
                os.mkdir(part, 0o700, dir_fd=fd)
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if info.st_uid not in (0, os.geteuid()):
                raise LogStorageError(LogStorageFailure.ADMISSION)
            if final:
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise LogStorageError(LogStorageFailure.ADMISSION)
            elif info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise LogStorageError(LogStorageFailure.ADMISSION)
        result, fd = fd, -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


class RawLogStore:
    """Single writer file owner; snapshot/read are bounded reader seams.

    File I/O never holds the metadata lock. Readers open one artifact per bounded
    read and resolve its identity in the current catalog, including after rename.
    Read-only admission holds a shared lock, so no new writer can start meanwhile.
    """

    def __init__(
        self,
        directory: Path,
        *,
        max_size: int,
        max_files: int,
        writable: bool,
        start: int = 0,
        writer: Callable[[int, bytes | memoryview], int] = os.write,
    ) -> None:
        if max_size <= 0 or max_files <= 0:
            raise ValueError("File history capacities must be positive.")
        self.directory = directory
        self._max_size = max_size
        self._max_files = max_files
        self._writable = writable
        self._writer = writer
        self._directory_fd = -1
        self._marker_fd = -1
        self._active_fd = -1
        self._directory_identity: tuple[int, int] | None = None
        self._marker_identity: tuple[int, int] | None = None
        self._spans: list[FileSpan] = []
        self._rotating: FileSpan | None = None
        self._acknowledged = start
        self._sequence = 0
        self._lock = threading.Lock()
        self._closed = False
        try:
            self._admit()
        except (OSError, LogStorageError) as error:
            self.close()
            raise LogStorageError(LogStorageFailure.ADMISSION) from error

    def _open_file(self, name: str, flags: int) -> int:
        fd = os.open(
            name,
            flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=self._directory_fd,
        )
        try:
            info = os.fstat(fd)
            _check_file(info)
            if _identity(info) != _identity(
                os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
            ):
                raise LogStorageError(LogStorageFailure.ADMISSION)
            return fd
        except (OSError, LogStorageError):
            os.close(fd)
            raise

    def _check_anchor(self) -> None:
        info = os.stat(self.directory, follow_symlinks=False)
        if _identity(info) != self._directory_identity or not stat.S_ISDIR(
            info.st_mode
        ):
            raise LogStorageError(LogStorageFailure.ADMISSION)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LogStorageError(LogStorageFailure.ADMISSION)
        if self._marker_identity is not None:
            marker = os.stat(_MARKER, dir_fd=self._directory_fd, follow_symlinks=False)
            _check_file(marker)
            if _identity(marker) != self._marker_identity:
                raise LogStorageError(LogStorageFailure.ADMISSION)

    def _admit(self) -> None:
        directory_fd = _open_directory(self.directory, create=self._writable)
        if directory_fd is None:
            return
        self._directory_fd = directory_fd
        self._directory_identity = _identity(os.fstat(directory_fd))
        names = self._owned_names()
        try:
            self._marker_fd = self._open_file(
                _MARKER, os.O_RDWR if self._writable else os.O_RDONLY
            )
        except FileNotFoundError:
            if names:
                raise LogStorageError(LogStorageFailure.ADMISSION) from None
            if not self._writable:
                return
            self._marker_fd = self._open_file(
                _MARKER, os.O_RDWR | os.O_CREAT | os.O_EXCL
            )
            if os.write(self._marker_fd, _MAGIC) != len(_MAGIC):
                raise LogStorageError(LogStorageFailure.ADMISSION) from None
        fcntl.flock(
            self._marker_fd,
            (fcntl.LOCK_EX if self._writable else fcntl.LOCK_SH) | fcntl.LOCK_NB,
        )
        self._marker_identity = _identity(os.fstat(self._marker_fd))
        if os.pread(self._marker_fd, len(_MAGIC) + 1, 0) != _MAGIC:
            raise LogStorageError(LogStorageFailure.ADMISSION)
        self._check_anchor()
        spans: list[FileSpan] = []
        total = 0
        for name in names:
            fd = self._open_file(name, os.O_RDONLY)
            try:
                info = os.fstat(fd)
                spans.append(
                    FileSpan(
                        name, info.st_dev, info.st_ino, total, total + info.st_size
                    )
                )
                total += info.st_size
            finally:
                os.close(fd)
        self._spans = [
            replace(span, start=span.start - total, end=span.end - total)
            for span in spans
        ]
        if self._writable:
            # Smaller newly admitted limits evict whole old files; raw payload is
            # never rewritten, resegmented or truncated to repair a partial line.
            for span in tuple(self._spans):
                if span.end - span.start > self._max_size:
                    self._remove(span)
            while len(self._spans) > self._max_files:
                self._remove(self._spans[0])
            active = next((span for span in self._spans if span.name == _ACTIVE), None)
            if active is None:
                self._create_active()
            else:
                self._active_fd = self._open_file(_ACTIVE, os.O_WRONLY | os.O_APPEND)
                if self._acknowledged:
                    if active.end > active.start:
                        self._rotate()
                    else:
                        self._spans[-1] = replace(
                            active, start=self._acknowledged, end=self._acknowledged
                        )

    def _owned_names(self) -> list[str]:
        archives: list[tuple[int, str]] = []
        active = False
        # Iterate rather than materializing unrelated directory entries.
        with os.scandir(self._directory_fd) as entries:
            for entry in entries:
                match = _ARCHIVE.fullmatch(entry.name)
                if match:
                    sequence = int(match[1])
                    if sequence == 0:
                        raise LogStorageError(LogStorageFailure.ADMISSION)
                    archives.append((sequence, entry.name))
                    if len(archives) > LOG_MAX_CATALOG_FILES:
                        raise LogStorageError(LogStorageFailure.ADMISSION)
                    self._sequence = max(self._sequence, sequence)
                elif entry.name == _ACTIVE:
                    active = True
        if len(archives) + int(active) > LOG_MAX_CATALOG_FILES:
            raise LogStorageError(LogStorageFailure.ADMISSION)
        return [name for _, name in sorted(archives)] + ([_ACTIVE] if active else [])

    def _remove(self, span: FileSpan) -> None:
        self._check_anchor()
        info = os.stat(span.name, dir_fd=self._directory_fd, follow_symlinks=False)
        _check_file(info)
        if _identity(info) != (span.device, span.inode):
            raise LogStorageError(LogStorageFailure.ADMISSION)
        os.unlink(span.name, dir_fd=self._directory_fd)
        with self._lock:
            self._spans.remove(span)

    def _create_active(self) -> None:
        while len(self._spans) >= self._max_files:
            self._remove(self._spans[0])
        self._check_anchor()
        if len(self._spans) >= LOG_MAX_CATALOG_FILES:
            raise LogStorageError(LogStorageFailure.ROTATION)
        self._active_fd = self._open_file(
            _ACTIVE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
        )
        info = os.fstat(self._active_fd)
        with self._lock:
            self._spans.append(
                FileSpan(
                    _ACTIVE,
                    info.st_dev,
                    info.st_ino,
                    self._acknowledged,
                    self._acknowledged,
                )
            )

    def _rotate(self) -> None:
        self._check_anchor()
        active = self._spans[-1]
        info = os.stat(_ACTIVE, dir_fd=self._directory_fd, follow_symlinks=False)
        _check_file(info)
        if _identity(info) != (active.device, active.inode):
            raise LogStorageError(LogStorageFailure.ROTATION)
        self._sequence += 1
        if self._sequence >= 10**20:
            raise LogStorageError(LogStorageFailure.ROTATION)
        name = f"runtime.{self._sequence:020d}.log"
        try:
            os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise LogStorageError(LogStorageFailure.ROTATION)
        try:
            os.fsync(self._active_fd)
        except OSError as error:
            raise LogStorageError(LogStorageFailure.SYNC) from error
        target = replace(active, name=name)
        with self._lock:
            self._rotating = target
        try:
            os.rename(
                _ACTIVE,
                name,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
        except OSError:
            with self._lock:
                self._rotating = None
            raise
        with self._lock:
            self._spans[-1] = target
            self._rotating = None
        os.close(self._active_fd)
        self._active_fd = -1
        self._create_active()

    def append(self, start: int, data: bytes) -> None:
        """Single writer only. A short successful prefix is published immediately."""
        if not self._writable or self._closed:
            raise LogStorageError(LogStorageFailure.WRITE)
        if start != self._acknowledged:
            raise ValueError("File publications must be sequential.")
        remaining = memoryview(data)
        while remaining:
            try:
                self._check_anchor()
                active = self._spans[-1]
                expected_size = active.offset + active.end - active.start
                info = os.fstat(self._active_fd)
                _check_file(info)
                named = os.stat(
                    _ACTIVE, dir_fd=self._directory_fd, follow_symlinks=False
                )
                if (
                    _identity(named) != (active.device, active.inode)
                    or info.st_size != expected_size
                ):
                    raise LogStorageError(LogStorageFailure.WRITE)
                if expected_size == self._max_size:
                    try:
                        self._rotate()
                    except LogStorageError:
                        raise
                    except OSError as error:
                        raise LogStorageError(LogStorageFailure.ROTATION) from error
                    continue
                offered = remaining[: self._max_size - expected_size]
                try:
                    count = self._writer(self._active_fd, offered)
                except InterruptedError:
                    continue
                if count <= 0 or count > len(offered):
                    raise LogStorageError(LogStorageFailure.WRITE)
                with self._lock:
                    self._acknowledged += count
                    self._spans[-1] = replace(active, end=active.end + count)
                remaining = remaining[count:]
            except OSError as error:
                raise LogStorageError(LogStorageFailure.WRITE) from error

    def snapshot(self) -> FileSnapshot:
        with self._lock:
            return FileSnapshot(tuple(self._spans), self._acknowledged)

    def _retained_span(self, span: FileSpan) -> FileSpan:
        with self._lock:
            current = next(
                (
                    item
                    for item in self._spans
                    if (item.device, item.inode) == (span.device, span.inode)
                ),
                None,
            )
        if (
            current is None
            or self._closed
            or current.start > span.start
            or current.end < span.end
        ):
            # A newly created file can reuse an evicted inode. Its later byte
            # range cannot satisfy an earlier controller snapshot.
            raise LogHistoryUnavailableError("File history has been evicted.")
        return current

    def read(self, span: FileSpan, start: int, size: int) -> bytes:
        if (
            not 0 <= size <= LOG_READ_BYTES
            or start < span.start
            or start + size > span.end
        ):
            raise ValueError("File reads must stay within a bounded snapshot range.")
        current = self._retained_span(span)
        try:
            return self._read_file(current, span, start, size)
        except (OSError, LogStorageError, LogHistoryUnavailableError) as error:
            refreshed = self._retained_span(span)
            with self._lock:
                rotating = self._rotating
            if rotating is not None and (rotating.device, rotating.inode) == (
                span.device,
                span.inode,
            ):
                refreshed = rotating
            # An active inode is renamed at most once. Its published transition
            # also covers rename's syscall/catalog-update window without waiting.
            if refreshed.name == current.name:
                raise LogHistoryUnavailableError(
                    "File history is not safely readable."
                ) from error
        try:
            return self._read_file(refreshed, span, start, size)
        except (OSError, LogStorageError, LogHistoryUnavailableError) as error:
            raise LogHistoryUnavailableError(
                "File history is not safely readable."
            ) from error

    def _read_file(
        self, current: FileSpan, span: FileSpan, start: int, size: int
    ) -> bytes:
        self._check_anchor()
        fd = self._open_file(current.name, os.O_RDONLY)
        try:
            if _identity(os.fstat(fd)) != (span.device, span.inode):
                raise LogHistoryUnavailableError("File history has been replaced.")
            data = os.pread(fd, size, span.offset + start - span.start)
            if len(data) != size:
                raise LogHistoryUnavailableError("File history is incomplete.")
            current = self._retained_span(span)
            with self._lock:
                rotating = self._rotating
            if rotating is not None and (rotating.device, rotating.inode) == (
                span.device,
                span.inode,
            ):
                # The opened descriptor already identifies the data. During the
                # rename transition either exact owned name may still identify it.
                try:
                    named = os.stat(
                        current.name, dir_fd=self._directory_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    current = rotating
                else:
                    if _identity(named) != (span.device, span.inode):
                        current = rotating
            self._check_anchor()
            named = os.stat(
                current.name, dir_fd=self._directory_fd, follow_symlinks=False
            )
            _check_file(named)
            if _identity(named) != (span.device, span.inode):
                raise LogHistoryUnavailableError("File history has been replaced.")
            return data
        finally:
            os.close(fd)

    def sync(self) -> None:
        if not self._writable or self._closed:
            return
        try:
            self._check_anchor()
            os.fsync(self._active_fd)
            os.fsync(self._marker_fd)
            os.fsync(self._directory_fd)
        except (OSError, LogStorageError) as error:
            raise LogStorageError(LogStorageFailure.SYNC) from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self._active_fd, self._marker_fd, self._directory_fd):
            if fd >= 0:
                with suppress(OSError):
                    os.close(fd)
        self._active_fd = self._marker_fd = self._directory_fd = -1


@dataclass(frozen=True, slots=True)
class _QueuedBytes:
    start: int
    data: bytes


class AsyncLogWriter:
    """Independent bounded queue; the first failure disables further recording."""

    def __init__(
        self,
        store: RawLogStore,
        *,
        queue_bytes: int = LOG_FILE_QUEUE_BYTES,
        failure_observer: Callable[[LogStorageFailure], object] = lambda _reason: None,
    ) -> None:
        if queue_bytes <= 0:
            raise ValueError("File queue capacity must be positive.")
        self.store = store
        self._failure_observer = failure_observer
        self._limit = queue_bytes
        self._queue: deque[_QueuedBytes] = deque()
        self._queued_bytes = 0
        self._next = store.snapshot().acknowledged
        self._condition = threading.Condition()
        self._failure: LogStorageFailure | None = None
        self._closing = False
        self._abort = False
        self._thread = threading.Thread(
            target=self._run, name="cdh-runtime-log-writer", daemon=True
        )
        try:
            self._thread.start()
        except RuntimeError as error:
            store.close()
            raise LogStorageError(LogStorageFailure.ADMISSION) from error

    def enqueue(self, start: int, data: bytes) -> bool:
        with self._condition:
            if self._failure is not None or self._closing:
                return False
            if start != self._next:
                raise ValueError("Queued file publications must be sequential.")
            if not data:
                return True
            blocks = (len(data) + LOG_BLOCK_BYTES - 1) // LOG_BLOCK_BYTES
            if (
                self._queued_bytes + len(data) > self._limit
                or len(self._queue) + blocks > LOG_FILE_QUEUE_ITEMS
            ):
                self._fail_locked(LogStorageFailure.QUEUE)
                return False
            self._next += len(data)
            self._queued_bytes += len(data)
            if self._queue and len(self._queue[-1].data) < LOG_BLOCK_BYTES:
                last = self._queue.pop()
                count = min(LOG_BLOCK_BYTES - len(last.data), len(data))
                self._queue.append(_QueuedBytes(last.start, last.data + data[:count]))
                start += count
                data = data[count:]
            for offset in range(0, len(data), LOG_BLOCK_BYTES):
                self._queue.append(
                    _QueuedBytes(
                        start + offset, data[offset : offset + LOG_BLOCK_BYTES]
                    )
                )
            self._condition.notify_all()
            return True

    def failure(self) -> LogStorageFailure | None:
        with self._condition:
            return self._failure

    def _fail_locked(self, reason: LogStorageFailure) -> None:
        first = self._failure is None
        if first:
            self._failure = reason
        self._queue.clear()
        self._condition.notify_all()
        if first:
            # Only a bounded event offer: observers must never wait for output,
            # call this writer, or acquire the broker publication lock.
            # Optional diagnostic delivery cannot become a producer failure.
            with suppress(Exception):
                self._failure_observer(reason)

    def wait_for_prefix(self, end: int, *, deadline: float) -> bool:
        with self._condition:
            while self.store.snapshot().acknowledged < end:
                if self._failure is not None or not self._thread.is_alive():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.02))
            return True

    def close(
        self, *, deadline: float, force_requested: Callable[[], bool] = lambda: False
    ) -> None:
        """Drain/sync only within a caller-owned absolute deadline; never reset it."""
        abort = deadline <= time.monotonic() or force_requested()
        with self._condition:
            self._closing = True
            self._abort = self._abort or abort
            if self._abort:
                self._queue.clear()
            self._condition.notify_all()
        while self._thread.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or force_requested():
                with self._condition:
                    self._abort = True
                    self._queue.clear()
                    self._condition.notify_all()
                return
            self._thread.join(min(remaining, 0.02))
        self.store.close()

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: (
                            bool(self._queue)
                            or self._closing
                            or self._failure is not None
                        )
                    )
                    if self._abort or self._failure is not None:
                        return
                    if not self._queue:
                        break
                    item = self._queue.popleft()
                try:
                    self.store.append(item.start, item.data)
                except LogStorageError as error:
                    with self._condition:
                        self._fail_locked(error.reason)
                    return
                with self._condition:
                    self._queued_bytes -= len(item.data)
                    self._condition.notify_all()
            with self._condition:
                if self._abort:
                    return
            try:
                self.store.sync()
            except LogStorageError as error:
                with self._condition:
                    self._fail_locked(error.reason)
        finally:
            with self._condition:
                closing = self._closing
                self._condition.notify_all()
            if closing:
                self.store.close()
