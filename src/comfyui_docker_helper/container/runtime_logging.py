"""Controller-lifetime primary stdout/stderr tee."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

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
    "client_closed",
    "overflow",
]


class RuntimeLogFollower:
    """One live-only bounded subscriber queue."""

    def __init__(self, broker: RuntimeLoggingBroker) -> None:
        self._broker = broker
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
            if self._queued_bytes + len(chunk.data) > RUNTIME_LOG_FOLLOWER_QUEUE_BYTES:
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
        self._chunks.clear()
        self._queued_bytes = 0
        self._condition.notify_all()


class RuntimeLogFollowerSource(Protocol):
    def follow(self) -> RuntimeLogFollower: ...


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
        self._failure_event = threading.Event()
        self._failure_lock = threading.Lock()
        self._failure: RuntimeLoggingFailure | None = None
        self._followers_lock = threading.Lock()
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
            self._threads = tuple(started_threads)
            self.close()
            raise RuntimeLoggingError(
                "Runtime logging broker could not start its primary drains."
            ) from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_followers()
        if not self._started:
            return
        self._closing.set()
        _flush_language_streams(strict=False)
        for pipe in self._pipes:
            with suppress(OSError):
                os.dup2(pipe.restore_fd, pipe.target_fd, inheritable=True)
            with suppress(OSError):
                os.close(pipe.restore_fd)
        join_deadline = time.monotonic() + RUNTIME_LOG_CLOSE_JOIN_SECONDS
        for thread in self._threads:
            thread.join(timeout=max(0.0, join_deadline - time.monotonic()))

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
            if len(self._followers) >= RUNTIME_LOG_MAX_FOLLOWERS:
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

    def _publish(self, chunk: RuntimeLogChunk) -> None:
        with self._followers_lock:
            followers = tuple(self._followers)
        for follower in followers:
            if not follower._publish(chunk):
                self._remove_follower(follower, reason="overflow")

    def _remove_follower(
        self,
        follower: RuntimeLogFollower,
        *,
        reason: RuntimeLogFollowerCloseReason,
    ) -> None:
        with self._followers_lock:
            self._followers.discard(follower)
        follower._close(reason)

    def _close_followers(self) -> None:
        with self._followers_lock:
            followers = tuple(self._followers)
            self._followers.clear()
        for follower in followers:
            follower._close("broker_closed")


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
