"""Shared durable download progress cadence."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum, auto

ACTIVE_PROGRESS_INTERVAL_SECONDS = 10.0
STALLED_PROGRESS_INTERVAL_SECONDS = 30.0


class DownloadCadenceDecision(Enum):
    SUPPRESS = auto()
    ACTIVE = auto()
    STALLED = auto()
    RECOVERED = auto()


class PlainDownloadCadence:
    """Select active, stalled, and recovered snapshots using a fakeable clock."""

    def __init__(self, *, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._transferred_bytes = 0
        self._last_change_at = clock()
        self._last_active_at = self._last_change_at
        self._last_stalled_at: float | None = None
        self._stalled = False

    def observe(self, transferred_bytes: int) -> DownloadCadenceDecision:
        """Observe current transfer bytes and select any immediately due line."""
        now = self._clock()
        if transferred_bytes != self._transferred_bytes:
            self._transferred_bytes = transferred_bytes
            self._last_change_at = now
            if self._stalled:
                self._stalled = False
                self._last_stalled_at = None
                self._last_active_at = now
                return DownloadCadenceDecision.RECOVERED
            if now - self._last_active_at >= ACTIVE_PROGRESS_INTERVAL_SECONDS:
                self._last_active_at = now
                return DownloadCadenceDecision.ACTIVE
            return DownloadCadenceDecision.SUPPRESS
        return self.poll()

    def poll(self) -> DownloadCadenceDecision:
        """Select a due stalled heartbeat without requiring a new byte event."""
        now = self._clock()
        stalled_at = self._last_stalled_at
        if not self._stalled:
            if now - self._last_change_at < STALLED_PROGRESS_INTERVAL_SECONDS:
                return DownloadCadenceDecision.SUPPRESS
            self._stalled = True
            self._last_stalled_at = now
            return DownloadCadenceDecision.STALLED
        if (
            stalled_at is not None
            and now - stalled_at >= STALLED_PROGRESS_INTERVAL_SECONDS
        ):
            self._last_stalled_at = now
            return DownloadCadenceDecision.STALLED
        return DownloadCadenceDecision.SUPPRESS

    def next_poll_delay(self) -> float:
        """Return the delay until the next stall decision can become due."""
        now = self._clock()
        if self._stalled and self._last_stalled_at is not None:
            deadline = self._last_stalled_at + STALLED_PROGRESS_INTERVAL_SECONDS
        else:
            deadline = self._last_change_at + STALLED_PROGRESS_INTERVAL_SECONDS
        return max(0.0, deadline - now)
