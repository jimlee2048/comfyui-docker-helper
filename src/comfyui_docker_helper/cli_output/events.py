"""Minimal dependency-injected event boundary."""

from typing import Protocol, TypeVar

EventT_contra = TypeVar("EventT_contra", contravariant=True)


class EventSink(Protocol[EventT_contra]):
    """Consume one immutable domain event without owning its presentation."""

    def emit(self, event: EventT_contra, /) -> None:
        """Accept one event."""
