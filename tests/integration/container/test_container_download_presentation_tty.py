"""Representative real-terminal download Progress lifecycle."""

from __future__ import annotations

import errno
import os
import pty
import re
import subprocess
import sys
from textwrap import dedent


def test_download_progress_activates_and_tears_down_on_a_real_terminal() -> None:
    script = dedent(
        """
        from comfyui_docker_helper.cli_output import CliOutputSettings
        from comfyui_docker_helper.container.download_events import (
            DownloadAttemptStarted,
            DownloadBackendName,
            DownloadBatchCompleted,
            DownloadFinalVerificationCompleted,
            DownloadFinalVerificationStarted,
            DownloadItemCompleted,
            DownloadItemStarted,
            DownloadItemStatus,
            DownloadPlacementCompleted,
            DownloadPlacementStarted,
            DownloadTransferProgress,
            DownloadVerificationCompleted,
            DownloadVerificationStarted,
        )
        from comfyui_docker_helper.container.presentation import (
            default_container_download_invocation,
        )

        with default_container_download_invocation(CliOutputSettings()) as display:
            display.emit(DownloadItemStarted(
                index=1,
                total=1,
                target="models/checkpoints/model.safetensors",
                backend=DownloadBackendName.HTTPX,
                max_attempts=3,
                checksum_expected=True,
            ))
            display.emit(DownloadAttemptStarted(1))
            display.emit(DownloadTransferProgress(1024, 1024, 1024, 512))
            display.emit(DownloadVerificationStarted())
            display.emit(DownloadVerificationCompleted())
            display.emit(DownloadPlacementStarted())
            display.emit(DownloadPlacementCompleted())
            display.emit(DownloadItemCompleted(
                status=DownloadItemStatus.DOWNLOADED,
                observed_bytes=1024,
                checksum_verified=True,
            ))
            display.emit(DownloadFinalVerificationStarted(1, 1))
            display.emit(DownloadFinalVerificationCompleted())
            display.emit(DownloadBatchCompleted(1, 1))
        """
    )
    master_fd, slave_fd = pty.openpty()
    environment = os.environ.copy()
    environment["TERM"] = "xterm-256color"
    environment.pop("NO_COLOR", None)
    environment.pop("TTY_COMPATIBLE", None)
    environment.pop("TTY_INTERACTIVE", None)
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=slave_fd,
            env=environment,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)

    returncode = process.wait(timeout=10)
    output = _read_terminal(master_fd)
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).replace("\r", "")

    assert returncode == 0
    assert "\x1b[" in output
    assert "models/checkpoints/model.safetensors" in plain
    assert plain.count("Downloaded:") == 1
    assert plain.count("Downloads complete:") == 1
    assert "Traceback" not in plain


def _read_terminal(master_fd: int) -> str:
    chunks: list[bytes] = []
    try:
        while True:
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
        os.close(master_fd)
    return b"".join(chunks).decode("utf-8", errors="replace")
