"""Real terminal-boundary coverage for operator-facing host output."""

from __future__ import annotations

import errno
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from rich.text import Text


def _run_in_pty(
    command: list[str],
    *,
    environment: dict[str, str],
) -> tuple[int, str, bool]:
    """Run one subprocess with both output streams attached to a real TTY."""
    import fcntl
    import struct
    import termios

    master_fd, slave_fd = os.openpty()
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 24, 120, 0, 0),
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=slave_fd,
        stderr=slave_fd,
        env=environment,
        close_fds=True,
    )
    os.close(slave_fd)
    chunks: list[bytes] = []
    deadline = time.monotonic() + 10
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            readable, _, _ = select.select((master_fd,), (), (), remaining)
            if not readable:
                timed_out = True
                break
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        if process.poll() is None:
            if timed_out:
                process.kill()
                process.wait(timeout=2)
            else:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        os.close(master_fd)

    assert process.returncode is not None
    output = b"".join(chunks).decode().replace("\r\n", "\n")
    return process.returncode, output, timed_out


@pytest.mark.skipif(
    not hasattr(os, "openpty"), reason="requires a POSIX pseudo-terminal"
)
def test_host_validate_no_color_success_summary_on_real_tty(tmp_path: Path) -> None:
    """Emit a concise, ANSI-free success summary to an actual terminal."""
    config = tmp_path / "config.toml"
    config.write_text(
        """
[compute_platform]
type = "cuda"
[compute_platform.cuda]
version = "13.0.3"
[pytorch]
version = "2.12.1"
[comfyui]
version = "0.11.0"
install_manager = false
"""
    )
    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"
    environment.setdefault("TERM", "xterm-256color")
    returncode, output, timed_out = _run_in_pty(
        [
            sys.executable,
            "-c",
            "from comfyui_docker_helper.cli import app; app()",
            "host",
            "validate",
            "-f",
            str(config),
        ],
        environment=environment,
    )

    assert timed_out is False
    assert returncode == 0
    assert "Configuration valid" in output
    assert "Configuration layers (1, merge order)" in output
    assert f"[1/1] {config}" in output
    assert "\x1b" not in output


@pytest.mark.skipif(
    not hasattr(os, "openpty"), reason="requires a POSIX pseudo-terminal"
)
def test_host_workflow_live_teardown_and_final_result_on_real_tty() -> None:
    """Complete a live Host workflow without hanging or duplicating its result."""
    code = "\n".join(
        (
            "from comfyui_docker_helper.host.events import HostPhase, "
            "HostPhaseCompleted, HostPhaseStarted, HostWorkflowSucceeded",
            "from comfyui_docker_helper.host.presentation import "
            "default_host_presenter",
            "from comfyui_docker_helper.host.render_service import PlanningOptions",
            "presenter = default_host_presenter()",
            "workflow = presenter.workflow('Preparing build context')",
            "workflow.emit(HostPhaseStarted(HostPhase.CONFIGURATION_VALIDATION))",
            "workflow.emit(HostPhaseCompleted(HostPhase.CONFIGURATION_VALIDATION))",
            "workflow.emit(HostPhaseStarted(HostPhase.BUILD_INPUT_RESOLUTION))",
            "workflow.emit(HostPhaseCompleted(HostPhase.BUILD_INPUT_RESOLUTION))",
            "workflow.finish(HostWorkflowSucceeded())",
            "presenter.render_success(",
            "    'context',",
            "    options=PlanningOptions(),",
            "    lock_changed=False,",
            "    workflow_summary=workflow.completed_summary,",
            ")",
        )
    )
    environment = os.environ.copy()
    environment.pop("NO_COLOR", None)
    environment.setdefault("TERM", "xterm-256color")
    returncode, output, timed_out = _run_in_pty(
        [sys.executable, "-c", code],
        environment=environment,
    )

    plain = Text.from_ansi(output).plain
    assert timed_out is False
    assert returncode == 0
    assert "Validating configuration" in plain
    assert "Resolving build inputs" in plain
    assert plain.count("Build context rendered") == 1
    assert plain.count("Context: context") == 1
    assert "Traceback" not in plain
