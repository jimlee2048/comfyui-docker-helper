from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_docker_helper.container.runtime.log_history import (
    LogHistoryUnavailableError,
    MemoryHistory,
)
from comfyui_docker_helper.container.runtime.log_storage import (
    AsyncLogWriter,
    LogStorageError,
    LogStorageFailure,
    RawLogStore,
)


def open_store(
    path: Path, *, size: int = 8, count: int = 3, writable: bool = True, **kwargs
) -> RawLogStore:
    return RawLogStore(
        path, max_size=size, max_files=count, writable=writable, **kwargs
    )


def read_all(store: RawLogStore) -> bytes:
    return b"".join(
        store.read(span, span.start, span.end - span.start)
        for span in store.snapshot().spans
    )


def test_raw_rotation_exact_caps_and_reopen_preserve_partial_line(
    tmp_path: Path,
) -> None:
    path = tmp_path / "logs"
    payload = b"\xff\r\x1b[31m\n\n" + b"x" * 10 + b"half"
    store = open_store(path)
    try:
        store.append(0, payload)
        assert store.snapshot().acknowledged == len(payload)
        assert read_all(store) == payload
        files = list(path.glob("*.log"))
        assert len(files) == 3
        assert all(file.stat().st_size <= 8 for file in files)
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert all(
            stat.S_IMODE(file.stat().st_mode) == 0o600 for file in path.iterdir()
        )
        assert not os.get_inheritable(store._marker_fd)
        store.sync()
    finally:
        store.close()
    reopened = open_store(path)
    try:
        assert reopened.snapshot().acknowledged == 0
        assert read_all(reopened) == payload
        reopened.append(0, b"line")
        assert read_all(reopened) == (payload + b"line")[8:]
        assert reopened.snapshot().acknowledged == 4
    finally:
        reopened.close()


def test_current_append_to_old_active_has_correct_offsets(tmp_path: Path) -> None:
    path = tmp_path / "logs"
    store = open_store(path)
    store.append(0, b"old")
    store.close()
    store = open_store(path)
    try:
        store.append(0, b"NEW")
        (span,) = store.snapshot().spans
        assert (span.start, span.end) == (-3, 3)
        assert store.snapshot().acknowledged == 3
        assert store.read(span, 0, 3) == b"NEW"
        assert read_all(store) == b"oldNEW"
    finally:
        store.close()


def test_nonzero_publication_start_preserves_known_gap(tmp_path: Path) -> None:
    path = tmp_path / "logs"
    store = open_store(path)
    store.append(0, b"old")
    store.close()
    store = open_store(path, start=100)
    try:
        store.append(100, b"new")
        assert [(span.start, span.end) for span in store.snapshot().spans] == [
            (-3, 0),
            (100, 103),
        ]
        assert read_all(store) == b"oldnew"
    finally:
        store.close()


def test_count_includes_active_and_eviction_invalidates_snapshot(
    tmp_path: Path,
) -> None:
    store = open_store(tmp_path / "logs", count=1, size=4)
    try:
        store.append(0, b"1234")
        (old,) = store.snapshot().spans
        store.append(4, b"567")
        assert read_all(store) == b"567"
        assert len(list(store.directory.glob("*.log"))) == 1
        with pytest.raises(LogHistoryUnavailableError):
            store.read(old, 0, 1)
    finally:
        store.close()


def test_reader_snapshot_follows_stable_identity_across_rotation(
    tmp_path: Path,
) -> None:
    store = open_store(tmp_path / "logs", size=4)
    try:
        store.append(0, b"1234")
        (span,) = store.snapshot().spans
        store.append(4, b"567")
        assert store.read(span, 0, 4) == b"1234"
    finally:
        store.close()


def test_interrupted_rotation_reopens_archive_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "logs"
    store = open_store(path, size=4)
    store.append(0, b"half")

    def fail_create() -> None:
        raise OSError("injected creation failure")

    monkeypatch.setattr(store, "_create_active", fail_create)
    with pytest.raises(LogStorageError) as error:
        store.append(4, b"next")
    assert error.value.reason is LogStorageFailure.ROTATION
    assert store.snapshot().acknowledged == 4
    assert read_all(store) == b"half"
    store.close()
    reopened = open_store(path, size=4)
    try:
        reopened.append(0, b"next")
        assert read_all(reopened) == b"halfnext"
    finally:
        reopened.close()


def test_reduced_limits_evict_whole_files_without_touching_unrelated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "logs"
    store = open_store(path, size=8, count=4)
    store.append(0, b"a" * 8 + b"b" * 8 + b"tail")
    store.close()
    unrelated = path / "notes.log"
    unrelated.write_bytes(b"keep")
    store = open_store(path, size=4, count=2)
    try:
        assert read_all(store) == b"tail"
        assert unrelated.read_bytes() == b"keep"
        assert len(store.snapshot().spans) <= 2
    finally:
        store.close()


def test_none_absent_and_existing_history_are_strictly_read_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "absent" / "logs"
    reader = open_store(path, writable=False)
    assert reader.snapshot().spans == ()
    reader.close()
    assert not path.parent.exists()
    path = tmp_path / "logs"
    store = open_store(path)
    store.append(0, b"old")
    store.close()
    before = {
        file.name: (file.stat().st_mode, file.stat().st_mtime_ns, file.read_bytes())
        for file in path.iterdir()
    }
    reader = open_store(path, size=1, count=1, writable=False)
    try:
        assert read_all(reader) == b"old"
        with pytest.raises(LogStorageError):
            open_store(path)
    finally:
        reader.close()
    after = {
        file.name: (file.stat().st_mode, file.stat().st_mtime_ns, file.read_bytes())
        for file in path.iterdir()
    }
    assert after == before


def test_second_writer_and_reader_reject_active_writer(tmp_path: Path) -> None:
    store = open_store(tmp_path / "logs")
    try:
        for writable in (True, False):
            with pytest.raises(LogStorageError):
                open_store(store.directory, writable=writable)
        store.append(0, b"safe")
        assert read_all(store) == b"safe"
    finally:
        store.close()


@pytest.mark.parametrize("hazard", ["symlink", "hardlink", "fifo", "mode", "unowned"])
def test_owned_artifact_admission_rejects_unsafe_shapes(
    tmp_path: Path, hazard: str
) -> None:
    path = tmp_path / "logs"
    store = open_store(path)
    store.close()
    active = path / "runtime.log"
    target = tmp_path / "untouched"
    target.write_bytes(b"keep")
    target.chmod(0o600)
    active.unlink()
    if hazard == "symlink":
        active.symlink_to(target)
    elif hazard == "hardlink":
        os.link(target, active)
    elif hazard == "fifo":
        os.mkfifo(active, 0o600)
    elif hazard == "mode":
        active.write_bytes(b"raw")
        active.chmod(0o644)
    else:
        active.write_bytes(b"raw")
        active.chmod(0o600)
        (path / ".cdh-logs.lock").unlink()
    with pytest.raises(LogStorageError):
        open_store(path)
    assert target.read_bytes() == b"keep"


def test_directory_symlink_and_replacement_do_not_redirect_writes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(LogStorageError):
        open_store(link)
    path = tmp_path / "logs"
    store = open_store(path)
    store.append(0, b"safe")
    moved = tmp_path / "moved"
    path.rename(moved)
    path.mkdir(mode=0o700)
    try:
        with pytest.raises(LogStorageError):
            store.append(4, b"unsafe")
        with pytest.raises(LogHistoryUnavailableError):
            read_all(store)
        assert not list(path.iterdir())
        assert (moved / "runtime.log").read_bytes() == b"safe"
    finally:
        store.close()


def test_replaced_artifact_fails_read_and_append(tmp_path: Path) -> None:
    store = open_store(tmp_path / "logs")
    store.append(0, b"safe")
    active = store.directory / "runtime.log"
    active.unlink()
    active.write_bytes(b"other")
    active.chmod(0o600)
    try:
        with pytest.raises(LogHistoryUnavailableError):
            read_all(store)
        with pytest.raises(LogStorageError):
            store.append(4, b"unsafe")
        assert active.read_bytes() == b"other"
    finally:
        store.close()


def test_short_write_prefix_is_acknowledged_before_failure(tmp_path: Path) -> None:
    calls = 0

    def short_then_fail(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return os.write(fd, data[:3])
        raise OSError("injected write failure")

    store = open_store(tmp_path / "logs", writer=short_then_fail)
    try:
        with pytest.raises(LogStorageError):
            store.append(0, b"abcdef")
        assert store.snapshot().acknowledged == 3
        assert read_all(store) == b"abc"
    finally:
        store.close()


def test_async_queue_failure_keeps_independent_ring_and_known_file_prefix(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def held_write(fd: int, data: bytes | memoryview) -> int:
        entered.set()
        assert release.wait(5)
        return os.write(fd, data)

    store = open_store(tmp_path / "logs", writer=held_write)
    writer = AsyncLogWriter(store, queue_bytes=4)
    ring = MemoryHistory(4)
    try:
        ring.append(0, b"abcd")
        assert writer.enqueue(0, b"abcd")
        assert entered.wait(5)
        ring.append(4, b"EFGH")
        assert not writer.enqueue(4, b"EFGH")
        assert writer.failure() is LogStorageFailure.QUEUE
        assert ring.read(4, 4) == b"EFGH"
        release.set()
        writer._thread.join(5)
        assert writer.wait_for_prefix(4, deadline=time.monotonic() + 5)
        assert read_all(store) == b"abcd"
        assert not writer.enqueue(4, b"retry")
    finally:
        release.set()
        writer.close(deadline=time.monotonic() + 5)


def test_expired_close_and_force_do_not_wait_for_stalled_syscall(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def held_write(fd: int, data: bytes | memoryview) -> int:
        entered.set()
        assert release.wait(5)
        return os.write(fd, data)

    store = open_store(tmp_path / "logs", writer=held_write)
    writer = AsyncLogWriter(store)
    assert writer.enqueue(0, b"data")
    assert entered.wait(5)
    try:
        writer.close(deadline=time.monotonic() - 1)
        assert writer._thread.is_alive()
        writer.close(deadline=time.monotonic() + 100, force_requested=lambda: True)
        assert writer._thread.is_alive()
    finally:
        release.set()
        writer._thread.join(5)
        assert not writer._thread.is_alive()


def test_async_sync_failure_is_latched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_store(tmp_path / "logs")

    def fail_sync() -> None:
        raise LogStorageError(LogStorageFailure.SYNC)

    monkeypatch.setattr(store, "sync", fail_sync)
    writer = AsyncLogWriter(store)
    assert writer.enqueue(0, b"raw")
    writer.close(deadline=time.monotonic() + 5)
    assert writer.failure() is LogStorageFailure.SYNC
    assert (store.directory / "runtime.log").read_bytes() == b"raw"


def test_rotation_between_lookup_and_open_retries_only_renamed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_store(tmp_path / "logs", size=4)
    store.append(0, b"1234")
    (span,) = store.snapshot().spans
    original = store._open_file
    once = True

    def rotating_open(name: str, flags: int) -> int:
        nonlocal once
        if once:
            once = False
            store.append(4, b"new")
        return original(name, flags)

    monkeypatch.setattr(store, "_open_file", rotating_open)
    try:
        assert store.read(span, 0, 4) == b"1234"
    finally:
        store.close()


def test_internal_catalog_budget_preserves_recoverable_rotation_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from comfyui_docker_helper.container.runtime import log_storage

    monkeypatch.setattr(log_storage, "LOG_MAX_CATALOG_FILES", 3)
    path = tmp_path / "logs"
    store = open_store(path, size=1, count=10)
    store.append(0, b"abc")
    with pytest.raises(LogStorageError) as error:
        store.append(3, b"d")
    assert error.value.reason is LogStorageFailure.ROTATION
    assert read_all(store) == b"abc"
    store.close()
    store = open_store(path, size=1, count=2)
    try:
        assert read_all(store) == b"c"
        store.append(0, b"d")
        assert read_all(store) == b"cd"
    finally:
        store.close()
    extra = path / "runtime.00000000000000000099.log"
    extra.write_bytes(b"x")
    extra.chmod(0o600)
    another = path / "runtime.00000000000000000100.log"
    another.write_bytes(b"y")
    another.chmod(0o600)
    with pytest.raises(LogStorageError):
        open_store(path, writable=False)
    assert extra.read_bytes() == b"x"
    assert another.read_bytes() == b"y"


def test_async_partial_write_failure_latches_without_retry(tmp_path: Path) -> None:
    calls = 0

    def short_then_fail(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return os.write(fd, data[:2])
        raise OSError("injected write failure")

    store = open_store(tmp_path / "logs", writer=short_then_fail)
    writer = AsyncLogWriter(store)
    try:
        assert writer.enqueue(0, b"abcde")
        writer._thread.join(5)
        assert writer.failure() is LogStorageFailure.WRITE
        assert store.snapshot().acknowledged == 2
        assert read_all(store) == b"ab"
        assert not writer.enqueue(5, b"next")
        assert calls == 2
    finally:
        writer.close(deadline=time.monotonic() + 5)


def test_sync_failure_at_rotation_retains_known_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_store(tmp_path / "logs", size=4)
    store.append(0, b"1234")

    def fail_sync(_fd: int) -> None:
        raise OSError("injected sync failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    try:
        with pytest.raises(LogStorageError) as error:
            store.append(4, b"next")
        assert error.value.reason is LogStorageFailure.SYNC
        assert store.snapshot().acknowledged == 4
        assert read_all(store) == b"1234"
    finally:
        store.close()


def test_read_during_rename_syscall_catalog_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_store(tmp_path / "logs", size=4)
    store.append(0, b"1234")
    (span,) = store.snapshot().spans
    original = os.rename
    observed: list[bytes] = []

    def rename_then_read(source: str, destination: str, **kwargs) -> None:
        original(source, destination, **kwargs)
        # The kernel name changed; _rotate has not yet published the new catalog.
        observed.append(store.read(span, 0, 4))

    monkeypatch.setattr(os, "rename", rename_then_read)
    try:
        store.append(4, b"new")
        assert observed == [b"1234"]
        assert read_all(store) == b"1234new"
    finally:
        store.close()


def test_inode_reuse_cannot_turn_evicted_range_into_new_payload(tmp_path: Path) -> None:
    store = open_store(tmp_path / "logs", size=4, count=1)
    store.append(0, b"old!")
    (old,) = store.snapshot().spans
    store.append(4, b"new!")
    (current,) = store.snapshot().spans
    # Reproduce the metadata observation after a filesystem reuses an inode.
    reused = replace(old, device=current.device, inode=current.inode)
    try:
        with pytest.raises(LogHistoryUnavailableError):
            store.read(reused, 0, 4)
        assert read_all(store) == b"new!"
    finally:
        store.close()
