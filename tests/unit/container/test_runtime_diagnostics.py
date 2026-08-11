"""Plain, control-safe rendering contracts for container diagnostics."""

import pytest

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticComparison,
    DiagnosticComparisonSite,
    DiagnosticSeverity,
    SourceLocation,
    SourceReference,
)
from comfyui_docker_helper.container.runtime_diagnostics import (
    format_runtime_diagnostics,
    render_runtime_diagnostics,
)


def _location(
    ordinal: int,
    label: str,
    path: tuple[str | int, ...],
) -> SourceLocation:
    return SourceLocation(SourceReference(ordinal, label), path)


def test_fatal_diagnostic_without_source_keeps_plain_existing_shape() -> None:
    rendered = format_runtime_diagnostics(
        "runtime configuration is invalid",
        (
            Diagnostic(
                path=("comfyui", "port"),
                code="schema.greater_than_equal",
                message="Input should be greater than or equal to 1",
            ),
        ),
    )

    assert rendered == (
        "runtime configuration is invalid\n"
        "[comfyui.port] Input should be greater than or equal to 1 "
        "(schema.greater_than_equal)"
    )


def test_single_source_and_hint_use_a_compact_vertical_layout() -> None:
    rendered = format_runtime_diagnostics(
        "runtime configuration is invalid",
        (
            Diagnostic(
                path=("files", 0, "url"),
                code="schema.missing",
                message="Field required",
                source_context=_location(
                    2,
                    "/etc/cdh/runtime/config.toml",
                    ("files", 0),
                ),
                hint="Add an HTTP(S) URL to this runtime file.",
            ),
        ),
    )

    assert rendered == (
        "runtime configuration is invalid\n"
        "[files.0.url] Field required (schema.missing)\n"
        "  Source: /etc/cdh/runtime/config.toml [files.0]\n"
        "  Hint: Add an HTTP(S) URL to this runtime file."
    )


def test_comparison_renders_symmetric_sites_and_only_approved_values() -> None:
    rendered = format_runtime_diagnostics(
        "runtime configuration is invalid",
        (
            Diagnostic(
                path=("files", 1, "filename"),
                code="runtime_file.duplicate_target",
                message="runtime file targets must be unique",
                source_context=DiagnosticComparison(
                    earlier=DiagnosticComparisonSite(
                        _location(
                            1,
                            "/opt/cdh/runtime/config.toml",
                            ("files", 0, "filename"),
                        ),
                        "model-a.safetensors",
                    ),
                    later=DiagnosticComparisonSite(
                        _location(
                            2,
                            "/etc/cdh/runtime/config.toml",
                            ("files", 0, "filename"),
                        )
                    ),
                ),
                hint="Keep one file entry for this target.",
            ),
        ),
    )

    assert rendered == (
        "runtime configuration is invalid\n"
        "[files.1.filename] runtime file targets must be unique "
        "(runtime_file.duplicate_target)\n"
        "  Earlier: /opt/cdh/runtime/config.toml [files.0.filename]\n"
        "    Value: model-a.safetensors\n"
        "  Later: /etc/cdh/runtime/config.toml [files.0.filename]\n"
        "  Hint: Keep one file entry for this target."
    )


def test_warning_renderer_retains_code_and_severity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_runtime_diagnostics(
        "Runtime configuration warnings:",
        (
            Diagnostic(
                path=("system", "workspace"),
                code="runtime.host_only_ignored",
                message="host-only configuration is ignored by the container runtime",
                severity=DiagnosticSeverity.WARNING,
                source_context=_location(
                    1,
                    "/etc/cdh/runtime/config.toml",
                    ("system", "workspace"),
                ),
            ),
        ),
    )

    assert capsys.readouterr().err == (
        "Runtime configuration warnings:\n"
        "[system.workspace] host-only configuration is ignored by the container "
        "runtime (runtime.host_only_ignored; severity=warning)\n"
        "  Source: /etc/cdh/runtime/config.toml [system.workspace]\n"
    )


def test_all_dynamic_diagnostic_text_is_control_safe() -> None:
    rendered = format_runtime_diagnostics(
        "invalid\nheader\x1b[31m",
        (
            Diagnostic(
                path=("field\tname",),
                code="code\rvalue",
                message="bad\nmessage\x1b[2J",
                source_context=DiagnosticComparison(
                    earlier=DiagnosticComparisonSite(
                        _location(
                            1,
                            "base\n.toml\x1b[31m",
                            ("items", "earlier\tfield"),
                        ),
                        "first\rvalue\\literal",
                    ),
                    later=DiagnosticComparisonSite(
                        _location(
                            2,
                            "later\x00.toml",
                            ("items", "later\nfield"),
                        ),
                        "second\u2028value",
                    ),
                ),
                hint="fix\nthis\x1b[0m",
            ),
        ),
    )

    assert rendered.count("\n") == 6
    assert "\r" not in rendered
    assert "\t" not in rendered
    assert "\x00" not in rendered
    assert "\x1b" not in rendered
    assert "\u2028" not in rendered
    assert "invalid\\x0aheader\\x1b[31m" in rendered
    assert "[field\\x09name] bad\\x0amessage\\x1b[2J (code\\x0dvalue)" in rendered
    assert "base\\x0a.toml\\x1b[31m [items.earlier\\x09field]" in rendered
    assert "Value: first\\x0dvalue\\\\literal" in rendered
    assert "later\\x00.toml [items.later\\x0afield]" in rendered
    assert "Value: second\\u2028value" in rendered
    assert "Hint: fix\\x0athis\\x1b[0m" in rendered


def test_empty_diagnostics_do_not_render_a_header(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_runtime_diagnostics("unused\nheader", ())

    assert capsys.readouterr().err == ""
