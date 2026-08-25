"""Runtime diagnostic formatting for container logs and errors."""

from __future__ import annotations

import sys

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticComparison,
    DiagnosticComparisonSite,
    SourceLocation,
)


def format_runtime_diagnostics(
    header: str,
    diagnostics: tuple[Diagnostic, ...],
) -> str:
    """Format ordered diagnostics for one fatal runtime error."""
    lines = [_escape_terminal_text(header)]
    for diagnostic in diagnostics:
        lines.extend(_format_diagnostic_lines(diagnostic, include_severity=False))
    return "\n".join(lines)


def render_runtime_diagnostics(
    header: str,
    diagnostics: tuple[Diagnostic, ...],
) -> None:
    """Render ordered nonfatal diagnostics to stderr."""
    if not diagnostics:
        return
    print(_escape_terminal_text(header), file=sys.stderr)
    for diagnostic in diagnostics:
        for line in _format_diagnostic_lines(diagnostic, include_severity=True):
            print(line, file=sys.stderr)


def _format_diagnostic_lines(
    diagnostic: Diagnostic,
    *,
    include_severity: bool,
) -> list[str]:
    code = _escape_terminal_text(diagnostic.code)
    suffix = code
    if include_severity:
        suffix = f"{code}; severity={_escape_terminal_text(diagnostic.severity)}"
    lines = [
        f"[{_format_path(diagnostic.path)}] "
        f"{_escape_terminal_text(diagnostic.message)} ({suffix})"
    ]

    context = diagnostic.source_context
    if isinstance(context, SourceLocation):
        lines.append(f"  Source: {_format_source_location(context)}")
    elif isinstance(context, DiagnosticComparison):
        lines.extend(_format_comparison_site("Earlier", context.earlier))
        lines.extend(_format_comparison_site("Later", context.later))

    if diagnostic.hint is not None:
        lines.append(f"  Hint: {_escape_terminal_text(diagnostic.hint)}")
    return lines


def _format_comparison_site(
    label: str,
    site: DiagnosticComparisonSite,
) -> list[str]:
    lines = [f"  {label}: {_format_source_location(site.location)}"]
    if site.display_value is not None:
        lines.append(f"    Value: {_escape_terminal_text(site.display_value)}")
    return lines


def _format_source_location(location: SourceLocation) -> str:
    return (
        f"{_escape_terminal_text(location.source.label)} "
        f"[{_format_path(location.path)}]"
    )


def _format_path(path: tuple[str | int, ...]) -> str:
    if not path:
        return "<root>"
    return ".".join(_escape_terminal_text(str(part)) for part in path)


def _escape_terminal_text(value: str) -> str:
    """Escape backslashes and non-printing characters for plain terminal output."""
    escaped: list[str] = []
    for character in value:
        if character == "\\":
            escaped.append("\\\\")
            continue
        if character.isprintable():
            escaped.append(character)
            continue
        codepoint = ord(character)
        if codepoint <= 0xFF:
            escaped.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return "".join(escaped)
