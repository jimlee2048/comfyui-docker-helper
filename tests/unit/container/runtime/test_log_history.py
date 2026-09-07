from __future__ import annotations

import pytest

from comfyui_docker_helper.container.runtime.log_history import (
    LOG_BLOCK_BYTES,
    LOG_READ_BYTES,
    ByteRange,
    LogHistoryUnavailableError,
    MemoryHistory,
)


@pytest.mark.parametrize("capacity", [1, 7, LOG_BLOCK_BYTES + 3])
def test_ring_keeps_exact_raw_tail_with_no_readers(capacity: int) -> None:
    ring = MemoryHistory(capacity, start=3)
    original = b""
    for chunk in (b"\xff\r\x1b[31m", b"\n\n", b"x" * (LOG_BLOCK_BYTES * 2), b"tail"):
        ring.append(3 + len(original), chunk)
        original += chunk
        expected = original[-capacity:]
        snapshot = ring.snapshot()
        assert snapshot == ByteRange(
            3 + len(original) - len(expected), 3 + len(original)
        )
        assert ring.read(snapshot.start, len(expected)) == expected
    with pytest.raises(LogHistoryUnavailableError):
        ring.read(ring.snapshot().start - 1, 1)


def test_snapshots_do_not_pin_evicted_data() -> None:
    ring = MemoryHistory(4)
    ring.append(0, b"abcd")
    view = ring.snapshot()
    ring.append(4, b"efgh")
    with pytest.raises(LogHistoryUnavailableError):
        ring.read(view.start, view.end - view.start)
    assert ring.read(4, 4) == b"efgh"


def test_tiny_publications_have_bounded_block_metadata() -> None:
    ring = MemoryHistory(LOG_BLOCK_BYTES + 1)
    for index in range(LOG_BLOCK_BYTES * 2 + 3):
        ring.append(index, b"x")
    assert len(ring._blocks) <= 3
    assert sum(map(len, ring._blocks.values())) == LOG_BLOCK_BYTES + 1
    assert max(map(len, ring._blocks.values())) <= LOG_BLOCK_BYTES


def test_bounded_read_directly_addresses_blocks() -> None:
    class CountedBlocks(dict[int, bytes]):
        reads = 0

        def __getitem__(self, key: int) -> bytes:
            self.reads += 1
            return super().__getitem__(key)

        def __iter__(self):
            pytest.fail("A bounded read must not scan the whole history.")

    ring = MemoryHistory(LOG_BLOCK_BYTES * 100)
    ring.append(0, b"x" * (LOG_BLOCK_BYTES * 100))
    ring._blocks = CountedBlocks(ring._blocks)
    assert ring.read(LOG_BLOCK_BYTES * 90 + 1, LOG_READ_BYTES) == b"x" * LOG_READ_BYTES
    assert ring._blocks.reads == 5


def test_reads_and_publications_reject_invalid_ranges() -> None:
    ring = MemoryHistory(4)
    assert ring.read(0, 0) == b""
    with pytest.raises(ValueError):
        ring.append(1, b"x")
    with pytest.raises(ValueError):
        ring.read(0, LOG_READ_BYTES + 1)
    with pytest.raises(ValueError):
        MemoryHistory(0)
