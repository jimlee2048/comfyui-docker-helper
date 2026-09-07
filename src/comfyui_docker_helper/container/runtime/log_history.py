"""Bounded raw memory history addressed by controller publication byte offsets."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum

LOG_BLOCK_BYTES = 16 * 1024
LOG_READ_BYTES = 64 * 1024


class LogStorageFailure(StrEnum):
    ADMISSION = "admission"
    WRITE = "write"
    ROTATION = "rotation"
    SYNC = "sync"
    QUEUE = "queue"


class LogHistoryUnavailableError(RuntimeError):
    """A requested retained range is no longer safely readable."""


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int


class MemoryHistory:
    """A sequential tail with directly addressed blocks and metadata-only views."""

    def __init__(self, max_size: int, *, start: int = 0) -> None:
        if max_size <= 0:
            raise ValueError("Memory history capacity must be positive.")
        self._max_size = max_size
        self._start = start
        self._end = start
        self._blocks: dict[int, bytes] = {}
        self._lock = threading.Lock()

    def append(self, start: int, data: bytes) -> None:
        with self._lock:
            if start != self._end:
                raise ValueError("Memory history publications must be sequential.")
            old_start = self._start
            old_end = self._end
            self._end += len(data)
            self._start = max(self._start, self._end - self._max_size)
            if start < self._start:
                data = data[self._start - start :]
                start = self._start
            if self._start >= old_end:
                self._blocks.clear()
            else:
                for key in range(
                    old_start // LOG_BLOCK_BYTES, self._start // LOG_BLOCK_BYTES
                ):
                    self._blocks.pop(key, None)
            first_key = self._start // LOG_BLOCK_BYTES
            if first_key in self._blocks and self._start > old_start:
                previous_start = max(old_start, first_key * LOG_BLOCK_BYTES)
                self._blocks[first_key] = self._blocks[first_key][
                    self._start - previous_start :
                ]
            offset = 0
            while offset < len(data):
                position = start + offset
                key = position // LOG_BLOCK_BYTES
                count = min(
                    len(data) - offset, LOG_BLOCK_BYTES - position % LOG_BLOCK_BYTES
                )
                self._blocks[key] = (
                    self._blocks.get(key, b"") + data[offset : offset + count]
                )
                offset += count

    def snapshot(self) -> ByteRange:
        with self._lock:
            return ByteRange(self._start, self._end)

    def read(self, start: int, size: int) -> bytes:
        if not 0 <= size <= LOG_READ_BYTES:
            raise ValueError("History reads must use bounded buffers.")
        with self._lock:
            if start < self._start or start + size > self._end:
                raise LogHistoryUnavailableError("Memory history has been evicted.")
            parts: list[bytes] = []
            end = start + size
            while start < end:
                key = start // LOG_BLOCK_BYTES
                block_start = max(self._start, key * LOG_BLOCK_BYTES)
                count = min(end - start, LOG_BLOCK_BYTES - start % LOG_BLOCK_BYTES)
                parts.append(
                    self._blocks[key][start - block_start : start - block_start + count]
                )
                start += count
            return b"".join(parts)
