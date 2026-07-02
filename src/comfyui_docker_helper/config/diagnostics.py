"""Stable diagnostics shared by configuration validation stages."""

from dataclasses import dataclass
from enum import StrEnum

type DiagnosticPathPart = str | int
type DiagnosticPath = tuple[DiagnosticPathPart, ...]


class DiagnosticSeverity(StrEnum):
    """User-facing diagnostic severity."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """An immutable configuration problem with a stable code and location."""

    path: DiagnosticPath
    code: str
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
