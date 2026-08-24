"""Runtime semantic event fact contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from comfyui_docker_helper.container.runtime.events import (
    RuntimeDownloadFailed,
    RuntimeDownloadReconciled,
    RuntimeGenerationReady,
    RuntimeHookStarted,
    RuntimeSshWarning,
    RuntimeSshWarningKind,
    RuntimeStaleCleanupPending,
)
from comfyui_docker_helper.container.transfer.events import DownloadRetryReason


def test_runtime_events_reject_uncontrolled_dynamic_payloads() -> None:
    with pytest.raises(ValueError, match="controller-owned identity"):
        RuntimeGenerationReady("https://user:secret@example.invalid\n")
    with pytest.raises(ValueError, match="safe hook leaf"):
        RuntimeHookStarted(
            1,
            1,
            "pre-start",
            "mounted",
            "https://user:secret@example.invalid/hook.sh\n",
        )
    with pytest.raises(ValueError, match="controlled value"):
        RuntimeHookStarted(1, 1, "startup", "mounted", "10-hook.sh")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="relative path"):
        RuntimeStaleCleanupPending("../secret")
    with pytest.raises(ValueError, match="must equal desired"):
        RuntimeDownloadReconciled(2, 1, 1, 1, 0, 0)
    with pytest.raises(ValueError, match="must not exceed stale"):
        RuntimeDownloadReconciled(2, 1, 0, 1, 0, 1)
    with pytest.raises(ValueError, match="controlled value"):
        RuntimeDownloadFailed(
            "models/file.bin",
            "sync",
            "ignore",  # type: ignore[arg-type]
            DownloadRetryReason.UNKNOWN,
            1,
            1,
        )
    with pytest.raises(ValueError, match="integer"):
        RuntimeSshWarning(RuntimeSshWarningKind.MONITOR_FAILED, returncode=True)
    with pytest.raises(ValueError, match="unexpected SSH exit"):
        RuntimeSshWarning(RuntimeSshWarningKind.MONITOR_FAILED, returncode=7)


def test_runtime_event_facts_are_immutable() -> None:
    event = RuntimeGenerationReady("gen-3")
    with pytest.raises(FrozenInstanceError):
        event.generation = "gen-4"  # type: ignore[misc]
