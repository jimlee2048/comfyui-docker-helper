"""Real child-process escalation and reap coverage."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from comfyui_docker_helper.container.process_control import (
    start_direct_process,
    terminate_process_group_until,
    wait_for_process_reap,
)


def _wait_for_file(path: Path, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"child readiness marker was not created: {path}")
        time.sleep(0.01)


# Real subprocesses must be waited and reaped so the process owner never leaves
# its direct child as a live or zombie process.
def test_direct_process_normal_exit_is_reaped(tmp_path: Path) -> None:
    process = start_direct_process(
        [sys.executable, "-c", "raise SystemExit(7)"],
        cwd=os.fspath(tmp_path),
        env=dict(os.environ),
        description="test child",
    )
    assert (
        wait_for_process_reap(
            process,
            timeout=2.0,
            poll_interval=0.01,
        )
        is True
    )
    assert process.returncode == 7
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)


# A new-session child that ignores SIGTERM is killed through its owned process
# group and then reaped after one caller-owned absolute cooperative boundary.
def test_ignored_signal_process_group_is_killed_and_reaped(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    script = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({os.fspath(ready)!r}).write_text('ready'); "
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=dict(os.environ),
        start_new_session=True,
    )
    try:
        _wait_for_file(ready)
        result = terminate_process_group_until(
            process,
            deadline=time.monotonic() + 0.1,
            poll_interval=0.01,
        )
        assert result.forced is True
        assert result.terminal.returncode == -int(signal.SIGKILL)
        assert process.returncode == -int(signal.SIGKILL)
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2.0)
