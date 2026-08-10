"""Focused host presentation contracts."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console
from tests.build_plan_support import accepted_resolution, build_plan, final_config

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticComparison,
    DiagnosticComparisonSite,
    DiagnosticSeverity,
    SourceLocation,
    SourceReference,
)
from comfyui_docker_helper.host import diagnostics as diagnostics_module
from comfyui_docker_helper.host.buildx import BuildxOutputPlan
from comfyui_docker_helper.host.diagnostics import HostPresenter
from comfyui_docker_helper.host.render_service import PlanningOptions


def _console(
    stream: StringIO,
    *,
    terminal: bool,
    width: int = 120,
    no_color: bool = False,
) -> Console:
    return Console(
        file=stream,
        force_terminal=terminal,
        color_system=None if no_color or not terminal else "standard",
        no_color=no_color,
        highlight=False,
        markup=False,
        width=width,
    )


def _presenter(
    *,
    stdout_terminal: bool = False,
    stderr_terminal: bool = False,
    stdout_width: int = 120,
    stderr_width: int = 120,
) -> tuple[HostPresenter, StringIO, StringIO]:
    stdout = StringIO()
    stderr = StringIO()
    return (
        HostPresenter(
            stdout=_console(
                stdout,
                terminal=stdout_terminal,
                width=stdout_width,
            ),
            stderr=_console(
                stderr,
                terminal=stderr_terminal,
                width=stderr_width,
            ),
        ),
        stdout,
        stderr,
    )


def _comparison_diagnostic(*, approved_value: bool = True) -> Diagnostic:
    return Diagnostic(
        path=("python", "extra_packages", 1),
        code="python.conflicting_package_requirement",
        message="requirements [red]conflict[/red]\nnext\x1b",
        source_context=DiagnosticComparison(
            earlier=DiagnosticComparisonSite(
                SourceLocation(
                    SourceReference(0, "base[red].toml\nname\x1b"),
                    ("python", "extra_packages", 0),
                ),
                display_value="demo<2[red]" if approved_value else None,
            ),
            later=DiagnosticComparisonSite(
                SourceLocation(
                    SourceReference(1, "later.toml"),
                    ("python", "extra_packages", 0),
                ),
                display_value="demo>=2" if approved_value else None,
            ),
        ),
        hint="choose [bold]one[/bold]\rnow",
    )


def test_non_tty_diagnostic_is_plain_vertical_safe_and_omits_code() -> None:
    presenter, stdout, stderr = _presenter()

    presenter.diagnostics(
        "Configuration [bold]is invalid[/bold]\n",
        (_comparison_diagnostic(),),
    )

    output = stderr.getvalue()
    assert stdout.getvalue() == ""
    assert "Error: Configuration [bold]is invalid[/bold]\\n" in output
    assert "Field: python.extra_packages.1" in output
    assert "requirements [red]conflict[/red]\\nnext\\x1b" in output
    assert output.index("Earlier:\n") < output.index("Later:\n")
    assert "  File: base[red].toml\\nname\\x1b" in output
    assert "  Value: demo<2[red]" in output
    assert "Hint: choose [bold]one[/bold]\\rnow" in output
    assert "python.conflicting_package_requirement" not in output
    assert "\x1b[" not in output
    assert not any(character in output for character in "╭╮╰╯│")


def test_wide_tty_comparison_is_aligned_in_one_bordered_row() -> None:
    presenter, _, stderr = _presenter(stderr_terminal=True, stderr_width=120)

    presenter.diagnostics("Configuration is invalid", (_comparison_diagnostic(),))

    plain = _strip_ansi(stderr.getvalue())
    assert any("Earlier" in line and "Later" in line for line in plain.splitlines())
    assert any(character in plain for character in "╭┌")
    assert "base[red].toml\\nname\\x1b" in plain
    assert "base[red].toml\\\\n" not in plain
    assert "python.conflicting_package_requirement" not in plain


def test_narrow_tty_comparison_uses_symmetric_vertical_sections() -> None:
    presenter, _, stderr = _presenter(stderr_terminal=True, stderr_width=70)

    presenter.diagnostics("Configuration is invalid", (_comparison_diagnostic(),))

    plain = _strip_ansi(stderr.getvalue())
    assert "Earlier" in plain and "Later" in plain
    assert not any("Earlier" in line and "Later" in line for line in plain.splitlines())
    assert plain.index("Earlier") < plain.index("Later")
    assert plain.count("File:") == 2
    assert plain.count("Field:") == 3


def test_single_source_warning_is_lightweight_and_has_no_sensitive_value() -> None:
    presenter, _, stderr = _presenter(stderr_terminal=True)
    secret_marker = "private-secret-marker"
    warning = Diagnostic(
        path=("secrets", "private"),
        code="secret.permissive_mode",
        message="Secret source mode is more permissive than recommended",
        severity=DiagnosticSeverity.WARNING,
        source_context=SourceLocation(
            SourceReference(0, "config.toml"),
            ("secrets", "private", "file"),
        ),
    )

    presenter.warnings((warning,))

    plain = _strip_ansi(stderr.getvalue())
    assert "Warnings" in plain
    assert "Source:" in plain
    assert "File: config.toml" in plain
    assert "Field: secrets.private.file" in plain
    assert "secret.permissive_mode" not in plain
    assert secret_marker not in plain
    assert "Value:" not in plain
    assert not any(character in plain for character in "╭╮╰╯│")


def test_comparison_omits_values_not_approved_by_the_producer() -> None:
    presenter, _, stderr = _presenter(stderr_terminal=True)

    presenter.diagnostics(
        "Configuration is invalid",
        (_comparison_diagnostic(approved_value=False),),
    )

    plain = _strip_ansi(stderr.getvalue())
    assert "Earlier" in plain and "Later" in plain
    assert "Value:" not in plain


def test_stream_interactivity_is_independent() -> None:
    presenter, stdout, stderr = _presenter(
        stdout_terminal=False,
        stderr_terminal=True,
    )

    presenter.validate_success(("config.toml",))
    presenter.warning("SSH forwarding was ignored")

    assert stdout.getvalue() == ""
    assert "\x1b[" in stderr.getvalue()
    assert "Warning: SSH forwarding was ignored" in _strip_ansi(stderr.getvalue())

    presenter, stdout, stderr = _presenter(
        stdout_terminal=True,
        stderr_terminal=False,
    )
    presenter.validate_success(("config[red].toml",))
    presenter.failure("Image build failed", "Docker [red]failed[/red]\x1b")

    assert "\x1b[" in stdout.getvalue()
    assert "Configuration valid" in _strip_ansi(stdout.getvalue())
    assert "File: config[red].toml" in _strip_ansi(stdout.getvalue())
    assert "\x1b[" not in stderr.getvalue()
    assert "Docker [red]failed[/red]\\x1b" in stderr.getvalue()


class _TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def test_default_presenter_honors_no_color_without_losing_tty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _TtyStringIO()
    stderr = _TtyStringIO()
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(diagnostics_module.sys, "stdout", stdout)
    monkeypatch.setattr(diagnostics_module.sys, "stderr", stderr)

    presenter = diagnostics_module.default_host_presenter()
    presenter.validate_success(("config.toml",))
    presenter.warning("plain warning")

    assert presenter.stdout.is_terminal is True
    assert presenter.stderr.is_terminal is True
    assert "Configuration valid" in stdout.getvalue()
    assert "Warning: plain warning" in stderr.getvalue()
    assert "\x1b[" not in stdout.getvalue() + stderr.getvalue()


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (PlanningOptions(), "Build context rendered"),
        (PlanningOptions(check=True), "Build context is up to date"),
        (PlanningOptions(locked=True), "Build context verified"),
    ],
)
def test_render_success_is_interactive_only_and_names_the_outcome(
    options: PlanningOptions,
    expected: str,
) -> None:
    interactive, stdout, _ = _presenter(stdout_terminal=True)
    interactive.render_success(
        "context[red]",
        options=options,
        lock_changed=False,
    )
    plain = _strip_ansi(stdout.getvalue())
    assert expected in plain
    assert "Context: context[red]" in plain
    assert "Lock: unchanged" in plain

    redirected, stdout, stderr = _presenter()
    redirected.render_success(
        "context",
        options=options,
        lock_changed=True,
    )
    assert stdout.getvalue() == stderr.getvalue() == ""


def test_dry_run_has_no_extra_success_and_plan_preview_owns_stdout() -> None:
    presenter, stdout, stderr = _presenter()
    resolution = accepted_resolution()
    plan = build_plan(final_config(), resolution)
    options = PlanningOptions(dry_run=True)

    presenter.render_success("context", options=options, lock_changed=True)
    presenter.plan_preview(
        plan,
        lock_result=resolution,
        options=options,
        output_plan=None,
    )

    output = stdout.getvalue()
    assert "Build context rendered" not in output
    assert "Build plan preview" in output
    assert "Buildx output\n  None" in output
    assert "\x1b[" not in output
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("output", ["load", "push"])
def test_build_summaries_bracket_external_stream_on_stdout(output: str) -> None:
    presenter, stdout, stderr = _presenter()
    output_plan = BuildxOutputPlan(tags=("example/image:one",), output=output)

    presenter.build_start(
        "context[red]",
        output_plan=output_plan,
        platforms=("linux/amd64",),
    )
    stdout.write("external-buildkit-line\n")
    presenter.build_complete(output_plan=output_plan)

    rendered = stdout.getvalue()
    assert rendered.index("Starting image build") < rendered.index(
        "external-buildkit-line"
    )
    assert rendered.index("external-buildkit-line") < rendered.index(
        "Image build complete"
    )
    assert "Context: context[red]" in rendered
    outcome = "Pushed" if output == "push" else "Loaded"
    assert f"{outcome}: example/image:one" in rendered
    assert "\x1b[" not in rendered
    assert stderr.getvalue() == ""


def _strip_ansi(value: str) -> str:
    result = value
    while "\x1b[" in result:
        prefix, suffix = result.split("\x1b[", maxsplit=1)
        index = 0
        while index < len(suffix) and not suffix[index].isalpha():
            index += 1
        result = prefix + suffix[index + 1 :]
    return result
