"""Smoke tests for the CLI skeleton and shared error boundary."""

import json
from contextlib import contextmanager
from importlib.metadata import entry_points, version
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from tests.unit.test_build_plan import accepted_resolution, build_plan, final_config
from typer.main import get_command
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.config.build_plan import BuildOutputPlan, build_plan_digest
from comfyui_docker_helper.config.canonical_resolver import CanonicalAcquisitionError
from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.container import cli as container_cli
from comfyui_docker_helper.container import phase_inputs as phase_inputs_module
from comfyui_docker_helper.container.phase_inputs import (
    BuildPhaseDocument,
    phase_document,
)
from comfyui_docker_helper.errors import ApplicationError, ApplicationGroup
from comfyui_docker_helper.host.render_service import HostRenderServiceError
from comfyui_docker_helper.host.uv_runner import HostUvError
from comfyui_docker_helper.rendering.final_materializer import materialize_build_plan


# The public command surface, adapters, and error boundary stay executable and concise.
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
        "install-comfyui",
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
        (["container", "download-files"], "Usage: cdh container download-files"),
        (["container", "install-comfyui"], "Usage: cdh container install-comfyui"),
        (
            ["container", "install-custom-nodes"],
            "Usage: cdh container install-custom-nodes",
        ),
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


def test_registry_helper_help_exposes_only_owned_inputs(
    cli_runner: CliRunner,
) -> None:
    result = cli_runner.invoke(
        app,
        ["container", "install-custom-nodes", "--help"],
    )

    assert result.exit_code == 0
    assert "--custom-nodes-phase" in result.output
    assert "--application-phase" in result.output
    assert "--build-plan-digest" in result.output
    assert "--constraints" in result.output
    assert "--hooks-directory" in result.output
    assert "--config" not in result.output
    assert "--lock" not in result.output


@pytest.mark.parametrize(
    "command",
    ["download-files", "install-comfyui", "install-custom-nodes"],
)
def test_container_phase_consumers_admit_one_canonical_plan_per_invocation(
    command: str,
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admit every installer input once at the CLI edge as narrow phases."""
    plan = build_plan(final_config(), accepted_resolution())
    context = tmp_path / "context"
    context.mkdir()
    materialize_build_plan(plan, context)
    parse_count = 0
    parse = phase_inputs_module.parse_build_plan_json

    def counted_parse(document):
        nonlocal parse_count
        parse_count += 1
        return parse(document)

    observed: list[tuple[object, ...]] = []
    monkeypatch.setattr(phase_inputs_module, "parse_build_plan_json", counted_parse)
    monkeypatch.setattr(
        container_cli, "MATERIALIZED_BUILD_PLAN_PATH", context / "build-plan.json"
    )
    monkeypatch.setattr(
        container_cli,
        "download_files",
        lambda files: observed.append((files,)),
    )
    monkeypatch.setattr(
        container_cli,
        "install_comfyui",
        lambda application, toolchain, **_kwargs: observed.append(
            (application, toolchain)
        ),
    )
    monkeypatch.setattr(
        container_cli,
        "install_custom_nodes",
        lambda custom_nodes, application, **_kwargs: observed.append(
            (custom_nodes, application)
        ),
    )
    monkeypatch.setenv("WORKSPACE", plan.application.paths.workspace)
    monkeypatch.setenv("COMFYUI_PATH", plan.application.paths.comfyui)
    monkeypatch.setenv("VIRTUAL_ENV", plan.application.paths.venv)
    phase_dir = context / "phases"
    arguments = {
        "download-files": [
            "--phase",
            str(phase_dir / "files.json"),
        ],
        "install-comfyui": [
            "--application-phase",
            str(phase_dir / "application.json"),
            "--toolchain-phase",
            str(phase_dir / "toolchain.json"),
        ],
        "install-custom-nodes": [
            "--custom-nodes-phase",
            str(phase_dir / "custom-nodes.json"),
            "--application-phase",
            str(phase_dir / "application.json"),
        ],
    }
    result = cli_runner.invoke(
        app,
        [
            "container",
            command,
            *arguments[command],
            "--build-plan-digest",
            build_plan_digest(plan),
        ],
    )

    assert result.exit_code == 0, result.output
    assert parse_count == 1
    assert (
        observed
        == {
            "download-files": [(plan.files,)],
            "install-comfyui": [(plan.application, plan.toolchain)],
            "install-custom-nodes": [(plan.custom_nodes, plan.application)],
        }[command]
    )


# CLI admission reports canonical-plan failures without disclosing plan values.
def test_container_phase_admission_hides_invalid_plan_secret_values(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "password-sentinel-do-not-print"
    plan = build_plan(final_config(), accepted_resolution())
    context = tmp_path / "context"
    context.mkdir()
    materialize_build_plan(plan, context)
    plan_path = context / "build-plan.json"
    document = json.loads(plan_path.read_bytes())
    document["runtime"]["ssh"]["password"] = f"{sentinel}\n"
    plan_path.write_text(json.dumps(document))
    monkeypatch.setattr(container_cli, "MATERIALIZED_BUILD_PLAN_PATH", plan_path)

    result = cli_runner.invoke(
        app,
        [
            "container",
            "download-files",
            "--phase",
            str(context / "phases/files.json"),
            "--build-plan-digest",
            build_plan_digest(plan),
        ],
    )

    assert result.exit_code == 1
    assert result.output == "Error: canonical BuildPlan is invalid\n"
    assert sentinel not in result.output
    assert sentinel not in str(result.exception)


def test_container_phase_cli_rejects_registry_without_manager(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    context = tmp_path / "context"
    context.mkdir()
    materialize_build_plan(plan, context)
    plan_path = context / "build-plan.json"
    document = json.loads(plan_path.read_bytes())
    document["application"]["comfyui"]["manager"] = None
    document["custom_nodes"]["install_manager"] = False
    plan_path.write_text(json.dumps(document))
    monkeypatch.setattr(container_cli, "MATERIALIZED_BUILD_PLAN_PATH", plan_path)

    result = cli_runner.invoke(
        app,
        [
            "container",
            "download-files",
            "--phase",
            str(context / "phases/files.json"),
            "--build-plan-digest",
            build_plan_digest(plan),
        ],
    )

    assert result.exit_code == 1
    assert result.output == "Error: canonical BuildPlan is invalid\n"


# Host command adapters preserve offline validation and explicit render/build inputs.
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
version = "0.11.0"
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
version = "0.11.0"
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


def test_build_overrides_flow_through_plan_phase_and_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    @contextmanager
    def providers():
        yield SimpleNamespace(acquirer=object(), local_acquirer=object())

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
version = "0.11.0"
install_manager = false
[build]
tags = ["config:test"]
output = "load"
platforms = ["linux/amd64"]
"""
    )
    plan_digest = f"sha256:{'a' * 64}"
    plan_build = BuildOutputPlan(
        tags=("cli:first", "cli:second"),
        output="push",
        platforms=("linux/amd64",),
    )

    def prepare(*args, **kwargs):
        seen["hooks_dir"] = kwargs["hooks_dir"]
        configuration = kwargs["configuration_result"]
        seen["configuration_build"] = configuration.config.build
        phase = phase_document("build", plan_build, plan_digest)
        phase_path = context / "phases" / "build.json"
        phase_path.parent.mkdir(parents=True)
        phase_path.write_text(phase.model_dump_json())
        return SimpleNamespace(
            warnings=(),
            plan=SimpleNamespace(build=plan_build),
        )

    def buildx(**kwargs):
        seen["buildx"] = {
            "image_tags": kwargs["image_tags"],
            "output": kwargs["output"],
            "platforms": kwargs["platforms"],
            "context_dir": kwargs["context_dir"],
        }

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
            str(config),
            "--context-dir",
            str(context),
            "--hooks-dir",
            str(hooks),
            "--tag",
            "cli:first",
            "--tag",
            "cli:second",
            "--push",
        ],
    )

    assert result.exit_code == 0
    assert seen["hooks_dir"] == hooks
    configuration_build = seen["configuration_build"]
    assert configuration_build.tags == list(plan_build.tags)
    assert configuration_build.output == plan_build.output
    assert configuration_build.platforms == list(plan_build.platforms)
    assert (
        BuildPhaseDocument.model_validate_json(
            (context / "phases" / "build.json").read_bytes()
        ).payload
        == plan_build
    )
    assert seen["buildx"] == {
        "image_tags": plan_build.tags,
        "output": plan_build.output,
        "platforms": plan_build.platforms,
        "context_dir": context,
    }


def test_locked_build_override_stops_before_buildx_when_context_is_stale(
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
version = "0.11.0"
install_manager = false
[build]
tags = ["config:test"]
platforms = ["linux/amd64"]
"""
    )

    @contextmanager
    def providers():
        yield SimpleNamespace(acquirer=object(), local_acquirer=object())

    def prepare(*args, **kwargs):
        configuration = kwargs["configuration_result"]
        assert configuration.config.build.tags == ["cli:test"]
        assert kwargs["options"].locked is True
        raise HostRenderServiceError(
            (
                Diagnostic(
                    ("render",),
                    "render.context_changed",
                    "rendered context differs from the current BuildPlan",
                ),
            )
        )

    def fail_buildx(**kwargs):
        pytest.fail("stale locked context must stop before Docker Buildx")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers", providers
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.prepare_render_context", prepare
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx", fail_buildx
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "--context-dir",
            str(tmp_path / "context"),
            "--tag",
            "cli:test",
            "--locked",
        ],
    )

    assert result.exit_code == 1
    assert "render.context_changed" in result.output
    assert "Traceback" not in result.output


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


# Usage and application failures remain nonzero without hiding unexpected exceptions.
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
