"""History selection, admission and handoff without a socket or filesystem."""

from __future__ import annotations

import pytest

from comfyui_docker_helper.config.logs import RuntimeLogSettings
from comfyui_docker_helper.container.runtime import logging as logging_module
from comfyui_docker_helper.container.runtime.log_history import (
    LOG_READ_BYTES,
    MemoryHistory,
)
from comfyui_docker_helper.container.runtime.log_query import (
    HistorySpan,
    LogQueryDiagnostic,
    LogReplayComplete,
    MissingHistory,
    replay_history,
)
from comfyui_docker_helper.container.runtime.logging import (
    RUNTIME_LOG_FOLLOWER_QUEUE_BYTES,
    RUNTIME_LOG_MAX_FOLLOWERS,
    RuntimeLogChunk,
    RuntimeLoggingBroker,
    RuntimeLoggingFollowerLimitError,
)


def _span(data: bytes, start: int = 0) -> HistorySpan:
    memory = MemoryHistory(max(1, len(data)), start=start)
    memory.append(start, data)
    return HistorySpan(start, start + len(data), memory)


def _result(items):
    result = list(items)
    return b"".join(item for item in result if isinstance(item, bytes)), result[-1]


@pytest.mark.parametrize(
    ("data", "tail", "expected"),
    [
        (b"", None, b""),
        (b"a\nb\n", 1, b"b\n"),
        (b"a\nb", 1, b"b"),
        (b"a\n\n", 1, b"\n"),
        (b"a\rb\xff\x1b[1m", 1, b"a\rb\xff\x1b[1m"),
        (b"a\nb", 0, b""),
        (b"a\nb", 20, b"a\nb"),
    ],
)
def test_raw_tail_counts_lf_and_final_fragment(data, tail, expected):
    payload, completion = _result(
        replay_history(
            (_span(data),) if data else (),
            endpoint=len(data),
            tail=tail,
            require_endpoint=True,
        )
    )
    assert payload == expected
    assert completion == LogReplayComplete(True)


def test_tail_scans_huge_line_with_bounded_reads_across_segments():
    data = b"old\n" + b"\xff" * (3 * LOG_READ_BYTES) + b"\nlast"
    cut = len(data) // 2
    spans = (_span(data[:cut]), _span(data[cut:], cut))
    payload, completion = _result(
        replay_history(spans, endpoint=len(data), tail=2, require_endpoint=True)
    )
    assert payload == data[4:]
    assert completion.complete


def test_q9_returns_both_sides_but_tail_does_not_expand_to_all():
    old = b"old\n" * 25
    recent = b"new\n" * 100
    spans = (_span(old), _span(recent, 600))
    payload, complete = _result(
        replay_history(spans, endpoint=1000, tail=None, require_endpoint=True)
    )
    assert payload == old + recent
    assert not complete.complete
    payload, complete = _result(
        replay_history(spans, endpoint=1000, tail=2, require_endpoint=True)
    )
    assert payload == b"new\n" * 2
    assert complete.complete
    payload, complete = _result(
        replay_history(spans, endpoint=1000, tail=101, require_endpoint=True)
    )
    assert payload == recent
    assert not complete.complete


def test_tail_read_failure_only_returns_confirmed_recent_suffix():
    spans = (HistorySpan(0, 100, MissingHistory()), _span(b"new\n", 100))
    payload, complete = _result(
        replay_history(spans, endpoint=104, tail=2, require_endpoint=True)
    )
    assert payload == b"new\n"
    assert not complete.complete


def _broker(mode="memory", size=128):
    broker = RuntimeLoggingBroker()
    broker._started = True
    broker.configure(RuntimeLogSettings(mode=mode, max_size=size))
    return broker


def test_history_endpoint_and_subscription_share_publication_boundary():
    broker = _broker()
    broker._publish(RuntimeLogChunk("stderr", b"before\xff"))
    with broker.logs(follow=True) as query:
        broker._publish(RuntimeLogChunk("stdout", b"after\x80"))
        payload, completion = _result(query.replay())
        assert payload == b"before\xff"
        assert completion.complete
        assert query.follower.receive(0).data == b"after\x80"
        assert query.follower.receive(0) is None


def test_snapshot_eviction_is_a_query_failure_but_prior_retention_is_normal():
    broker = _broker(size=4)
    broker._publish(RuntimeLogChunk("stdout", b"1234"))
    with broker.logs() as query:
        broker._publish(RuntimeLogChunk("stdout", b"5678"))
        _, completion = _result(query.replay())
        assert not completion.complete
    with broker.logs() as query:
        payload, completion = _result(query.replay())
        assert payload == b"5678"
        assert completion.complete


def test_overflowed_follow_replay_keeps_query_lease_until_close():
    broker = _broker()
    queries = [broker.logs(follow=True) for _ in range(RUNTIME_LOG_MAX_FOLLOWERS)]
    broker._publish(
        RuntimeLogChunk("stdout", b"x" * (RUNTIME_LOG_FOLLOWER_QUEUE_BYTES + 1))
    )
    with pytest.raises(RuntimeLoggingFollowerLimitError):
        broker.logs()
    for query in queries:
        assert query.follower.close_reason() == "overflow"
        query.close()
    with broker.logs(tail=0) as query:
        assert _result(query.replay())[1].complete


def test_none_discards_bootstrap_and_live_only_never_opens_archive(monkeypatch):
    broker = RuntimeLoggingBroker()
    broker._started = True
    broker._publish(RuntimeLogChunk("stdout", b"bootstrap"))
    broker.configure(RuntimeLogSettings(mode="none"))
    assert broker._history.snapshot().start == broker._history.snapshot().end

    def forbidden(*args, **kwargs):
        pytest.fail("live-only must not inspect the log directory")

    monkeypatch.setattr(logging_module, "RawLogStore", forbidden)
    with broker.logs(tail=0, follow=True) as query:
        assert _result(query.replay()) == (b"", LogReplayComplete(True))
        broker._publish(RuntimeLogChunk("stderr", b"live"))
        assert query.follower.receive(0).data == b"live"


def test_file_admission_failure_retains_memory_and_emits_one_warning(monkeypatch):
    from comfyui_docker_helper.container.runtime.log_storage import (
        LogStorageError,
        LogStorageFailure,
    )

    def unavailable(*args, **kwargs):
        raise LogStorageError(LogStorageFailure.ADMISSION)

    monkeypatch.setattr(logging_module, "RawLogStore", unavailable)
    broker = RuntimeLoggingBroker()
    broker._started = True
    warnings = []
    broker.set_storage_warning_observer(lambda reason: warnings.append(reason))
    broker.configure(RuntimeLogSettings())
    broker._publish(RuntimeLogChunk("stdout", b"retained"))
    with broker.logs() as query:
        items = list(query.replay())
        assert any(isinstance(item, LogQueryDiagnostic) for item in items)
        assert _result(items) == (b"retained", LogReplayComplete(True))
    assert warnings == [LogStorageFailure.ADMISSION]
