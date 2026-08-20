"""Shared typed Runtime event test seam."""

from __future__ import annotations

from threading import Lock
from typing import Any

from comfyui_docker_helper.cli_output import EventSink
from comfyui_docker_helper.container.runtime_event_delivery import (
    RuntimeBackgroundEventSink,
)
from comfyui_docker_helper.container.runtime_events import RuntimeEvent
from comfyui_docker_helper.container.runtime_serve import run_runtime_generation_once


class RecordingRuntimeEventSink:
    """Record direct and background Runtime facts without owning presentation."""

    def __init__(self) -> None:
        self.events: list[object] = []
        self.progress_events: list[tuple[object, object]] = []
        self.closed_progress_scopes: list[object] = []
        self._lock = Lock()

    def emit(self, event: object, /) -> None:
        with self._lock:
            self.events.append(event)

    def emit_progress(self, scope: object, event: object) -> None:
        with self._lock:
            self.events.append(event)
            self.progress_events.append((scope, event))

    def close_progress(self, scope: object) -> None:
        with self._lock:
            self.closed_progress_scopes.append(scope)


def run_runtime_generation_once_for_test(
    *,
    event_sink: EventSink[RuntimeEvent] | None = None,
    background_event_sink: RuntimeBackgroundEventSink | None = None,
    **kwargs: Any,
) -> int:
    """Run the injected generation seam with explicit typed test sinks."""
    recorder = RecordingRuntimeEventSink()
    return run_runtime_generation_once(
        event_sink=recorder if event_sink is None else event_sink,
        background_event_sink=(
            recorder if background_event_sink is None else background_event_sink
        ),
        **kwargs,
    )
