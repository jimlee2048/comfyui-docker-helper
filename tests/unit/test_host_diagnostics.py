"""Focused host rendering contracts for source-aware diagnostics."""

from io import StringIO

from rich.console import Console

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticComparison,
    DiagnosticComparisonSite,
    SourceLocation,
    SourceReference,
)
from comfyui_docker_helper.host.diagnostics import render_configuration_diagnostics


def _console_output(diagnostic: Diagnostic, *, config_path: str = "config.toml") -> str:
    stream = StringIO()
    console = Console(
        file=stream,
        color_system=None,
        force_terminal=False,
        highlight=False,
        width=200,
    )
    render_configuration_diagnostics(config_path, (diagnostic,), console=console)
    return stream.getvalue()


def test_single_source_and_hint_render_as_control_safe_literal_text() -> None:
    diagnostic = Diagnostic(
        path=("system", "field[bold]", 0),
        code="schema.invalid[red]",
        message="invalid [green]value[/green]\nnext\x1b",
        source_context=SourceLocation(
            SourceReference(1, "later[red].toml\nname\x1b"),
            ("system", "field[blue]\nname", 0),
        ),
        hint="choose [bold]one[/bold]\rnow",
    )

    output = _console_output(diagnostic, config_path="configs[red]\n.toml")

    assert "Unable to process configuration: configs[red]\\n.toml" in output
    assert "[system.field[bold].0]" in output
    assert "invalid [green]value[/green]\\nnext\\x1b" in output
    assert "(schema.invalid[red])" in output
    assert "  Source:\n" in output
    assert "    File: later[red].toml\\nname\\x1b\n" in output
    assert "    Field: system.field[blue]\\nname.0\n" in output
    assert "  Hint: choose [bold]one[/bold]\\rnow\n" in output
    assert "\x1b" not in output


def test_comparison_is_vertical_ordered_and_omits_unapproved_values() -> None:
    earlier = SourceLocation(
        SourceReference(0, "base.toml"),
        ("cdh", "git", "credentials", 0, "match"),
    )
    later = SourceLocation(
        SourceReference(1, "later.toml"),
        ("cdh", "git", "credentials", 1, "match"),
    )
    diagnostic = Diagnostic(
        path=later.path,
        code="git_credential.duplicate_match",
        message="credential match contexts must be unique after normalization",
        source_context=DiagnosticComparison(
            earlier=DiagnosticComparisonSite(earlier),
            later=DiagnosticComparisonSite(later),
        ),
    )

    output = _console_output(diagnostic)

    earlier_position = output.index("  Earlier:\n")
    later_position = output.index("  Later:\n")
    assert earlier_position < later_position
    assert "    File: base.toml\n" in output
    assert "    Field: cdh.git.credentials.0.match\n" in output
    assert "    File: later.toml\n" in output
    assert "    Field: cdh.git.credentials.1.match\n" in output
    assert "Value:" not in output
    assert "(git_credential.duplicate_match)" in output


def test_comparison_renders_only_producer_approved_values() -> None:
    diagnostic = Diagnostic(
        path=("python", "extra_packages", 1),
        code="python.conflicting_package_requirement",
        message="requirements for the same package owner conflict",
        source_context=DiagnosticComparison(
            earlier=DiagnosticComparisonSite(
                SourceLocation(
                    SourceReference(0, "base.toml"),
                    ("python", "extra_packages", 0),
                ),
                display_value="demo<2[red]\n",
            ),
            later=DiagnosticComparisonSite(
                SourceLocation(
                    SourceReference(1, "later.toml"),
                    ("python", "extra_packages", 0),
                ),
                display_value=None,
            ),
        ),
    )

    output = _console_output(diagnostic)

    assert "    Value: demo<2[red]\\n\n" in output
    assert output.count("    Value:") == 1
    assert "(python.conflicting_package_requirement)" in output
