"""Smoke tests for the CLI skeleton and shared error boundary."""

from importlib.metadata import entry_points, version
from pathlib import Path

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.errors import ApplicationError, ApplicationGroup


def test_console_script_loads_root_app() -> None:
    """Keep the installed cdh entry point connected to the root Typer app."""
    (entry_point,) = entry_points(group="console_scripts", name="cdh")

    assert entry_point.value == "comfyui_docker_helper.cli:app"
    assert entry_point.load() is app


def test_version_option_reports_installed_distribution_version(
    cli_runner: CliRunner,
) -> None:
    """Expose the installed package version at the root command."""
    result = cli_runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output == f"cdh {version('comfyui-docker-helper')}\n"


def test_root_command_exposes_current_groups() -> None:
    """Expose the supported root command groups and subcommands."""
    command = get_command(app)

    assert isinstance(command, ApplicationGroup)
    assert command.name == "cdh"
    assert set(command.commands) == {"host", "container"}
    assert set(command.commands["host"].commands) == {
        "build",
        "render",
        "validate",
    }
    assert set(command.commands["container"].commands) == {
        "download-files",
        "entrypoint",
        "install-custom-nodes",
    }


@pytest.mark.parametrize(
    ("args", "usage"),
    [
        ([], "Usage: cdh"),
        (["host"], "Usage: cdh host"),
        (["host", "validate"], "Usage: cdh host validate"),
        (["host", "render"], "Usage: cdh host render"),
        (["host", "build"], "Usage: cdh host build"),
        (["container"], "Usage: cdh container"),
        (
            ["container", "install-custom-nodes"],
            "Usage: cdh container install-custom-nodes",
        ),
        (["container", "download-files"], "Usage: cdh container download-files"),
        (["container", "entrypoint"], "Usage: cdh container entrypoint"),
    ],
)
@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_help_succeeds(
    cli_runner: CliRunner,
    args: list[str],
    usage: str,
    help_flag: str,
) -> None:
    """Expose working help for the root and both command groups."""
    result = cli_runner.invoke(app, [*args, help_flag])

    assert result.exit_code == 0
    assert usage in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["container", "install-custom-nodes"],
        ["container", "download-files"],
    ],
)
def test_container_helper_help_exposes_lock_option(
    cli_runner: CliRunner,
    args: list[str],
) -> None:
    """Keep root lock artifacts visible on container helper commands."""
    result = cli_runner.invoke(app, [*args, "--help"])

    assert result.exit_code == 0
    assert "--config" in result.output
    assert "--lock" in result.output
    assert "Root rendered config.lock.toml." in result.output


def test_container_entrypoint_invokes_service_and_propagates_exit_code(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the CLI command wired to the runtime entrypoint service."""
    seen: dict[str, Path] = {}

    def fake_run_entrypoint(*, runtime) -> int:
        seen["workspace"] = runtime.workspace
        seen["comfyui_path"] = runtime.comfyui_path
        return 17

    monkeypatch.setattr(
        "comfyui_docker_helper.container.cli.run_entrypoint",
        fake_run_entrypoint,
    )
    monkeypatch.setenv("WORKSPACE", "/srv/work")
    monkeypatch.setenv("COMFYUI_PATH", "/opt/comfy")

    result = cli_runner.invoke(app, ["container", "entrypoint"])

    assert result.exit_code == 17
    assert seen == {
        "workspace": Path("/srv/work"),
        "comfyui_path": Path("/opt/comfy"),
    }


@pytest.mark.parametrize("args", [["--install-completion"], ["--show-completion"]])
def test_completion_options_remain_disabled(
    cli_runner: CliRunner,
    args: list[str],
) -> None:
    result = cli_runner.invoke(app, args)

    assert result.exit_code == 2
    assert "No such option" in result.output


@pytest.mark.parametrize(
    ("args", "usage"),
    [
        ([], "Usage: cdh"),
        (["host"], "Usage: cdh host"),
        (["container"], "Usage: cdh container"),
    ],
)
def test_unimplemented_groups_do_not_silently_succeed(
    cli_runner: CliRunner,
    args: list[str],
    usage: str,
) -> None:
    """Treat invocation without an implemented command as a usage failure."""
    result = cli_runner.invoke(app, args)

    assert result.exit_code == 2
    assert usage in result.output


def test_unknown_command_fails(cli_runner: CliRunner) -> None:
    """Reject commands outside the supported CLI surface."""
    result = cli_runner.invoke(app, ["unknown"])

    assert result.exit_code == 2
    assert "No such command" in result.output


def test_application_error_boundary(cli_runner: CliRunner) -> None:
    """Translate an expected application failure into a concise CLI failure."""
    probe_app = typer.Typer(cls=ApplicationGroup)

    @probe_app.callback()
    def probe() -> None:
        """Keep the probe as a group so its custom group class is exercised."""

    @probe_app.command()
    def fail() -> None:
        raise ApplicationError("expected failure", exit_code=7)

    result = cli_runner.invoke(probe_app, ["fail"])

    assert result.exit_code == 7
    assert result.stdout == ""
    assert result.stderr == "Error: expected failure\n"
    assert result.output == "Error: expected failure\n"


def test_application_error_requires_nonzero_exit_code() -> None:
    """Prevent expected failures from accidentally reporting success."""
    with pytest.raises(ValueError, match="exit codes must be positive"):
        ApplicationError("invalid exit code", exit_code=0)


def test_application_error_boundary_preserves_unexpected_errors(
    cli_runner: CliRunner,
) -> None:
    """Let programming errors propagate unchanged for diagnosis."""
    probe_app = typer.Typer(cls=ApplicationGroup)
    unexpected_error = RuntimeError("unexpected failure")

    @probe_app.callback()
    def probe() -> None:
        """Keep the probe as a group so its custom group class is exercised."""

    @probe_app.command()
    def fail() -> None:
        raise unexpected_error

    result = cli_runner.invoke(probe_app, ["fail"])

    assert result.exit_code == 1
    assert result.exception is unexpected_error
