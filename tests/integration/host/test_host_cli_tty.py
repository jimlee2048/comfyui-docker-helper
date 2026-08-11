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


@pytest.mark.skipif(
    not hasattr(os, "openpty"), reason="requires a POSIX pseudo-terminal"
)
def test_host_validate_no_color_success_summary_on_real_tty(tmp_path: Path) -> None:
    """Emit a concise, ANSI-free success summary to an actual terminal."""
    import fcntl
    import struct
    import termios

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
    master_fd, slave_fd = os.openpty()
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 24, 120, 0, 0),
    )
    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"
    environment.setdefault("TERM", "xterm-256color")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from comfyui_docker_helper.cli import app; app()",
            "host",
            "validate",
            "-f",
            str(config),
        ],
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

    assert timed_out is False
    assert process.returncode == 0
    output = b"".join(chunks).decode().replace("\r\n", "\n")
    assert "Configuration valid" in output
    assert f"File: {config}" in output
    assert "\x1b" not in output
