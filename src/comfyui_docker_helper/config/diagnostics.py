"""Stable diagnostics shared by configuration validation stages."""

from dataclasses import dataclass

type DiagnosticPathPart = str | int
type DiagnosticPath = tuple[DiagnosticPathPart, ...]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """An immutable configuration problem with a stable code and location."""

    path: DiagnosticPath
    code: str
    message: str
