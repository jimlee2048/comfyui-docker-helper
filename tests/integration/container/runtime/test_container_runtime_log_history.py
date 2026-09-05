"""Real rotating files and fd capture behind protocol-neutral runtime queries."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from comfyui_docker_helper.config.logs import RuntimeLogSettings
from comfyui_docker_helper.container.runtime.log_query import (
    LogQueryDiagnostic,
    LogReplayComplete,
)
from comfyui_docker_helper.container.runtime.log_storage import (
    LogStorageError,
    LogStorageFailure,
)
from comfyui_docker_helper.container.runtime.logging import (
    RuntimeLogChunk,
    RuntimeLoggingBroker,
)


def _broker(directory: Path, *, mode="file", size=32, files=5):
    broker = RuntimeLoggingBroker()
    broker._started = True
    broker.configure(
        RuntimeLogSettings(
            mode=mode, directory=str(directory), max_size=size, max_files=files
        )
    )
    return broker


def _replay(broker, tail=None):
    with broker.logs(tail=tail) as query:
        items = list(query.replay())
    return b"".join(item for item in items if isinstance(item, bytes)), items


def _settle(broker):
    assert broker._file_writer.wait_for_prefix(
        broker._position, deadline=time.monotonic() + 2
    )


def test_file_memory_overlap_and_reopen_are_raw_and_deduplicated(tmp_path):
    directory = tmp_path / "logs"
    first = _broker(directory, size=16)
    try:
        first._publish(RuntimeLogChunk("stdout", b"old\xff\n"))
        first._publish(RuntimeLogChunk("stderr", b"error\rpartial"))
        _settle(first)
        payload, items = _replay(first)
        assert payload == b"old\xff\nerror\rpartial"
        assert items[-1] == LogReplayComplete(True)
    finally:
        first.close()
    second = _broker(directory, size=16)
    try:
        second._publish(RuntimeLogChunk("stdout", b"next\n"))
        payload, items = _replay(second)
        assert payload == b"old\xff\nerror\rpartialnext\n"
        assert items[-1].complete
    finally:
        second.close()
    assert (
        b"".join(path.read_bytes() for path in sorted(directory.glob("runtime.*.log")))
        + (directory / "runtime.log").read_bytes()
        == b"old\xff\nerror\rpartialnext\n"
    )


def test_none_reads_old_files_without_mutation_and_memory_ignores_them(tmp_path):
    directory = tmp_path / "logs"
    writer = _broker(directory)
    writer._publish(RuntimeLogChunk("stdout", b"old\n"))
    writer.close()
    before = {
        path.name: (path.stat().st_mode, path.read_bytes())
        for path in directory.iterdir()
    }
    reader = _broker(directory, mode="none")
    reader._publish(RuntimeLogChunk("stdout", b"ignored\n"))
    try:
        payload, items = _replay(reader)
        assert payload == b"old\n"
        assert isinstance(items[0], LogQueryDiagnostic)
        assert items[-1].complete
    finally:
        reader.close()
    assert before == {
        path.name: (path.stat().st_mode, path.read_bytes())
        for path in directory.iterdir()
    }
    memory = _broker(directory, mode="memory")
    try:
        memory._publish(RuntimeLogChunk("stdout", b"new\n"))
        assert _replay(memory)[0] == b"new\n"
    finally:
        memory.close()


def test_query_snapshot_file_eviction_is_explicit(tmp_path):
    broker = _broker(tmp_path / "logs", size=4, files=2)
    try:
        broker._publish(RuntimeLogChunk("stdout", b"abcdEFGH"))
        _settle(broker)
        with broker.logs() as query:
            broker._publish(RuntimeLogChunk("stdout", b"ijklMNOP"))
            _settle(broker)
            items = list(query.replay())
            assert items[-1] == LogReplayComplete(False)
    finally:
        broker.close()


def test_pending_file_prefix_can_bridge_ring_without_post_snapshot_eviction(
    tmp_path, monkeypatch
):
    broker = _broker(tmp_path / "logs", size=4, files=10)
    entered, release = threading.Event(), threading.Event()
    append = broker._store.append

    def blocked(start, data):
        entered.set()
        assert release.wait(2)
        append(start, data)

    monkeypatch.setattr(broker._store, "append", blocked)
    try:
        broker._publish(RuntimeLogChunk("stdout", b"abcdefghijkl"))
        assert entered.wait(2)
        with broker.logs() as query:
            release.set()
            _settle(broker)
            items = list(query.replay())
            assert (
                b"".join(item for item in items if isinstance(item, bytes))
                == b"abcdefghijkl"
            )
            assert items[-1].complete
    finally:
        release.set()
        broker.close()


def test_writer_failure_without_more_output_offers_one_warning_and_retains_ring(
    tmp_path, monkeypatch
):
    broker = _broker(tmp_path / "logs")
    noticed = threading.Event()
    broker.set_storage_warning_observer(lambda _reason: noticed.set())

    def fail(_start, _data):
        raise LogStorageError(LogStorageFailure.WRITE)

    monkeypatch.setattr(broker._store, "append", fail)
    try:
        broker._publish(RuntimeLogChunk("stdout", b"retained"))
        assert noticed.wait(2)
        assert _replay(broker)[0] == b"retained"
    finally:
        broker.close()


def test_final_sync_failure_is_pending_outside_writer_lock(tmp_path, monkeypatch):
    broker = _broker(tmp_path / "logs")
    broker._publish(RuntimeLogChunk("stdout", b"written"))
    _settle(broker)

    def fail():
        raise LogStorageError(LogStorageFailure.SYNC)

    monkeypatch.setattr(broker._store, "sync", fail)
    broker.defer_storage_warnings()
    broker.close(deadline=time.monotonic() + 1)
    assert broker.take_final_storage_warning()
    assert not broker.take_final_storage_warning()
    assert broker._file_writer._condition.acquire(blocking=False)
    broker._file_writer._condition.release()


def test_real_fd_history_captures_bootstrap_streams_and_live_handoff(tmp_path):
    code = r"""
import os, sys
from pathlib import Path
from comfyui_docker_helper.config.logs import RuntimeLogSettings
from comfyui_docker_helper.container.runtime.logging import RuntimeLoggingBroker
broker = RuntimeLoggingBroker()
broker.start()
barrier = broker.follow()
os.write(1, b"bootstrap\xff\n")
assert barrier.receive(2).data == b"bootstrap\xff\n"
broker.configure(RuntimeLogSettings(directory=sys.argv[1]))
with broker.logs(follow=True) as query:
    os.write(2, b"stderr\x80\n")
    output = b"".join(item for item in query.replay() if isinstance(item, bytes))
    assert output == b"bootstrap\xff\n"
    assert query.follower.receive(2).data == b"stderr\x80\n"
barrier.close()
broker.close()
stored = (Path(sys.argv[1]) / "runtime.log").read_bytes()
assert stored == b"bootstrap\xff\nstderr\x80\n"
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "logs")],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"bootstrap\xff\n"
    assert result.stderr == b"stderr\x80\n"


def test_initial_runtime_error_is_saved_once_before_environment_admission(tmp_path):
    directory = tmp_path / "logs"
    code = r"""
import sys
from comfyui_docker_helper.container.runtime.serve import run_runtime_serve
raise SystemExit(run_runtime_serve(
    environ={"CDH_LOG_DIRECTORY": sys.argv[1]},
    baked_config_path=sys.argv[2], mounted_config_path=sys.argv[3],
))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(directory),
            str(tmp_path / "no-baked"),
            str(tmp_path / "no-mounted"),
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stderr.count(b"Error:") == 1
    assert (directory / "runtime.log").read_bytes().count(b"Error:") == 1


def test_recent_memory_tail_never_waits_for_unrelated_pending_file_prefix(
    tmp_path, monkeypatch
):
    broker = _broker(tmp_path / "logs", size=16, files=10)
    entered, release = threading.Event(), threading.Event()
    append = broker._store.append

    def blocked(start, data):
        entered.set()
        assert release.wait(2)
        append(start, data)

    def unexpected_wait(*args, **kwargs):
        raise AssertionError("A retained recent memory tail needs no disk barrier")

    monkeypatch.setattr(broker._store, "append", blocked)
    monkeypatch.setattr(broker._file_writer, "wait_for_prefix", unexpected_wait)
    try:
        broker._publish(RuntimeLogChunk("stdout", b"old\n" * 20 + b"a\nb\nc\n"))
        assert entered.wait(2)
        payload, items = _replay(broker, tail=2)
        assert payload == b"b\nc\n"
        assert items[-1].complete
    finally:
        release.set()
        broker.close()


def test_output_during_file_admission_keeps_known_gap_without_publication_lock(
    tmp_path, monkeypatch
):
    from comfyui_docker_helper.container.runtime import logging as logging_module

    directory = tmp_path / "logs"
    previous = _broker(directory)
    previous._publish(RuntimeLogChunk("stdout", b"old\n"))
    previous.close()
    broker = RuntimeLoggingBroker()
    broker._started = True
    real_store = logging_module.RawLogStore

    def admit(*args, **kwargs):
        assert broker._followers_lock.acquire(blocking=False)
        broker._followers_lock.release()
        broker._publish(RuntimeLogChunk("stdout", b"x" * 64))
        return real_store(*args, **kwargs)

    monkeypatch.setattr(logging_module, "RawLogStore", admit)
    broker.configure(RuntimeLogSettings(directory=str(directory), max_size=32))
    try:
        payload, items = _replay(broker)
        assert payload == b"old\n" + b"x" * 32
        assert items[-1] == LogReplayComplete(False)
        assert any(
            isinstance(item, LogQueryDiagnostic) and "queue" in item.message
            for item in items
        )
    finally:
        broker.close()
