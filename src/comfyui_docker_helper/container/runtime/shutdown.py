"""The lifecycle-owned deadline projected to final controller cleanup."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeShutdownDeadline:
    generation: str | None
    deadline: float | None
