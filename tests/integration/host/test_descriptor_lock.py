"""Cross-process ownership contract for the selected descriptor lock."""

import multiprocessing
import os
from pathlib import Path
from typing import Protocol

from comfyui_docker_helper.host.descriptor_lock import (
    acquire_descriptor_lock,
    release_descriptor_lock,
)


class _Event(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def _hold_lock(path: str, ready: _Event, release: _Event) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        assert acquire_descriptor_lock(descriptor)
        ready.set()
        assert release.wait(10)
        release_descriptor_lock(descriptor)
    finally:
        os.close(descriptor)


def test_descriptor_lock_contends_across_processes_without_owning_file(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "session.lock"
    lock_path.write_bytes(b"\0")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    child = context.Process(
        target=_hold_lock,
        args=(os.fspath(lock_path), ready, release),
    )
    child.start()
    try:
        assert ready.wait(10)
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            assert acquire_descriptor_lock(descriptor, blocking=False) is False
            release.set()
            child.join(10)
            assert child.exitcode == 0
            assert acquire_descriptor_lock(descriptor, blocking=False) is True
            release_descriptor_lock(descriptor)

            second_descriptor = os.open(lock_path, os.O_RDWR)
            try:
                assert (
                    acquire_descriptor_lock(second_descriptor, blocking=False) is True
                )
                release_descriptor_lock(second_descriptor)
            finally:
                os.close(second_descriptor)

            os.fstat(descriptor)
        finally:
            os.close(descriptor)
    finally:
        release.set()
        if child.is_alive():
            child.terminate()
        child.join(10)

    assert lock_path.read_bytes() == b"\0"
