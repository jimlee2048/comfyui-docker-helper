"""Smoke tests for the CLI skeleton and shared error boundary."""

from contextlib import contextmanager
from importlib.metadata import entry_points, version
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.config.canonical_resolver import CanonicalAcquisitionError
from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.errors import ApplicationError, ApplicationGroup
from comfyui_docker_helper.host.render_service import HostRenderServiceError
from comfyui_docker_helper.host.uv_runner import HostUvError


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


def test_container_helper_help_exposes_phase_binding(cli_runner: CliRunner) -> None:
    """Container build helpers accept only digest-bound phase inputs."""
    result = cli_runner.invoke(app, ["container", "download-files", "--help"])

    assert result.exit_code == 0
    assert "--phase" in result.output
    assert "--build-plan-digest" in result.output
    assert "--config" not in result.output
    assert "--lock" not in result.output


def test_host_hook_option_is_preserved_only_on_render_and_build(
    cli_runner: CliRunner,
) -> None:
    render_help = cli_runner.invoke(app, ["host", "render", "--help"])
    build_help = cli_runner.invoke(app, ["host", "build", "--help"])
    validate_help = cli_runner.invoke(app, ["host", "validate", "--help"])

    assert "--hooks-dir" in render_help.output
    assert "--hooks-dir" in build_help.output
    assert "--hooks-dir" not in validate_help.output


def test_host_validate_remains_offline_and_does_not_construct_providers(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_providers():
        pytest.fail("host validate must not construct planning providers")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers",
        fail_providers,
    )

    result = cli_runner.invoke(
        app,
        ["host", "validate", "-f", "examples/minimal.toml"],
    )

    assert result.exit_code == 0
    assert result.output == ""


def test_render_option_conflict_is_one_short_diagnostic_without_traceback(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(tmp_path / "missing.toml"),
            "-o",
            str(tmp_path / "context"),
            "--locked",
            "--upgrade-lock",
        ],
    )

    assert result.exit_code == 1
    assert "render.options_conflict" in result.output
    assert "Traceback" not in result.output


def test_render_host_uv_error_is_one_short_diagnostic_without_traceback(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[compute_platform]
type = "cuda"
[compute_platform.cuda]
version = "13.0.3"
[pytorch]
version = "2.12.1"
[comfyui]
version = "0.4.0"
install_manager = false
[build]
tags = ["example:test"]
platforms = ["linux/amd64"]
"""
    )

    def fail_providers():
        raise HostUvError(
            (Diagnostic(("host", "uv"), "host.uv.not-found", "reinstall cdh"),)
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers",
        fail_providers,
    )
    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(tmp_path / "context")],
    )

    assert result.exit_code == 1
    assert "host.uv.not-found" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "command_args",
    [
        ["host", "render", "-o", "context"],
        ["host", "build", "--context-dir", "context"],
    ],
)
def test_host_uv_catalog_acquisition_error_is_short_without_traceback(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_args: list[str],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[compute_platform]
type = "cuda"
[compute_platform.cuda]
version = "13.0.3"
[pytorch]
version = "2.12.1"
[comfyui]
version = "0.4.0"
install_manager = false
[build]
tags = ["example:test"]
platforms = ["linux/amd64"]
"""
    )

    class FailingAcquirer:
        def acquire(self, request, request_digest):
            raise CanonicalAcquisitionError(
                "OCI registry: identity provider request failed"
            )

    @contextmanager
    def providers():
        yield SimpleNamespace(acquirer=FailingAcquirer(), local_acquirer=object())

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers", providers
    )
    resolved_args = [
        str(tmp_path / argument) if argument == "context" else argument
        for argument in command_args
    ]
    result = cli_runner.invoke(app, [*resolved_args, "-f", str(config)])

    assert result.exit_code == 1
    assert "lock.resolve_failed" in result.output
    assert "identity provider request failed" in result.output
    assert "Traceback" not in result.output


def test_render_passes_hooks_dir_through_current_planning_boundary(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    @contextmanager
    def providers():
        yield SimpleNamespace(acquirer=object(), local_acquirer=object())

    def prepare(*args, **kwargs):
        seen["hooks_dir"] = kwargs["hooks_dir"]
        return SimpleNamespace(warnings=())

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers", providers
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.prepare_render_context", prepare
    )
    hooks = tmp_path / "hooks"
    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(tmp_path / "config.toml"),
            "-o",
            str(tmp_path / "context"),
            "--hooks-dir",
            str(hooks),
        ],
    )

    assert result.exit_code == 0
    assert seen["hooks_dir"] == hooks


def test_render_materialization_error_is_short_and_has_no_traceback(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def providers():
        yield SimpleNamespace(acquirer=object(), local_acquirer=object())

    def fail_prepare(*args, **kwargs):
        raise HostRenderServiceError(
            (
                Diagnostic(
                    ("render",),
                    "render.context_write_failed",
                    "context could not be written",
                ),
            )
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers", providers
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.prepare_render_context", fail_prepare
    )
    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(tmp_path / "config.toml"),
            "-o",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 1
    assert "render.context_write_failed" in result.output
    assert "Traceback" not in result.output


def test_build_passes_hooks_dir_before_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    @contextmanager
    def providers():
        yield SimpleNamespace(acquirer=object(), local_acquirer=object())

    validated = SimpleNamespace(
        config=SimpleNamespace(
            build=SimpleNamespace(tags=["example:test"], output="load")
        )
    )

    def prepare(*args, **kwargs):
        seen["hooks_dir"] = kwargs["hooks_dir"]
        return SimpleNamespace(
            warnings=(),
            plan=SimpleNamespace(build=SimpleNamespace(platforms=("linux/amd64",))),
        )

    def buildx(**kwargs):
        seen["built"] = kwargs["context_dir"]

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.load_validate_config_result",
        lambda *args, **kwargs: validated,
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers", providers
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.prepare_render_context", prepare
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx", buildx
    )
    hooks = tmp_path / "hooks"
    context = tmp_path / "context"
    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(tmp_path / "config.toml"),
            "--context-dir",
            str(context),
            "--hooks-dir",
            str(hooks),
        ],
    )

    assert result.exit_code == 0
    assert seen == {"hooks_dir": hooks, "built": context}


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
    """Avoid advertising shell completion commands before supporting them."""
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
