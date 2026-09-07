"""Protocol-neutral, bounded raw history replay with explicit completeness."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise
from typing import Protocol

from comfyui_docker_helper.container.runtime.log_history import (
    LOG_READ_BYTES,
    LogHistoryUnavailableError,
    MemoryHistory,
)
from comfyui_docker_helper.container.runtime.log_storage import (
    FileSnapshot,
    FileSpan,
    RawLogStore,
)


class HistoryReader(Protocol):
    def read(self, start: int, size: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _FileReader:
    store: RawLogStore
    span: FileSpan

    def read(self, start: int, size: int) -> bytes:
        return self.store.read(self.span, start, size)


@dataclass(frozen=True, slots=True)
class HistorySpan:
    start: int
    end: int
    reader: HistoryReader


class MissingHistory:
    def read(self, start: int, size: int) -> bytes:
        raise LogHistoryUnavailableError("A captured history range was evicted.")


@dataclass(frozen=True, slots=True)
class LogQueryDiagnostic:
    message: str
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class LogReplayComplete:
    complete: bool


type LogReplayItem = bytes | LogQueryDiagnostic | LogReplayComplete


def history_spans(
    *,
    endpoint: int,
    memory: MemoryHistory | None,
    memory_start: int,
    store: RawLogStore | None,
    files: FileSnapshot | None = None,
) -> tuple[HistorySpan, ...]:
    """Prefer retained memory for the current suffix; never read after H."""
    result: list[HistorySpan] = []
    memory_end = endpoint if memory is not None else memory_start
    if store is not None:
        for span in (store.snapshot() if files is None else files).spans:
            end = min(span.end, endpoint)
            if memory is not None:
                end = min(end, memory_start)
            if end > span.start:
                result.append(HistorySpan(span.start, end, _FileReader(store, span)))
    if memory is not None and memory_end > memory_start:
        result.append(HistorySpan(memory_start, memory_end, memory))
    return tuple(result)


def memory_tail(span: HistorySpan, count: int) -> tuple[HistorySpan, ...] | None:
    """Return a proven recent suffix without consulting older file history."""
    if span.start == span.end:
        return None
    selected, incomplete = _select_tail((span,), count)
    if not incomplete and selected and selected[0].start > span.start:
        return selected
    return None


def replay_history(
    spans: tuple[HistorySpan, ...],
    *,
    endpoint: int,
    tail: int | None,
    require_endpoint: bool,
) -> Iterator[LogReplayItem]:
    """Read fixed buffers, retaining only span metadata even for huge lines."""
    if tail == 0:
        yield LogReplayComplete(True)
        return
    incomplete = False
    selected = spans
    if tail is not None and selected:
        selected, incomplete = _select_tail(selected, tail)
    if any(left.end != right.start for left, right in pairwise(selected)):
        incomplete = True
    if require_endpoint and (
        (selected and selected[-1].end != endpoint) or (not selected and endpoint > 0)
    ):
        incomplete = True
    for span in selected:
        position = span.start
        try:
            while position < span.end:
                data = span.reader.read(
                    position, min(LOG_READ_BYTES, span.end - position)
                )
                yield data
                position += len(data)
        except LogHistoryUnavailableError:
            incomplete = True
            # Continue other available spans, including the far side of a gap.
    if incomplete:
        yield LogQueryDiagnostic(
            "Requested log history is incomplete; available ranges were returned.",
            incomplete=True,
        )
    yield LogReplayComplete(not incomplete)


def _select_tail(
    spans: tuple[HistorySpan, ...], count: int
) -> tuple[tuple[HistorySpan, ...], bool]:
    remaining = count
    first_byte = True
    next_start: int | None = None
    for index in range(len(spans) - 1, -1, -1):
        span = spans[index]
        if next_start is not None and span.end != next_start:
            # The missing interval may contain arbitrarily many LF separators.
            return spans[index + 1 :], True
        next_start = span.start
        end = span.end
        while end > span.start:
            start = max(span.start, end - LOG_READ_BYTES)
            try:
                data = span.reader.read(start, end - start)
            except LogHistoryUnavailableError:
                suffix = (
                    (HistorySpan(end, span.end, span.reader),) if end < span.end else ()
                )
                return suffix + spans[index + 1 :], True
            cursor = len(data)
            if first_byte:
                if data[-1:] == b"\n":
                    remaining += 1
                first_byte = False
            while remaining:
                found = data.rfind(b"\n", 0, cursor)
                if found < 0:
                    break
                remaining -= 1
                cursor = found
                if remaining == 0:
                    begin = start + found + 1
                    selected = (
                        (HistorySpan(begin, span.end, span.reader),)
                        if begin < span.end
                        else ()
                    )
                    return selected + spans[index + 1 :], False
            end = start
    return spans, False
