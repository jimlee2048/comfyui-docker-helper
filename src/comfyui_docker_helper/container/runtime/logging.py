"""Controller-lifetime primary stdout/stderr tee."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal

from comfyui_docker_helper.config.logs import RuntimeLogSettings
from comfyui_docker_helper.container.runtime.log_history import (
    LOG_READ_BYTES,
    LogStorageFailure,
    MemoryHistory,
)
from comfyui_docker_helper.container.runtime.log_query import (
    HistorySpan,
    LogQueryDiagnostic,
    LogReplayComplete,
    LogReplayItem,
    MissingHistory,
    history_spans,
    memory_tail,
    replay_history,
)
from comfyui_docker_helper.container.runtime.log_storage import (
    LOG_FILE_QUEUE_BYTES,
    AsyncLogWriter,
    FileSnapshot,
    LogStorageError,
    RawLogStore,
)
from comfyui_docker_helper.errors import ApplicationError

RUNTIME_LOG_READ_CHUNK_BYTES = 16 * 1024
RUNTIME_LOG_CLOSE_JOIN_SECONDS = 0.5
RUNTIME_LOG_FOLLOWER_QUEUE_BYTES = 256 * 1024
RUNTIME_LOG_MAX_FOLLOWERS = 8
RUNTIME_LOGGING_UNAVAILABLE_MESSAGE = (
    "runtime logging failed while preserving primary output"
)
_RUNTIME_LOG_DIAGNOSTIC_MAX_BYTES = 1024

type RuntimeLogStream = Literal["stdout", "stderr"]
type RuntimeLogFailureObserver = Callable[[str], object]
type RuntimeLogWarningObserver = Callable[[LogStorageFailure], object]
type RuntimeLogWriter = Callable[[int, bytes | memoryview], int]
type RuntimeLoggingFactory = Callable[[RuntimeLogFailureObserver], RuntimeLoggingBroker]


class RuntimeLoggingError(ApplicationError):
    """The controller cannot preserve its primary output path."""


class RuntimeLoggingFollowerLimitError(RuntimeError):
    """The fixed live follower capacity is already in use."""


class RuntimeLoggingFailureKind(StrEnum):
    """Controlled primary Runtime logging failure category."""

    DRAIN_FAILED = "drain-failed"
    DRAIN_CLOSED = "drain-closed"
    PRIMARY_OUTPUT_FAILED = "primary-output-failed"


@dataclass(frozen=True, slots=True)
class RuntimeLoggingFailure:
    stream: RuntimeLogStream
    kind: RuntimeLoggingFailureKind

    @property
    def message(self) -> str:
        """Return the fixed user-facing failure message."""
        if self.kind is RuntimeLoggingFailureKind.DRAIN_FAILED:
            return f"Runtime {self.stream} drain failed."
        if self.kind is RuntimeLoggingFailureKind.DRAIN_CLOSED:
            return f"Runtime {self.stream} drain closed unexpectedly."
        return f"Runtime {self.stream} primary output failed."


@dataclass(frozen=True, slots=True)
class RuntimeLogChunk:
    stream: RuntimeLogStream
    data: bytes


type RuntimeLogFollowerCloseReason = Literal[
    "broker_closed",
    "broker_failed",
    "client_closed",
    "overflow",
]


class RuntimeLogFollower:
    """One live-only bounded subscriber queue."""

    def __init__(self, broker: RuntimeLoggingBroker) -> None:
        self._broker = broker
        self._query_owned = False
        self._condition = threading.Condition()
        self._chunks: deque[RuntimeLogChunk] = deque()
        self._queued_bytes = 0
        self._close_reason: RuntimeLogFollowerCloseReason | None = None

    def receive(self, timeout: float | None = None) -> RuntimeLogChunk | None:
        with self._condition:
            available = self._condition.wait_for(
                lambda: bool(self._chunks) or self._close_reason is not None,
                timeout=timeout,
            )
            if not available or not self._chunks:
                return None
            chunk = self._chunks.popleft()
            self._queued_bytes -= len(chunk.data)
            return chunk

    def close(self) -> None:
        self._broker._remove_follower(self, reason="client_closed")

    def close_reason(self) -> RuntimeLogFollowerCloseReason | None:
        with self._condition:
            return self._close_reason

    def _publish(self, chunk: RuntimeLogChunk) -> bool:
        with self._condition:
            if self._close_reason is not None:
                return False
            if (
                self._queued_bytes + len(chunk.data) > RUNTIME_LOG_FOLLOWER_QUEUE_BYTES
                or len(self._chunks) >= 1024
            ):
                self._close_unlocked("overflow")
                return False
            self._chunks.append(chunk)
            self._queued_bytes += len(chunk.data)
            self._condition.notify()
            return True

    def _close(self, reason: RuntimeLogFollowerCloseReason) -> None:
        with self._condition:
            self._close_unlocked(reason)

    def _close_unlocked(self, reason: RuntimeLogFollowerCloseReason) -> None:
        if self._close_reason is not None:
            return
        self._close_reason = reason
        if reason != "broker_closed":
            self._chunks.clear()
            self._queued_bytes = 0
        self._condition.notify_all()


@dataclass(frozen=True, slots=True)
class _RuntimeLogPipe:
    stream: RuntimeLogStream
    target_fd: int
    restore_fd: int
    writer_fd: int
    read_fd: int


class RuntimeLoggingBroker:
    """Tee fd 1/2 to their saved originals for the controller lifetime."""

    def __init__(
        self,
        *,
        failure_observer: RuntimeLogFailureObserver = lambda _message: None,
        writer: RuntimeLogWriter = os.write,
    ) -> None:
        self._failure_observer = failure_observer
        self._writer = writer
        self._closing = threading.Event()
        self._drain_interrupted = threading.Event()
        self._failure_event = threading.Event()
        self._failure_lock = threading.Lock()
        self._failure: RuntimeLoggingFailure | None = None
        self._followers_lock = threading.Lock()
        self._history = MemoryHistory(RUNTIME_LOG_FOLLOWER_QUEUE_BYTES)
        self._position = 0
        self._settings: RuntimeLogSettings | None = None
        self._store: RawLogStore | None = None
        self._file_writer: AsyncLogWriter | None = None
        self._storage_failed: LogStorageFailure | None = None
        self._storage_warning_sent = False
        self._warning_lock = threading.Lock()
        self._final_warning: LogStorageFailure | None = None
        self._warning_observer: RuntimeLogWarningObserver = lambda _reason: None
        self._query_count = 0
        self._followers: set[RuntimeLogFollower] = set()
        self._pipes: tuple[_RuntimeLogPipe, ...] = ()
        self._threads: tuple[threading.Thread, ...] = ()
        self._started = False
        self._closed = False

    def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeLoggingError("Runtime logging broker cannot be started twice.")
        _flush_language_streams(strict=True)
        restore_fds: list[int] = []
        writer_fds: list[int] = []
        read_fds: list[int] = []
        write_fds: list[int] = []
        redirected: list[tuple[int, int]] = []
        try:
            for target_fd in (1, 2):
                restore_fd = os.dup(target_fd)
                restore_fds.append(restore_fd)
                writer_fd = os.dup(target_fd)
                writer_fds.append(writer_fd)
                os.set_inheritable(restore_fd, False)
                os.set_inheritable(writer_fd, False)
                read_fd, write_fd = os.pipe()
                read_fds.append(read_fd)
                write_fds.append(write_fd)
                os.set_inheritable(read_fd, False)
                os.set_inheritable(write_fd, False)
            for target_fd, restore_fd, write_fd in zip(
                (1, 2), restore_fds, write_fds, strict=True
            ):
                os.dup2(write_fd, target_fd, inheritable=True)
                redirected.append((target_fd, restore_fd))
            for write_fd in write_fds:
                os.close(write_fd)
            write_fds.clear()
        except OSError as error:
            for target_fd, saved_fd in redirected:
                with suppress(OSError):
                    os.dup2(saved_fd, target_fd, inheritable=True)
            for fd in (*write_fds, *read_fds, *writer_fds, *restore_fds):
                with suppress(OSError):
                    os.close(fd)
            raise RuntimeLoggingError(
                "Runtime logging broker could not preserve stdout/stderr."
            ) from error

        self._pipes = (
            _RuntimeLogPipe(
                "stdout",
                1,
                restore_fds[0],
                writer_fds[0],
                read_fds[0],
            ),
            _RuntimeLogPipe(
                "stderr",
                2,
                restore_fds[1],
                writer_fds[1],
                read_fds[1],
            ),
        )
        self._threads = tuple(
            threading.Thread(
                target=self._drain,
                args=(pipe,),
                name=f"cdh-runtime-log-{pipe.stream}",
                daemon=True,
            )
            for pipe in self._pipes
        )
        self._started = True
        started_threads: list[threading.Thread] = []
        try:
            for thread in self._threads:
                thread.start()
                started_threads.append(thread)
        except Exception as error:
            for pipe in self._pipes[len(started_threads) :]:
                for fd in (pipe.read_fd, pipe.writer_fd):
                    with suppress(OSError):
                        os.close(fd)
            self._threads = tuple(started_threads)
            self.close()
            raise RuntimeLoggingError(
                "Runtime logging broker could not start its primary drains."
            ) from error

    def close(
        self,
        *,
        deadline: float | None = None,
        force_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if deadline is None:
            deadline = time.monotonic() + RUNTIME_LOG_CLOSE_JOIN_SECONDS
        if not self._started:
            self._close_history(deadline, force_requested)
            self._close_followers()
            return
        self._closing.set()
        _flush_language_streams(strict=False)
        for pipe in self._pipes:
            with suppress(OSError):
                os.dup2(pipe.restore_fd, pipe.target_fd, inheritable=True)
            with suppress(OSError):
                os.close(pipe.restore_fd)
        for thread in self._threads:
            while thread.is_alive() and not force_requested():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(timeout=min(0.02, remaining))
        self._close_history(deadline, force_requested)
        complete = (
            not force_requested()
            and time.monotonic() < deadline
            and not any(thread.is_alive() for thread in self._threads)
            and self.failure() is None
            and not self._drain_interrupted.is_set()
        )
        self._close_followers("broker_closed" if complete else "broker_failed")

    def _close_history(
        self, deadline: float, force_requested: Callable[[], bool]
    ) -> None:
        if self._file_writer is not None:
            self._file_writer.close(deadline=deadline, force_requested=force_requested)
        self._report_storage_failure()

    def failure(self) -> RuntimeLoggingFailure | None:
        with self._failure_lock:
            return self._failure

    def failure_message(self) -> str | None:
        failure = self.failure()
        return None if failure is None else failure.message

    def wait_for_failure(self, timeout: float | None = None) -> bool:
        return self._failure_event.wait(timeout)

    def follow(self) -> RuntimeLogFollower:
        with self._followers_lock:
            if not self._started or self._closed:
                raise RuntimeLoggingError("Runtime logging is not available.")
            if (
                sum(not item._query_owned for item in self._followers)
                + self._query_count
                >= RUNTIME_LOG_MAX_FOLLOWERS
            ):
                raise RuntimeLoggingFollowerLimitError(
                    "The runtime log follower limit has been reached."
                )
            follower = RuntimeLogFollower(self)
            self._followers.add(follower)
            return follower

    def __enter__(self) -> RuntimeLoggingBroker:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _drain(self, pipe: _RuntimeLogPipe) -> None:
        primary_failed = False
        try:
            while True:
                try:
                    chunk = os.read(pipe.read_fd, RUNTIME_LOG_READ_CHUNK_BYTES)
                except InterruptedError:
                    continue
                except OSError:
                    self._drain_interrupted.set()
                    if not self._closing.is_set():
                        self._record_failure(
                            pipe.stream,
                            RuntimeLoggingFailureKind.DRAIN_FAILED,
                        )
                    return
                if not chunk:
                    if not self._closing.is_set():
                        self._record_failure(
                            pipe.stream,
                            RuntimeLoggingFailureKind.DRAIN_CLOSED,
                        )
                    return
                if primary_failed:
                    continue
                try:
                    _write_all(pipe.writer_fd, chunk, writer=self._writer)
                except OSError:
                    self._drain_interrupted.set()
                    primary_failed = True
                    self._record_failure(
                        pipe.stream,
                        RuntimeLoggingFailureKind.PRIMARY_OUTPUT_FAILED,
                    )
                    continue
                self._publish(RuntimeLogChunk(stream=pipe.stream, data=chunk))
        finally:
            with suppress(OSError):
                os.close(pipe.read_fd)
            with suppress(OSError):
                os.close(pipe.writer_fd)

    def _record_failure(
        self,
        stream: RuntimeLogStream,
        kind: RuntimeLoggingFailureKind,
    ) -> None:
        with self._failure_lock:
            if self._failure is not None or self._closing.is_set():
                return
            failure = RuntimeLoggingFailure(stream=stream, kind=kind)
            self._failure = failure
            self._failure_event.set()
        message = failure.message
        self._failure_observer(message)
        self._write_fatal_diagnostic(stream, message)

    def _write_fatal_diagnostic(
        self,
        failed_stream: RuntimeLogStream,
        message: str,
    ) -> None:
        payload = f"cdh: {message}\n".encode("utf-8", errors="replace")
        payload = payload[:_RUNTIME_LOG_DIAGNOSTIC_MAX_BYTES]
        preferred = "stderr" if failed_stream == "stdout" else "stdout"
        candidates = sorted(
            self._pipes,
            key=lambda pipe: pipe.stream != preferred,
        )
        for pipe in candidates:
            try:
                _write_all(pipe.writer_fd, payload)
            except OSError:
                continue
            return

    @property
    def settings(self) -> RuntimeLogSettings | None:
        return self._settings

    def configure(self, settings: RuntimeLogSettings) -> None:
        """Admit once; filesystem work never holds the publication lock."""
        with self._followers_lock:
            if self._settings is not None:
                raise RuntimeLoggingError(
                    "Runtime recording settings are already admitted."
                )
            bootstrap = self._history.snapshot()
            start = max(bootstrap.start, bootstrap.end - settings.max_size)
            history = MemoryHistory(settings.max_size, start=start)
            for position in range(start, bootstrap.end, LOG_READ_BYTES):
                history.append(
                    position,
                    self._history.read(
                        position, min(LOG_READ_BYTES, bootstrap.end - position)
                    ),
                )
            if settings.mode == "none":
                history = MemoryHistory(settings.max_size, start=bootstrap.end)
            self._history = history
            self._settings = settings
        if settings.mode != "file":
            return
        try:
            store = RawLogStore(
                Path(settings.directory),
                max_size=settings.max_size,
                max_files=settings.max_files,
                writable=True,
                start=start,
            )
            writer = AsyncLogWriter(store, failure_observer=self._offer_storage_warning)
        except LogStorageError as error:
            self._storage_failed = error.reason
            self._report_storage_failure()
            return
        with self._followers_lock:
            self._store = store
            self._file_writer = writer
            retained = self._history.snapshot()
            if retained.start > start or retained.end - start > LOG_FILE_QUEUE_BYTES:
                # Admission exceeded the retained bootstrap or the fixed queue.
                # Bound the handoff work even if a fast writer keeps draining.
                self._storage_failed = LogStorageFailure.QUEUE
            else:
                for position in range(start, retained.end, LOG_READ_BYTES):
                    if not writer.enqueue(
                        position,
                        self._history.read(
                            position, min(LOG_READ_BYTES, retained.end - position)
                        ),
                    ):
                        self._storage_failed = LogStorageFailure.QUEUE
                        break
        self._report_storage_failure()

    def logs(self, *, tail: int | None = None, follow: bool = False) -> RuntimeLogQuery:
        """Reserve one bounded query and atomically subscribe after its endpoint."""
        if tail is not None and (type(tail) is not int or tail < 0):
            raise ValueError("Log tail must be a non-negative integer or None.")
        with self._followers_lock:
            if not self._started or self._closed or self._settings is None:
                raise RuntimeLoggingError("Runtime log history is not available.")
            if (
                self._query_count
                + sum(not item._query_owned for item in self._followers)
                >= RUNTIME_LOG_MAX_FOLLOWERS
            ):
                raise RuntimeLoggingFollowerLimitError(
                    "The runtime log query limit has been reached."
                )
            follower = RuntimeLogFollower(self) if follow else None
            if follower is not None:
                follower._query_owned = True
                self._followers.add(follower)
            self._query_count += 1
            retained = self._history.snapshot()
            return RuntimeLogQuery(
                self,
                self._position,
                retained.start,
                tail,
                follower,
                self._store.snapshot() if self._store is not None else None,
            )

    def set_storage_warning_observer(self, observer: RuntimeLogWarningObserver) -> None:
        with self._warning_lock:
            self._warning_observer = observer

    def defer_storage_warnings(self) -> None:
        def defer(reason: LogStorageFailure) -> None:
            self._final_warning = reason

        self.set_storage_warning_observer(defer)

    def take_final_storage_warning(self) -> LogStorageFailure | None:
        with self._warning_lock:
            pending = self._final_warning
            self._final_warning = None
            return pending

    def _offer_storage_warning(self, reason: LogStorageFailure) -> None:
        with self._warning_lock:
            if self._storage_warning_sent:
                return
            self._storage_warning_sent = True
            with suppress(Exception):
                self._warning_observer(reason)

    def _report_storage_failure(self) -> None:
        reason = self._storage_failed
        if reason is None and self._file_writer is not None:
            reason = self._file_writer.failure()
        if reason is not None:
            self._offer_storage_warning(reason)

    def _publish(self, chunk: RuntimeLogChunk) -> None:
        with self._followers_lock:
            start = self._position
            self._position += len(chunk.data)
            if self._settings is None or self._settings.mode != "none":
                self._history.append(start, chunk.data)
            if self._file_writer is not None and not self._storage_failed:
                self._file_writer.enqueue(start, chunk.data)
            for follower in tuple(self._followers):
                if not follower._publish(chunk):
                    self._followers.discard(follower)
        self._report_storage_failure()

    def _remove_follower(
        self,
        follower: RuntimeLogFollower,
        *,
        reason: RuntimeLogFollowerCloseReason,
    ) -> None:
        with self._followers_lock:
            self._followers.discard(follower)
        follower._close(reason)

    def _close_followers(
        self, reason: RuntimeLogFollowerCloseReason = "broker_closed"
    ) -> None:
        with self._followers_lock:
            followers = tuple(self._followers)
            self._followers.clear()
        for follower in followers:
            follower._close(reason)


def open_runtime_logging_broker(
    failure_observer: RuntimeLogFailureObserver,
) -> RuntimeLoggingBroker:
    return RuntimeLoggingBroker(failure_observer=failure_observer)


def _write_all(
    fd: int,
    data: bytes,
    *,
    writer: RuntimeLogWriter = os.write,
) -> None:
    remaining = memoryview(data)
    while remaining:
        try:
            written = writer(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("primary output descriptor made no write progress")
        remaining = remaining[written:]


def _flush_language_streams(*, strict: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError) as error:
            if strict:
                raise RuntimeLoggingError(
                    "Runtime logging broker could not flush stdout/stderr."
                ) from error


class RuntimeLogQuery:
    """One query lease; replay completion is explicit before optional live reads."""

    def __init__(
        self,
        broker: RuntimeLoggingBroker,
        endpoint: int,
        memory_start: int,
        tail: int | None,
        follower: RuntimeLogFollower | None,
        files: FileSnapshot | None,
    ) -> None:
        self._files = files
        self._broker = broker
        self.endpoint = endpoint
        self._memory_start = memory_start
        self._tail = tail
        self.follower = follower
        self._readonly: RawLogStore | None = None
        self._closed = False
        self._replayed = False
        self._degradation_reported = False

    def replay(self) -> Iterator[LogReplayItem]:
        if self._replayed:
            raise RuntimeLoggingError("Runtime log replay cannot be repeated.")
        self._replayed = True
        broker = self._broker
        settings = broker._settings
        assert settings is not None
        diagnostic = self.poll_diagnostic()
        if diagnostic is not None:
            yield diagnostic
        if self._tail == 0:
            yield LogReplayComplete(True)
            return
        memory = broker._history if settings.mode != "none" else None
        store = broker._store
        if settings.mode == "none":
            yield LogQueryDiagnostic(
                "Local log recording is disabled; "
                "only previously retained files are available."
            )
            if self.follower is not None:
                yield LogQueryDiagnostic(
                    "Recording was disabled between retained history "
                    "and this live subscription."
                )
            try:
                self._readonly = RawLogStore(
                    Path(settings.directory),
                    max_size=settings.max_size,
                    max_files=settings.max_files,
                    writable=False,
                )
                store = self._readonly
            except LogStorageError:
                yield LogQueryDiagnostic(
                    "Previously retained log files cannot be read safely.",
                    incomplete=True,
                )
                yield LogReplayComplete(False)
                return
        elif settings.mode == "memory":
            store = None
        # If the snapshot suffix was evicted, a bounded disk catch-up can bridge
        # it. Never wait for persistence while a retained memory copy suffices.
        start = self._memory_start
        if memory is not None:
            start = min(self.endpoint, max(start, memory.snapshot().start))
        if memory is not None and self._tail is not None:
            suffix = memory_tail(HistorySpan(start, self.endpoint, memory), self._tail)
            if suffix is not None:
                yield from replay_history(
                    suffix, endpoint=self.endpoint, tail=None, require_endpoint=True
                )
                return
        if (
            store is not None
            and broker._file_writer is not None
            and store.snapshot().acknowledged < start
        ):
            broker._file_writer.wait_for_prefix(start, deadline=time.monotonic() + 0.1)
        files = self._files
        if files is not None and store is not None:
            # Keep every original artifact identity; only add newly acknowledged
            # current bytes to bridge a ring suffix evicted after H was captured.
            old_end = max((span.end for span in files.spans), default=0)
            additions = []
            for span in store.snapshot().spans:
                if span.end > old_end:
                    additions.append(span)
            additions = [
                replace(
                    span,
                    start=max(span.start, old_end),
                    offset=span.offset + max(0, old_end - span.start),
                )
                for span in additions
            ]
            files = FileSnapshot(files.spans + tuple(additions), files.acknowledged)
        spans = history_spans(
            endpoint=self.endpoint,
            memory=memory,
            memory_start=start,
            store=store,
            files=files,
        )
        expected_start = (
            min(
                (span.start for span in self._files.spans if span.end > span.start),
                default=self._memory_start,
            )
            if self._files is not None
            else self._memory_start
        )
        if (
            memory is not None
            and start > self._memory_start
            and (not spans or spans[0].start > expected_start)
        ):
            end = min(self.endpoint, spans[0].start if spans else self.endpoint)
            if end > expected_start:
                spans = (HistorySpan(expected_start, end, MissingHistory()), *spans)
        for item in replay_history(
            spans,
            endpoint=self.endpoint,
            tail=self._tail,
            require_endpoint=settings.mode != "none",
        ):
            if (
                isinstance(item, LogReplayComplete)
                and not item.complete
                and self.follower is not None
            ):
                self.follower.close()
            yield item

    def poll_diagnostic(self) -> LogQueryDiagnostic | None:
        """Offer a newly observed degradation once, without filesystem access."""
        broker = self._broker
        settings = broker._settings
        if self._degradation_reported or settings is None or settings.mode != "file":
            return None
        reason = broker._storage_failed
        if reason is None and broker._file_writer is not None:
            reason = broker._file_writer.failure()
        if reason is None:
            return None
        self._degradation_reported = True
        return LogQueryDiagnostic(
            f"Local file recording is interrupted ({reason.value}); "
            "new logs are retained only in memory and lost on container restart."
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.follower is not None:
            self.follower.close()
        with self._broker._followers_lock:
            self._broker._query_count -= 1
        if self._readonly is not None:
            self._readonly.close()

    def __enter__(self) -> RuntimeLogQuery:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
