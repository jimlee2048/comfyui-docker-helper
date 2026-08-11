"""Stable diagnostics shared by configuration validation stages."""

from dataclasses import dataclass, field
from enum import StrEnum

type DiagnosticPathPart = str | int
type DiagnosticPath = tuple[DiagnosticPathPart, ...]


@dataclass(frozen=True, slots=True)
class SourceReference:
    """One ordered configuration input with a user-supplied display label."""

    layer_ordinal: int
    label: str = field(compare=False)


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """An authored semantic path within one configuration input."""

    source: SourceReference
    path: DiagnosticPath


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


class DiagnosticError(Exception):
    """Base for expected failures represented by stable diagnostics."""

    def __init__(
        self,
        diagnostics: tuple[Diagnostic, ...],
        *,
        exit_code: int = 1,
    ) -> None:
        if not diagnostics:
            raise ValueError("diagnostic errors require at least one diagnostic")
        if exit_code <= 0:
            raise ValueError("diagnostic error exit codes must be positive")
        self.diagnostics = diagnostics
        self.exit_code = exit_code
        super().__init__("operation failed")
