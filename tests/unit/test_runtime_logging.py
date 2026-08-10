"""Controller-lifetime runtime logging mechanics."""

from __future__ import annotations

import threading

import pytest

from comfyui_docker_helper.container import runtime_logging as logging_module
from comfyui_docker_helper.container.runtime_logging import (
    RUNTIME_LOG_FOLLOWER_QUEUE_BYTES,
    RUNTIME_LOG_MAX_FOLLOWERS,
    RuntimeLogChunk,
    RuntimeLoggingBroker,
    RuntimeLoggingError,
    RuntimeLoggingFollowerLimitError,
    _write_all,
)


def test_primary_write_retries_interruption_and_partial_progress() -> None:
    writes: list[bytes] = []
    attempts = 0

    def writer(fd: int, data: bytes | memoryview) -> int:
        nonlocal attempts
        assert fd == 42
        attempts += 1
        if attempts == 1:
            raise InterruptedError
        chunk = bytes(data[:2])
        writes.append(chunk)
        return len(chunk)

    _write_all(42, b"abcdef", writer=writer)

    assert attempts == 4
    assert b"".join(writes) == b"abcdef"


def test_primary_write_rejects_zero_progress() -> None:
    with pytest.raises(OSError, match="no write progress"):
        _write_all(42, b"blocked", writer=lambda _fd, _data: 0)


def test_broker_reports_only_the_first_primary_failure() -> None:
    observed: list[str] = []
    broker = RuntimeLoggingBroker(failure_observer=observed.append)

    broker._record_failure("stdout", "stdout failed")
    broker._record_failure("stderr", "stderr failed")

    failure = broker.failure()
    assert failure is not None
    assert failure.stream == "stdout"
    assert failure.message == "stdout failed"
    assert broker.wait_for_failure(0) is True
    assert observed == ["stdout failed"]


def test_broker_start_rejects_unflushed_language_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedStream:
        def flush(self) -> None:
            raise OSError("synthetic flush failure")

    monkeypatch.setattr(logging_module.sys, "stdout", FailedStream())

    with pytest.raises(RuntimeLoggingError, match="could not flush"):
        RuntimeLoggingBroker().start()


def _active_broker() -> RuntimeLoggingBroker:
    broker = RuntimeLoggingBroker()
    broker._started = True
    return broker


def test_follower_is_live_only_and_preserves_stream_bytes_in_order() -> None:
    broker = _active_broker()
    broker._publish(RuntimeLogChunk("stdout", b"before"))
    follower = broker.follow()

    broker._publish(RuntimeLogChunk("stdout", b"out\x00\xff"))
    broker._publish(RuntimeLogChunk("stderr", b"err-part"))
    broker._publish(RuntimeLogChunk("stdout", b"-tail"))

    assert follower.receive(0) == RuntimeLogChunk("stdout", b"out\x00\xff")
    assert follower.receive(0) == RuntimeLogChunk("stderr", b"err-part")
    assert follower.receive(0) == RuntimeLogChunk("stdout", b"-tail")
    assert follower.receive(0) is None


def test_follower_queue_uses_exact_byte_limit_and_overflow_is_isolated() -> None:
    broker = _active_broker()
    slow = broker.follow()
    healthy = broker.follow()
    chunk = RuntimeLogChunk("stdout", b"x" * 1024)

    for _ in range(RUNTIME_LOG_FOLLOWER_QUEUE_BYTES // len(chunk.data)):
        broker._publish(chunk)
        assert healthy.receive(0) == chunk

    assert slow.close_reason() is None
    broker._publish(RuntimeLogChunk("stdout", b"!"))

    assert slow.close_reason() == "overflow"
    assert slow.receive(0) is None
    assert healthy.receive(0) == RuntimeLogChunk("stdout", b"!")
    assert healthy.close_reason() is None


def test_follower_limit_is_fixed_and_closed_slot_is_reused() -> None:
    broker = _active_broker()
    followers = [broker.follow() for _ in range(RUNTIME_LOG_MAX_FOLLOWERS)]

    with pytest.raises(RuntimeLoggingFollowerLimitError, match="limit"):
        broker.follow()

    followers[0].close()
    replacement = broker.follow()
    assert replacement.close_reason() is None


def test_follower_close_wakes_receiver_and_linearizes_with_publish() -> None:
    broker = _active_broker()
    follower = broker.follow()
    received: list[RuntimeLogChunk | None] = []
    waiting = threading.Thread(target=lambda: received.append(follower.receive(1.0)))
    waiting.start()
    follower.close()
    waiting.join(timeout=1.0)

    assert not waiting.is_alive()
    assert received == [None]
    assert follower.close_reason() == "client_closed"
    assert follower._publish(RuntimeLogChunk("stdout", b"late")) is False
    assert follower.receive(0) is None


def test_broker_close_wakes_followers_and_rejects_new_subscriptions() -> None:
    broker = _active_broker()
    follower = broker.follow()
    received: list[RuntimeLogChunk | None] = []
    waiting = threading.Thread(target=lambda: received.append(follower.receive(1.0)))
    waiting.start()

    broker.close()
    waiting.join(timeout=1.0)

    assert not waiting.is_alive()
    assert received == [None]
    assert follower.close_reason() == "broker_closed"
    with pytest.raises(RuntimeLoggingError, match="not available"):
        broker.follow()


def test_follower_close_and_publish_share_one_linearization_boundary() -> None:
    broker = _active_broker()
    follower = broker.follow()
    start = threading.Barrier(3)
    publish_results: list[bool] = []

    def publish() -> None:
        start.wait()
        publish_results.append(follower._publish(RuntimeLogChunk("stdout", b"race")))

    def close() -> None:
        start.wait()
        follower.close()

    publisher = threading.Thread(target=publish)
    closer = threading.Thread(target=close)
    publisher.start()
    closer.start()
    start.wait()
    publisher.join(timeout=1.0)
    closer.join(timeout=1.0)

    assert not publisher.is_alive()
    assert not closer.is_alive()
    assert publish_results in ([True], [False])
    assert follower.close_reason() == "client_closed"
    assert follower.receive(0) is None
