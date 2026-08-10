"""Controller-lifetime runtime logging mechanics."""

from __future__ import annotations

import pytest

from comfyui_docker_helper.container import runtime_logging as logging_module
from comfyui_docker_helper.container.runtime_logging import (
    RuntimeLoggingBroker,
    RuntimeLoggingError,
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
