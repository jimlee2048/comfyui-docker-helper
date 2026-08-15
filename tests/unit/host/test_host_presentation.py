"""Focused host presentation contracts."""

from __future__ import annotations

from io import StringIO
from pathlib import PurePosixPath, PureWindowsPath

import pytest
from rich.console import Console

from comfyui_docker_helper.cli_output.policy import (
    CliOutputSettings,
    OutputContextKind,
    OutputDetail,
    OutputPolicy,
    StreamCapabilities,
)
from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticComparison,
    DiagnosticComparisonSite,
    DiagnosticSeverity,
    SourceLocation,
    SourceReference,
)
from comfyui_docker_helper.host import presentation as presentation_module
from comfyui_docker_helper.host.buildx import BuildxOutputPlan
from comfyui_docker_helper.host.events import HostPhase
from comfyui_docker_helper.host.path_display import display_host_path
from comfyui_docker_helper.host.presentation import HostPresenter
from comfyui_docker_helper.host.render_service import PlanningOptions
from comfyui_docker_helper.host.workflow_display import (
    HostCompletedPhase,
    HostWorkflowSummary,
)
from tests.build_plan_support import accepted_resolution, build_plan, final_config


def _console(
    stream: StringIO,
    *,
    terminal: bool,
    width: int = 120,
) -> Console:
    return Console(
        file=stream,
        force_terminal=terminal,
        color_system="standard" if terminal else None,
        no_color=False,
        highlight=False,
        markup=False,
        width=width,
        height=25,
    )


def _presenter(
    *,
    detail: OutputDetail = OutputDetail.NORMAL,
    stdout_terminal: bool = False,
    stderr_terminal: bool = False,
    stdout_unicode: bool = True,
    stderr_unicode: bool = True,
    stdout_width: int = 120,
    stderr_width: int = 120,
    working_directory: PurePosixPath | PureWindowsPath | None = None,
) -> tuple[HostPresenter, StringIO, StringIO]:
    stdout = StringIO()
    stderr = StringIO()
    stdout_capabilities = _capabilities(
        terminal=stdout_terminal,
        unicode=stdout_unicode,
    )
    stderr_capabilities = _capabilities(
        terminal=stderr_terminal,
        unicode=stderr_unicode,
    )
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
            policy=OutputPolicy(
                settings=CliOutputSettings(detail=detail),
                stdout=stdout_capabilities,
                stderr=stderr_capabilities,
                context=OutputContextKind.ONE_SHOT,
            ),
            working_directory=(
                PurePosixPath("/workspace/project")
                if working_directory is None
                else working_directory
            ),
        ),
        stdout,
        stderr,
    )


def _capabilities(*, terminal: bool, unicode: bool) -> StreamCapabilities:
    return StreamCapabilities.from_facts(
        is_terminal=terminal,
        no_color=False,
        term="xterm-256color" if unicode else "dumb",
        encoding="utf-8" if unicode else "ascii",
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


@pytest.mark.parametrize(
    "detail",
    [OutputDetail.QUIET, OutputDetail.NORMAL, OutputDetail.VERBOSE],
)
def test_diagnostic_code_is_reserved_for_debug(detail: OutputDetail) -> None:
    presenter, _, stderr = _presenter(detail=detail)

    presenter.diagnostics("Configuration is invalid", (_comparison_diagnostic(),))

    assert "python.conflicting_package_requirement" not in stderr.getvalue()


def test_debug_diagnostic_includes_control_safe_code() -> None:
    presenter, _, stderr = _presenter(detail=OutputDetail.DEBUG)
    diagnostic = Diagnostic(
        path=("python",),
        code="python.invalid\ncode\x1b",
        message="invalid configuration",
    )

    presenter.diagnostics("Configuration is invalid", (diagnostic,))

    output = stderr.getvalue()
    assert "Code: python.invalid\\ncode\\x1b" in output
    assert "\x1b[" not in output


def test_wide_tty_comparison_aligns_participants_on_one_row() -> None:
    presenter, _, stderr = _presenter(stderr_terminal=True, stderr_width=120)

    presenter.diagnostics("Configuration is invalid", (_comparison_diagnostic(),))

    plain = _strip_ansi(stderr.getvalue())
    assert any("Earlier" in line and "Later" in line for line in plain.splitlines())
    assert "base[red].toml\\nname\\x1b" in plain
    assert "base[red].toml\\\\n" not in plain


def test_narrow_tty_comparison_uses_symmetric_vertical_sections() -> None:
    presenter, _, stderr = _presenter(stderr_terminal=True, stderr_width=70)

    presenter.diagnostics("Configuration is invalid", (_comparison_diagnostic(),))

    plain = _strip_ansi(stderr.getvalue())
    assert "Earlier" in plain and "Later" in plain
    assert not any("Earlier" in line and "Later" in line for line in plain.splitlines())
    assert plain.index("Earlier") < plain.index("Later")
    assert "base[red].toml\\nname\\x1b" in plain
    assert "later.toml" in plain
    assert "python.extra_packages.0" in plain
    assert "python.extra_packages.1" in plain


def test_single_source_warning_omits_code_and_unapproved_value() -> None:
    presenter, _, stderr = _presenter(stderr_terminal=True)
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
    assert "Value:" not in plain


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
    assert "[1/1] config[red].toml" in _strip_ansi(stdout.getvalue())
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
    monkeypatch.setattr(presentation_module.sys, "stdout", stdout)
    monkeypatch.setattr(presentation_module.sys, "stderr", stderr)

    presenter = presentation_module.default_host_presenter()
    presenter.validate_success(("config.toml",))
    presenter.warning("plain warning")

    assert presenter.stdout.is_terminal is True
    assert presenter.stderr.is_terminal is True
    assert "Configuration valid" in stdout.getvalue()
    assert "Warning: plain warning" in stderr.getvalue()
    assert "\x1b[" not in stdout.getvalue() + stderr.getvalue()


def test_tty_validation_lists_layers_in_merge_order_with_relative_paths() -> None:
    presenter, stdout, stderr = _presenter(stdout_terminal=True)

    presenter.validate_success(
        (
            PurePosixPath("/workspace/project/config/base.toml"),
            PurePosixPath("/workspace/project/config/gpu.toml"),
        )
    )

    output = _strip_ansi(stdout.getvalue())
    assert "Configuration valid" in output
    assert "Configuration layers (2, merge order)" in output
    assert "[1/2] config/base.toml" in output
    assert "[2/2] config/gpu.toml" in output
    assert output.index("[1/2]") < output.index("[2/2]")
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("detail", [OutputDetail.NORMAL, OutputDetail.QUIET])
def test_non_tty_validation_is_silent_without_verbose_detail(
    detail: OutputDetail,
) -> None:
    presenter, stdout, stderr = _presenter(detail=detail)

    presenter.validate_success(("config.toml", "local.toml"))

    assert stdout.getvalue() == stderr.getvalue() == ""


def test_non_tty_verbose_validation_uses_ordered_ascii_summary() -> None:
    presenter, stdout, stderr = _presenter(detail=OutputDetail.VERBOSE)

    presenter.validate_success(("config.toml", "local.toml"))

    output = stdout.getvalue()
    assert "Configuration layers (2, merge order)" in output
    assert "[1/2] config.toml" in output
    assert "[2/2] local.toml" in output
    assert "├" not in output and "└" not in output and "│" not in output
    assert "\x1b[" not in output and "\r" not in output
    assert stderr.getvalue() == ""


def test_quiet_tty_validation_suppresses_optional_success() -> None:
    presenter, stdout, stderr = _presenter(
        detail=OutputDetail.QUIET,
        stdout_terminal=True,
    )

    presenter.validate_success(("config.toml",))

    assert stdout.getvalue() == stderr.getvalue() == ""


def test_weak_tty_validation_uses_ascii_without_color() -> None:
    presenter, stdout, _ = _presenter(
        stdout_terminal=True,
        stdout_unicode=False,
    )

    presenter.validate_success(("config.toml", "local.toml"))

    output = stdout.getvalue()
    first_layer = output.index("[1/2] config.toml")
    second_layer = output.index("[2/2] local.toml")
    assert first_layer < second_layer
    assert "├" not in output and "└" not in output and "│" not in output
    assert "\x1b[" not in output


def test_host_path_display_is_lexical_relative_inside_and_absolute_outside() -> None:
    cwd = PurePosixPath("/workspace/project")

    assert (
        display_host_path(
            PurePosixPath("/workspace/project/config/../base.toml"),
            working_directory=cwd,
        )
        == "base.toml"
    )
    assert (
        display_host_path(
            PurePosixPath("/workspace/other/config.toml"),
            working_directory=cwd,
        )
        == "/workspace/other/config.toml"
    )


def test_windows_drive_and_unc_paths_keep_native_single_backslashes() -> None:
    drive_cwd = PureWindowsPath(r"C:\workspace\project")
    drive_path = display_host_path(
        PureWindowsPath(r"C:\workspace\project\config\base.toml"),
        working_directory=drive_cwd,
    )
    outside_drive = display_host_path(
        PureWindowsPath(r"D:\shared\base.toml"),
        working_directory=drive_cwd,
    )
    unc_path = display_host_path(
        PureWindowsPath(r"\\server\share\project\config\base.toml"),
        working_directory=PureWindowsPath(r"\\server\share\project"),
    )

    assert drive_path == r"config\base.toml"
    assert outside_drive == r"D:\shared\base.toml"
    assert unc_path == r"config\base.toml"
    assert r"config\\base.toml" not in drive_path + unc_path


def test_host_path_display_escapes_controls_without_resolving_symlinks() -> None:
    displayed = display_host_path(
        PurePosixPath("/workspace/project/config\nname\x1b.toml"),
        working_directory=PurePosixPath("/workspace/project"),
    )

    assert displayed == r"config\nname\x1b.toml"


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (PlanningOptions(), "Build context rendered"),
        (PlanningOptions(check=True), "Build context is up to date"),
        (PlanningOptions(locked=True), "Build context verified"),
    ],
)
def test_render_success_names_the_outcome_in_interactive_and_plain_modes(
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
    assert stdout.getvalue() == f"{expected}: context (lock updated)\n"
    assert stderr.getvalue() == ""


def test_interactive_render_success_combines_phase_summary_and_result_once() -> None:
    presenter, stdout, stderr = _presenter(stdout_terminal=True)

    presenter.render_success(
        "context",
        options=PlanningOptions(),
        lock_changed=True,
        workflow_summary=HostWorkflowSummary(
            (
                HostCompletedPhase(HostPhase.CONFIGURATION_VALIDATION, 0.1),
                HostCompletedPhase(HostPhase.BUILD_INPUT_RESOLUTION, 0.2),
            )
        ),
    )

    output = _strip_ansi(stdout.getvalue())
    assert output.count("Build context rendered") == 1
    assert output.count("Completed: Validating configuration") == 1
    assert output.count("Completed: Resolving build inputs") == 1
    assert "Context: context" in output
    assert "Lock: updated" in output
    assert stderr.getvalue() == ""


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
    preview_index = output.index("Build plan preview")
    buildx_index = output.index("Buildx output")
    none_index = output.index("None", buildx_index)
    assert preview_index < buildx_index < none_index
    assert "\x1b[" not in output
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("output", ["load", "push"])
def test_build_start_uses_stderr_and_completion_uses_stdout(output: str) -> None:
    presenter, stdout, stderr = _presenter()
    output_plan = BuildxOutputPlan(tags=("example/image:one",), output=output)

    presenter.build_start(
        "context[red]",
        output_plan=output_plan,
        platforms=("linux/amd64",),
    )
    stdout.write("external-buildkit-line\n")
    presenter.build_complete(output_plan=output_plan)

    rendered_stdout = stdout.getvalue()
    assert "Starting image build" not in rendered_stdout
    assert rendered_stdout.index("external-buildkit-line") < rendered_stdout.index(
        "Image build complete"
    )
    assert stderr.getvalue() == "Starting image build\n"
    assert "Context: context[red]" not in stderr.getvalue()
    outcome = "Pushed" if output == "push" else "Loaded"
    assert f"{outcome}: example/image:one" in rendered_stdout
    assert "\x1b[" not in rendered_stdout + stderr.getvalue()


def test_verbose_build_start_adds_safe_details_on_stderr() -> None:
    presenter, stdout, stderr = _presenter(detail=OutputDetail.VERBOSE)
    output_plan = BuildxOutputPlan(tags=("example/image:one",), output="load")

    presenter.build_start(
        "context[red]",
        output_plan=output_plan,
        platforms=("linux/amd64",),
    )

    assert stdout.getvalue() == ""
    output = stderr.getvalue()
    assert "Starting image build" in output
    assert "Context: context[red]" in output
    assert "Output: load" in output
    assert "Platforms: linux/amd64" in output
    assert "Tags: example/image:one" in output


def test_quiet_hides_optional_build_framing_and_completion() -> None:
    presenter, stdout, stderr = _presenter(detail=OutputDetail.QUIET)
    output_plan = BuildxOutputPlan(tags=("example/image:one",), output="load")

    presenter.build_start(
        "context",
        output_plan=output_plan,
        platforms=("linux/amd64",),
    )
    presenter.build_complete(output_plan=output_plan)

    assert stdout.getvalue() == stderr.getvalue() == ""


def _strip_ansi(value: str) -> str:
    result = value
    while "\x1b[" in result:
        prefix, suffix = result.split("\x1b[", maxsplit=1)
        index = 0
        while index < len(suffix) and not suffix[index].isalpha():
            index += 1
        result = prefix + suffix[index + 1 :]
    return result
