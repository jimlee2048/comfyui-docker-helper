"""Smoke tests for the CLI skeleton and shared error boundary."""

import inspect
from contextlib import contextmanager
from importlib.metadata import entry_points, version
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from rich.text import Text
from typer.main import get_command
from typer.testing import CliRunner

from comfyui_docker_helper.build_ssh import KNOWN_HOSTS_MOUNTS
from comfyui_docker_helper.cli import app
from comfyui_docker_helper.cli_output import CliOutputSettings, OutputDetail
from comfyui_docker_helper.config.build_plan import (
    DownloaderCredentialRoutePlan,
    GitCredentialRoutePlan,
)
from comfyui_docker_helper.config.canonical_resolver import CanonicalAcquisitionError
from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticSeverity
from comfyui_docker_helper.container import cli as container_cli
from comfyui_docker_helper.errors import ApplicationError, ApplicationGroup
from comfyui_docker_helper.host import cli as host_cli
from comfyui_docker_helper.host import secret_session as secret_session_module
from comfyui_docker_helper.host.buildx import (
    BuildxBuildError,
    BuildxOutputPlan,
    FileSecretBinding,
)
from comfyui_docker_helper.host.events import (
    HostPhase,
    HostPhaseCompleted,
    HostPhaseStarted,
    HostSubphase,
    HostSubphaseCompleted,
    HostSubphaseStarted,
)
from comfyui_docker_helper.host.render_service import HostRenderServiceError
from comfyui_docker_helper.host.secret_session import (
    GIT_CREDENTIAL_SESSION_ENV,
    HostSecretSession,
    HostSecretSessionError,
)
from comfyui_docker_helper.rendering.final_materializer import (
    _materialize_private_stage,
)
from tests.build_plan_support import (
    accepted_resolution,
    build_plan,
    canonical_wheel,
    final_config,
)


def _plain_output(output: str) -> str:
    return Text.from_ansi(output).plain


def _diagnostic_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\")


def _write_minimal_config(path: Path) -> None:
    path.write_text(
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
"""
    )


def _write_build_hook_config(path: Path) -> None:
    _write_minimal_config(path)
    with path.open("a") as config:
        config.write(
            """
[[comfyui.custom_nodes]]
type = "git"
url = "https://example.test/node.git"
ref = "1111111111111111111111111111111111111111"
pre_install_hooks = ["nested/pre.sh"]
"""
        )


def _write_direct_git_config(path: Path) -> None:
    _write_minimal_config(path)
    with path.open("a") as config:
        config.write(
            """
[[comfyui.custom_nodes]]
type = "git"
url = "https://example.test/private-node.git"
ref = "1111111111111111111111111111111111111111"
"""
        )


def _write_http_credential_config(path: Path) -> None:
    _write_minimal_config(path)
    with path.open("a") as config:
        config.write(
            """
[secrets.private_git]
file = "missing-token-file"

[[cdh.git.credentials]]
match = "http://git.example.test/team/"
username = "token-user"
password = { secret = "private_git" }
"""
        )


def _write_registry_config(path: Path) -> None:
    _write_minimal_config(path)
    path.write_text(
        path.read_text().replace("install_manager = false", "install_manager = true")
    )
    with path.open("a") as config:
        config.write(
            """
[[comfyui.custom_nodes]]
type = "registry"
id = "example.registry.node"
version = "1.2.3"
"""
        )


@contextmanager
def _stub_planning_providers():
    yield SimpleNamespace(
        acquirer=object(),
        local_acquirer=object(),
        canonical_wheel=canonical_wheel(),
    )


def _prepared_build() -> SimpleNamespace:
    return SimpleNamespace(
        plan=SimpleNamespace(
            toolchain=SimpleNamespace(platform="linux/amd64"),
            custom_nodes=SimpleNamespace(nodes=(), git_credentials=()),
            files=SimpleNamespace(files=(), credentials=()),
        ),
        output_plan=BuildxOutputPlan(tags=("example:test",), output="load"),
        warnings=(),
    )


def _complete_stubbed_preparation(kwargs: dict[str, object]) -> None:
    """Honor the render-service observer contract in successful CLI stubs."""
    event_sink = kwargs["event_sink"]
    options = kwargs["options"]
    event_sink.emit(HostPhaseCompleted(HostPhase.BUILD_INPUT_RESOLUTION))
    event_sink.emit(HostPhaseStarted(HostPhase.LOCK_RECONCILIATION))
    event_sink.emit(HostSubphaseStarted(HostSubphase.CANONICAL_IDENTITY_RECONCILIATION))
    event_sink.emit(
        HostSubphaseCompleted(HostSubphase.CANONICAL_IDENTITY_RECONCILIATION)
    )
    event_sink.emit(HostPhaseCompleted(HostPhase.LOCK_RECONCILIATION))
    event_sink.emit(HostPhaseStarted(HostPhase.BUILD_PLAN_PREPARATION))
    if not options.dry_run:
        event_sink.emit(HostPhaseCompleted(HostPhase.BUILD_PLAN_PREPARATION))
        event_sink.emit(HostPhaseStarted(HostPhase.CONTEXT_RENDER_CHECK))


def _prepared_build_with_events(*_args, **kwargs) -> SimpleNamespace:
    _complete_stubbed_preparation(kwargs)
    return _prepared_build()


def _prepared_render_with_events(*_args, **kwargs) -> SimpleNamespace:
    _complete_stubbed_preparation(kwargs)
    return SimpleNamespace(
        lock_result=accepted_resolution(),
        warnings=(),
    )


def _credential_build_plan(*, downloader: bool = False):
    plan = build_plan(final_config(), accepted_resolution())
    updates = {
        "custom_nodes": plan.custom_nodes.model_copy(
            update={
                "git_credentials": (
                    GitCredentialRoutePlan(
                        match="https://example.test/",
                        username="root-user",
                        secret_id="cdh-git-credential-root_token",
                    ),
                    GitCredentialRoutePlan(
                        match="https://example.test/team",
                        username="team-user",
                        secret_id="cdh-git-credential-root_token",
                    ),
                    GitCredentialRoutePlan(
                        match="https://gitlab.example.test/",
                        username="oauth2",
                        secret_id="cdh-git-credential-team_token",
                    ),
                )
            }
        )
    }
    if downloader:
        updates["files"] = plan.files.model_copy(
            update={
                "credentials": (
                    DownloaderCredentialRoutePlan(
                        match="https://example.test/",
                        type="bearer",
                        token={"secret": "root_token"},
                        secret_id="cdh-downloader-credential-root_token",
                    ),
                ),
                "files": tuple(
                    item.model_copy(update={"downloader": "httpx"})
                    if item.type == "http"
                    else item
                    for item in plan.files.files
                ),
            }
        )
    return plan.model_copy(
        update={
            **updates,
        }
    )


def _build_ssh_args(config: Path, *, context: Path | None = None) -> list[str]:
    args = [
        "host",
        "build",
        "-f",
        str(config),
        "-t",
        "example:test",
        "--ssh",
    ]
    if context is not None:
        args.extend(("--context-dir", str(context)))
    return args


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


def test_quiet_does_not_hide_version(cli_runner: CliRunner) -> None:
    normal = cli_runner.invoke(app, ["--version"])
    quiet = cli_runner.invoke(app, ["--quiet", "--version"])

    assert quiet.exit_code == 0
    assert quiet.stdout == normal.stdout
    assert quiet.stderr == normal.stderr == ""


def test_root_help_owns_output_detail_options_once(cli_runner: CliRunner) -> None:
    root = _plain_output(cli_runner.invoke(app, ["--help"]).output)
    host = _plain_output(cli_runner.invoke(app, ["host", "--help"]).output)
    leaf = _plain_output(cli_runner.invoke(app, ["host", "validate", "--help"]).output)

    assert root.count("--quiet") == 1
    assert root.count("--verbose") == 1
    assert "--quiet" not in host
    assert "--verbose" not in host
    assert "--quiet" not in leaf
    assert "--verbose" not in leaf


def test_quiet_and_verbose_fail_as_root_parameter_conflict(
    cli_runner: CliRunner,
) -> None:
    result = cli_runner.invoke(app, ["--quiet", "--verbose", "host", "validate"])

    assert result.exit_code == 2
    assert result.stdout == ""
    output = _plain_output(result.stderr)
    assert "Usage: cdh" in output
    assert "--quiet" in output
    assert "--verbose" in output
    assert "used together" in output


def test_output_detail_options_are_root_only(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["host", "validate", "--quiet"])

    assert result.exit_code == 2
    assert "No such option" in _plain_output(result.stderr)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-v"], OutputDetail.VERBOSE),
        (["-vv"], OutputDetail.DEBUG),
        (["-vvv"], OutputDetail.DEBUG),
    ],
)
def test_host_group_receives_root_output_settings(
    args: list[str],
    expected: OutputDetail,
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    observed: list[CliOutputSettings] = []
    original = host_cli.require_output_settings

    def observe(context: typer.Context) -> CliOutputSettings:
        settings = original(context)
        observed.append(settings)
        return settings

    monkeypatch.setattr(host_cli, "require_output_settings", observe)

    result = cli_runner.invoke(
        app,
        [*args, "host", "validate", "-f", str(config)],
    )

    assert result.exit_code == 0
    assert [settings.detail for settings in observed] == [expected]


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
        "emit-final-manifest",
        "install-comfyui",
        "install-custom-nodes",
        "runtime",
    }
    assert set(command.commands["container"].commands["runtime"].commands) == {
        "follow",
        "restart",
        "serve",
        "status",
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
    assert usage in _plain_output(result.output)


def test_container_group_remains_helpful_outside_linux(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the public boundary visible without loading Linux-only services."""
    monkeypatch.setattr(container_cli.sys, "platform", "win32")

    result = cli_runner.invoke(app, ["container", "--help"])

    assert result.exit_code == 0
    assert "runtime" in _plain_output(result.output)


@pytest.mark.parametrize(
    "args",
    [
        ["container", "runtime", "--help"],
        ["container", "runtime", "serve", "--help"],
        ["container", "runtime", "restart", "--help"],
        ["container", "runtime", "status", "--help"],
        ["container", "runtime", "follow", "--help"],
    ],
)
def test_runtime_help_remains_available_outside_linux(
    args: list[str],
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose the image-internal command tree without loading its services."""
    monkeypatch.setattr(container_cli.sys, "platform", "win32")

    result = cli_runner.invoke(app, args)

    assert result.exit_code == 0
    assert "Usage: cdh container runtime" in _plain_output(result.output)


def test_container_execution_reports_linux_only_boundary(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct host users to the supported command surface on non-Linux hosts."""
    monkeypatch.setattr(container_cli.sys, "platform", "win32")

    result = cli_runner.invoke(app, ["container", "runtime", "serve"])

    assert result.exit_code == 1
    assert "run only inside" in result.output
    assert "Linux image" in result.output
    assert "cdh host" in result.output
    assert "traceback" not in result.output.lower()


# CLI admission reports canonical-plan failures without disclosing plan values.


# Host command adapters preserve offline validation and explicit render/build inputs.
def test_host_hook_option_is_preserved_only_on_render_and_build(
    cli_runner: CliRunner,
) -> None:
    render_help = cli_runner.invoke(app, ["host", "render", "--help"])
    build_help = cli_runner.invoke(app, ["host", "build", "--help"])
    validate_help = cli_runner.invoke(app, ["host", "validate", "--help"])

    render_output = _plain_output(render_help.output)
    build_output = _plain_output(build_help.output)
    validate_output = _plain_output(validate_help.output)
    assert "--runtime-hooks-dir" in render_output
    assert "--runtime-hooks-dir" in build_output
    assert "--runtime-hooks-dir" not in validate_output
    assert "--build-hooks-dir" in render_output
    assert "--build-hooks-dir" in build_output
    assert "--build-hooks-dir" in validate_output


# SSH authentication is an explicit build-only public capability.
def test_ssh_option_is_exposed_only_by_host_build(cli_runner: CliRunner) -> None:
    build_output = _plain_output(
        cli_runner.invoke(app, ["host", "build", "--help"]).output
    )
    render_output = _plain_output(
        cli_runner.invoke(app, ["host", "render", "--help"]).output
    )
    validate_output = _plain_output(
        cli_runner.invoke(app, ["host", "validate", "--help"]).output
    )

    assert "--ssh" in build_output
    assert "default SSH agent and known-hosts trust" in " ".join(
        build_output.replace("│", " ").split()
    )
    assert "--ssh" not in render_output
    assert "--ssh" not in validate_output


def test_build_cache_options_are_exposed_only_by_host_build(
    cli_runner: CliRunner,
) -> None:
    build_output = _plain_output(
        cli_runner.invoke(app, ["host", "build", "--help"]).output
    )
    render_output = _plain_output(
        cli_runner.invoke(app, ["host", "render", "--help"]).output
    )
    validate_output = _plain_output(
        cli_runner.invoke(app, ["host", "validate", "--help"]).output
    )

    for option in ("--cache-from", "--cache-to"):
        assert option in build_output
        assert option not in render_output
        assert option not in validate_output
    normalized = " ".join(build_output.replace("│", " ").split())
    assert normalized.count("May be provided once") == 2


# Host help distinguishes Docker-backed resolution from later Buildx
# materialization.
def test_host_render_and_build_help_explain_locked_docker_boundaries(
    cli_runner: CliRunner,
) -> None:
    render_help = cli_runner.invoke(app, ["host", "render", "--help"])
    build_help = cli_runner.invoke(app, ["host", "build", "--help"])

    render_output = _plain_output(render_help.output)
    build_output = _plain_output(build_help.output)
    assert "Docker may be used when new uv resolution is needed" in render_output
    for output in (render_output, build_output):
        normalized = " ".join(output.replace("│", " ").split())
        assert "matching lock" in normalized
        assert "without resolving" in normalized
        assert "updating the context" in normalized
    assert "build it with Docker Buildx" in build_output


def test_host_hook_roots_have_no_implicit_defaults() -> None:
    for command in (host_cli.validate, host_cli.render, host_cli.build):
        assert inspect.signature(command).parameters["build_hooks_dir"].default is None
    for command in (host_cli.render, host_cli.build):
        assert (
            inspect.signature(command).parameters["runtime_hooks_dir"].default is None
        )


@pytest.mark.parametrize(
    "command",
    [
        ("validate",),
        ("render", "-o", "context"),
        ("render", "-o", "context", "--locked"),
        ("render", "-o", "context", "--check"),
        ("build", "--context-dir", "context", "--locked"),
    ],
)
def test_required_build_hook_root_fails_before_planning_providers(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
) -> None:
    config = tmp_path / "config.toml"
    _write_build_hook_config(config)

    def fail_providers():
        pytest.fail("build-hook admission must precede planning providers")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers",
        fail_providers,
    )
    result = cli_runner.invoke(
        app,
        ["host", *command, "-f", str(config)],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Configuration is invalid" in result.stderr
    assert "Field: build_hooks_dir" in result.stderr
    assert "--build-hooks-dir is required when build hooks are configured" in (
        result.stderr
    )
    assert not (tmp_path / "context").exists()


def test_overlapping_build_hook_root_fails_before_planning_providers(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "build-hooks"
    hook = source / "nested/pre.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("sentinel\n")
    config = tmp_path / "config.toml"
    _write_build_hook_config(config)

    def fail_providers():
        pytest.fail("source/output admission must precede planning providers")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers",
        fail_providers,
    )
    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(source),
            "--build-hooks-dir",
            str(source),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Unable to render build context" in result.stderr
    assert "Field: render" in result.stderr
    assert "output and build hook source must not overlap" in result.stderr
    assert hook.read_text() == "sentinel\n"


def test_invalid_build_hook_root_fails_before_planning_providers(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an explicit invalid required root before any planning work."""
    config = tmp_path / "config.toml"
    _write_build_hook_config(config)
    context = tmp_path / "context"

    def fail_providers():
        pytest.fail("build-hook admission must precede planning providers")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers",
        fail_providers,
    )
    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(context),
            "--build-hooks-dir",
            str(tmp_path / "missing-build-hooks"),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Configuration is invalid" in result.stderr
    assert "Field: build_hooks_dir" in result.stderr
    assert "must be an existing regular directory" in result.stderr
    assert not context.exists()


def test_relative_build_hook_root_is_resolved_from_invocation_directory(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve a relative build-hook root from cwd, not the config directory."""
    invocation = tmp_path / "invocation"
    hook = invocation / "build-hooks/nested/pre.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\n")
    config_dir = tmp_path / "configuration"
    config_dir.mkdir()
    config = config_dir / "config.toml"
    _write_build_hook_config(config)
    observed: list[Path | None] = []

    @contextmanager
    def providers():
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=object(),
        )

    def prepare(_output_dir, *, build_hook_source_root, **kwargs):
        observed.append(build_hook_source_root)
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(lock_result=accepted_resolution(), warnings=())

    monkeypatch.chdir(invocation)
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers",
        providers,
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.prepare_render_context",
        prepare,
    )
    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            "context",
            "--build-hooks-dir",
            "build-hooks",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "Build context rendered: context (lock unchanged)\n"
    assert "In progress: Validating configuration" in result.stderr
    assert "In progress: Resolving build inputs" in result.stderr
    assert observed == [(invocation / "build-hooks").resolve()]


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
    assert result.stdout == ""
    assert result.stderr == ""


def test_host_validate_accepts_and_composes_repeated_config_files(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    _write_minimal_config(base)
    with base.open("a") as config:
        config.write('\n[system]\nextra_packages = ["git-lfs"]\n')
    override.write_text('[system]\nextra_packages = ["ffmpeg"]\n')

    result = cli_runner.invoke(
        app,
        [
            "host",
            "validate",
            "-f",
            str(base),
            "-f",
            str(override),
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_host_validate_renders_both_sources_for_layered_requirement_conflict(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.toml"
    override = tmp_path / "override.toml"
    _write_minimal_config(base)
    with base.open("a") as config:
        config.write('\n[python]\nextra_packages = ["demo>=1,<2"]\n')
    override.write_text('[python]\nextra_packages = ["Demo>=2,<3"]\n')

    result = cli_runner.invoke(
        app,
        [
            "host",
            "validate",
            "-f",
            str(base),
            "-f",
            str(override),
        ],
    )
    output = _plain_output(result.stderr)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Configuration is invalid" in output
    assert "Field: python.extra_packages.1" in output
    assert "package demo has conflicting requirements" in output
    assert "python.conflicting_package_requirement" not in output
    assert "Earlier:" in output and "Later:" in output
    assert _diagnostic_path(base) in output.replace("\n", "")
    assert _diagnostic_path(override) in output.replace("\n", "")
    assert "Value: demo>=1,<2" in output
    assert "Value: Demo>=2,<3" in output
    assert "\x1b" not in result.stderr


def test_quiet_host_validate_preserves_redundant_default_os_package_warning(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    with config.open("a") as stream:
        stream.write('\n[system]\nextra_packages = ["bash"]\n')

    result = cli_runner.invoke(
        app,
        ["--quiet", "host", "validate", "-f", str(config)],
    )
    output = _plain_output(result.stderr)

    assert result.exit_code == 0
    assert result.stdout == ""
    assert output.count("already installed") == 1
    assert "bash" in output
    assert "Field: system.extra_packages.0" in output
    assert _diagnostic_path(config) in output.replace("\n", "")


def test_host_validate_displays_http_credential_warning_offline(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_http_credential_config(config)

    monkeypatch.setattr(
        host_cli,
        "default_planning_providers",
        lambda **_kwargs: pytest.fail("validate must remain offline"),
    )
    result = cli_runner.invoke(app, ["host", "validate", "-f", str(config)])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert (
        result.stderr.count(
            "credentials sent over HTTP lack TLS transport confidentiality"
        )
        == 1
    )
    assert "Field: cdh.git.credentials.0.match" in result.stderr


def test_http_credential_warning_precedes_build_provider_initialization(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_http_credential_config(config)

    class BoundaryReached(RuntimeError):
        pass

    def fail_providers(**_kwargs):
        raise BoundaryReached("planning provider boundary reached")

    monkeypatch.setattr(host_cli, "default_planning_providers", fail_providers)
    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "example:test",
            "--context-dir",
            str(tmp_path / "context"),
        ],
    )

    assert result.stdout == ""
    assert (
        result.stderr.count(
            "credentials sent over HTTP lack TLS transport confidentiality"
        )
        == 1
    )
    assert isinstance(result.exception, BoundaryReached)


def test_render_option_conflict_is_one_short_diagnostic(
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
    assert result.stdout == ""
    assert "Error: Unable to render build context" in result.stderr
    assert "--locked and --upgrade-lock are mutually exclusive" in result.stderr


@pytest.mark.parametrize(
    "command_args",
    [
        ["host", "render", "-o", "context"],
        ["host", "build", "--context-dir", "context"],
    ],
)
def test_provider_acquisition_error_is_short_without_traceback(
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
        yield SimpleNamespace(
            acquirer=FailingAcquirer(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers", providers
    )
    resolved_args = [
        str(tmp_path / argument) if argument == "context" else argument
        for argument in command_args
    ]
    result = cli_runner.invoke(app, [*resolved_args, "-f", str(config)])

    assert result.exit_code == 1
    assert result.stdout == ""
    expected_title = (
        "Error: Unable to render build context"
        if command_args[1] == "render"
        else "Error: Unable to prepare image build"
    )
    assert expected_title in result.stderr
    assert "identity provider request failed" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("selector", "cli_tags", "expected_message"),
    [
        (
            "0.11.0",
            ["example/image:${{comfyui.commit}}"],
            "must use a supported expression with canonical spacing",
        ),
        (
            "0.11.0",
            ["busybox:x", "docker.io/library/busybox:x"],
            "must not duplicate another normalized publication target",
        ),
        (
            "nightly",
            ["example/image:v${{ comfyui.release }}"],
            "comfyui.release is unavailable for this ComfyUI selector",
        ),
        (
            "1111111111111111111111111111111111111111",
            ["example/image:v${{ comfyui.release }}"],
            "comfyui.release is unavailable for this ComfyUI selector",
        ),
    ],
)
def test_cli_tags_use_shared_static_validation_before_provider_construction(
    selector: str,
    cli_tags: list[str],
    expected_message: str,
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_providers():
        pytest.fail("invalid CLI tags must fail before planning providers")

    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    config.write_text(
        config.read_text().replace('version = "0.11.0"', f'version = "{selector}"')
    )
    monkeypatch.setattr(host_cli, "default_planning_providers", fail_providers)

    args = ["host", "build", "-f", str(config)]
    for tag in cli_tags:
        args.extend(("--tag", tag))
    result = cli_runner.invoke(app, args)

    assert result.exit_code == 2
    assert result.stdout == ""
    plain = _plain_output(result.stderr)
    assert "Usage: cdh host build" in plain
    assert "Failed: Resolving build inputs" in plain
    assert "Failed: Validating configuration" not in plain
    assert expected_message in " ".join(plain.replace("│", " ").split())
    assert "build.invalid_tag_expression" not in plain


def test_cli_tag_override_does_not_hide_invalid_config_tags(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_providers():
        pytest.fail("invalid configuration must fail before planning providers")

    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    with config.open("a") as stream:
        stream.write('\n[build]\ntags = ["example/Image:bad"]\n')
    monkeypatch.setattr(host_cli, "default_planning_providers", fail_providers)

    result = cli_runner.invoke(
        app,
        ["host", "build", "-f", str(config), "--tag", "example/image:valid"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Configuration is invalid" in result.stderr
    assert "Field: build.tags.0" in result.stderr
    assert "repository path components must use lowercase" in result.stderr


@pytest.mark.parametrize("with_tags", [False, True])
def test_dry_run_renders_an_independent_buildx_output_section(
    with_tags: bool,
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution = accepted_resolution()
    plan = build_plan(final_config(), resolution)
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    tag_templates: tuple[str, ...] = ()
    output_plan: BuildxOutputPlan | None = None
    if with_tags:
        tag_templates = (
            "example/comfyui:v${{ comfyui.release }}",
            "example/comfyui:custom-${{ comfyui.commit.prefix(12) }}",
        )
        with config.open("a") as stream:
            stream.write(
                "\n[build]\n"
                'tags = ["example/comfyui:v${{ comfyui.release }}", '
                '"example/comfyui:custom-${{ comfyui.commit.prefix(12) }}"]\n'
                'output = "push"\n'
            )
        output_plan = BuildxOutputPlan(
            tags=("example/comfyui:v0.11.0", "example/comfyui:custom-111111111111"),
            output="push",
        )

    def prepare(*args, **kwargs):
        del args
        assert tuple(kwargs["tag_templates"]) == tag_templates
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(
            plan=plan,
            lock_result=resolution,
            output_plan=output_plan,
            warnings=(),
        )

    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(host_cli, "prepare_render_context", prepare)
    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(tmp_path / "context"),
            "--dry-run",
        ],
    )

    plain = _plain_output(result.stdout)
    assert result.exit_code == 0
    assert "In progress: Validating configuration" in result.stderr
    assert "In progress: Resolving build inputs" in result.stderr
    assert "Rendering or checking build context" not in result.stderr
    buildx_index = plain.index("Buildx output")
    assert plain.index("Custom nodes:") < buildx_index
    buildx_section = plain[buildx_index:]
    if output_plan is None:
        assert "None" in buildx_section
        assert "Mode" not in buildx_section
        assert "Tags" not in buildx_section
    else:
        mode_index = buildx_section.index("Mode")
        output_index = buildx_section.index(output_plan.output, mode_index)
        tags_index = buildx_section.index("Tags", output_index)
        first_tag_index = buildx_section.index(output_plan.tags[0], tags_index)
        second_tag_index = buildx_section.index(output_plan.tags[1], first_tag_index)
        assert mode_index < output_index < tags_index
        assert tags_index < first_tag_index < second_tag_index
        assert "None" not in buildx_section


def test_quiet_render_suppresses_phases_and_optional_result(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(
        host_cli,
        "prepare_render_context",
        _prepared_render_with_events,
    )

    result = cli_runner.invoke(
        app,
        [
            "--quiet",
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == result.stderr == ""


def test_render_preparation_interrupt_reports_the_current_phase(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )

    def interrupt_during_lock_reconciliation(*_args, **kwargs) -> None:
        event_sink = kwargs["event_sink"]
        event_sink.emit(HostPhaseCompleted(HostPhase.BUILD_INPUT_RESOLUTION))
        event_sink.emit(HostPhaseStarted(HostPhase.LOCK_RECONCILIATION))
        raise KeyboardInterrupt

    monkeypatch.setattr(
        host_cli,
        "prepare_render_context",
        interrupt_during_lock_reconciliation,
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(tmp_path / "context"),
        ],
    )

    output = _plain_output(result.stderr)
    assert result.exit_code == 130
    assert result.stdout == ""
    assert "Interrupted" in output
    assert "lock" in output.lower()
    assert "Build context rendered" not in output
    assert "Traceback" not in result.output


def test_verbose_render_shows_safe_coarse_provider_subphases(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(
        host_cli, "prepare_render_context", _prepared_render_with_events
    )

    result = cli_runner.invoke(
        app,
        [
            "--verbose",
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 0
    assert "Preparing the canonical cdh wheel" in result.stderr
    assert "Reconciling canonical identities" in result.stderr


def test_render_cleanup_failure_marks_the_retained_context_phase(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CleanupFailingSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            raise HostSecretSessionError("cleanup_failed")

        def git_binding(self):
            return None

        def drain_warnings(self):
            return ()

    session = CleanupFailingSession()
    monkeypatch.setattr(
        HostSecretSession,
        "from_configuration",
        classmethod(lambda _cls, _result: session),
    )
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(
        host_cli, "prepare_render_context", _prepared_render_with_events
    )
    config = tmp_path / "config.toml"
    _write_minimal_config(config)

    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Failed: Rendering or checking build context" in result.stderr
    assert "Error: Unable to access configured secrets" in result.stderr
    assert "private Secret session could not be cleaned up" in result.stderr


# Host render preserves input ownership while presenting planning warnings on stderr.
def test_render_passes_runtime_hook_inputs_and_presents_planning_warnings(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    loaded = []
    original_load = host_cli.load_validate_config_result

    @contextmanager
    def providers():
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    def prepare(*args, **kwargs):
        seen["runtime_hooks_dir"] = kwargs["runtime_hooks_dir"]
        seen["configuration_result"] = kwargs["configuration_result"]
        seen["build_hook_source_root"] = kwargs["build_hook_source_root"]
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(
            lock_result=accepted_resolution(),
            warnings=(
                Diagnostic(
                    ("runtime_hooks_dir",),
                    "runtime_hooks.ignored_top_level",
                    "ignored 1 ordinary top-level runtime hook entry",
                    DiagnosticSeverity.WARNING,
                ),
            ),
        )

    def load(*args, **kwargs):
        result = original_load(*args, **kwargs)
        loaded.append(result)
        return result

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers", providers
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.prepare_render_context", prepare
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.load_validate_config_result", load
    )
    hooks = tmp_path / "hooks"
    unused_build_hooks = tmp_path / "unused-build-hooks"
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(tmp_path / "context"),
            "--runtime-hooks-dir",
            str(hooks),
            "--build-hooks-dir",
            str(unused_build_hooks),
        ],
    )

    assert result.exit_code == 0
    assert "Build context rendered:" in result.stdout
    assert "(lock unchanged)" in result.stdout
    assert "ignored 1 ordinary top-level runtime hook entry" in result.stderr
    assert "runtime_hooks.ignored_top_level" not in result.stderr
    assert seen["runtime_hooks_dir"] == hooks
    assert len(loaded) == 1
    assert seen["configuration_result"] is loaded[0]
    assert seen["build_hook_source_root"] is None
    assert not unused_build_hooks.exists()


def test_render_materialization_error_is_one_short_diagnostic(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def providers():
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    def fail_prepare(*args, **kwargs):
        _complete_stubbed_preparation(kwargs)
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
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Unable to render build context" in result.stderr
    assert "Field: render" in result.stderr
    assert "context could not be written" in result.stderr
    assert "Failed: Rendering or checking build context" in result.stderr


def test_build_overrides_flow_through_plan_and_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    original_seal = host_cli.HostWorkflowDisplay.seal_for_external_stream

    def seal_for_external_stream(display) -> None:
        original_seal(display)
        seen["workflow_sealed"] = True

    @contextmanager
    def providers():
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

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
    output_plan = BuildxOutputPlan(
        tags=("cli:first", "cli:second"),
        output="push",
    )

    def prepare(*args, **kwargs):
        seen["runtime_hooks_dir"] = kwargs["runtime_hooks_dir"]
        configuration = kwargs["configuration_result"]
        seen["configuration_build"] = configuration.config.build
        seen["tag_templates"] = kwargs["tag_templates"]
        seen["output"] = kwargs["output_mode"]
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(
            plan=SimpleNamespace(
                toolchain=SimpleNamespace(platform="linux/amd64"),
                custom_nodes=SimpleNamespace(nodes=(), git_credentials=()),
                files=SimpleNamespace(files=(), credentials=()),
            ),
            output_plan=output_plan,
            warnings=(),
        )

    def buildx(**kwargs):
        assert seen.get("workflow_sealed") is True
        kwargs["log"]("external-buildkit-\x1b[31msentinel\x1b[0m")
        seen["buildx_ssh"] = (
            kwargs["forward_default_ssh"],
            kwargs["file_secret_bindings"],
        )
        seen["buildx"] = {
            "image_tags": kwargs["image_tags"],
            "output": kwargs["output"],
            "platforms": kwargs["platforms"],
            "context_dir": kwargs["context_dir"],
            "cache_from": kwargs["cache_from"],
            "cache_to": kwargs["cache_to"],
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
    monkeypatch.setattr(
        host_cli.HostWorkflowDisplay,
        "seal_for_external_stream",
        seal_for_external_stream,
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
            "--runtime-hooks-dir",
            str(hooks),
            "--tag",
            "cli:first",
            "--tag",
            "cli:second",
            "--push",
            "--cache-from",
            "type=local,src=/cache source",
            "--cache-to",
            "type=gha,scope=build",
        ],
    )

    assert result.exit_code == 0
    assert "In progress: Validating configuration" in result.stderr
    assert "In progress: Resolving build inputs" in result.stderr
    assert "Starting image build" in result.stderr
    plain_stdout = _plain_output(result.stdout)
    assert "Starting image build" not in plain_stdout
    assert plain_stdout.index("external-buildkit-sentinel") < plain_stdout.index(
        "Image build complete"
    )
    assert "external-buildkit-\x1b[31msentinel\x1b[0m" in result.stdout
    before_external, _, after_external = result.stdout.partition("external-buildkit-")
    _, _, after_external = after_external.partition("\x1b[0m")
    assert "\x1b" not in before_external + after_external
    assert seen["runtime_hooks_dir"] == hooks
    configuration_build = seen["configuration_build"]
    assert configuration_build.tags == ["config:test"]
    assert configuration_build.output == "load"
    assert configuration_build.platforms == ["linux/amd64"]
    assert seen["tag_templates"] == output_plan.tags
    assert seen["output"] == output_plan.output
    assert seen["buildx"] == {
        "image_tags": output_plan.tags,
        "output": output_plan.output,
        "platforms": ("linux/amd64",),
        "context_dir": context,
        "cache_from": "type=local,src=/cache source",
        "cache_to": "type=gha,scope=build",
    }
    assert seen["buildx_ssh"] == (False, ())


def test_quiet_build_preserves_raw_buildkit_without_cdh_framing(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(
        host_cli,
        "prepare_render_context",
        _prepared_build_with_events,
    )

    def buildx(**kwargs):
        kwargs["log"]("external-buildkit-\x1b[31msentinel\x1b[0m")

    monkeypatch.setattr(host_cli, "build_image_with_buildx", buildx)

    result = cli_runner.invoke(
        app,
        [
            "--quiet",
            "host",
            "build",
            "-f",
            str(config),
            "--context-dir",
            str(tmp_path / "context"),
            "--tag",
            "example:test",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "external-buildkit-\x1b[31msentinel\x1b[0m\n"
    assert result.stderr == ""


def test_build_cleanup_failure_preserves_buildkit_and_omits_completion(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CleanupFailingSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            raise HostSecretSessionError("cleanup_failed")

        def git_binding(self):
            return None

        def drain_warnings(self):
            return ()

    session = CleanupFailingSession()
    monkeypatch.setattr(
        HostSecretSession,
        "from_configuration",
        classmethod(lambda _cls, _result: session),
    )
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(host_cli, "prepare_render_context", _prepared_build_with_events)

    def buildx(**kwargs):
        kwargs["log"]("external-buildkit-cleanup-sentinel")

    monkeypatch.setattr(host_cli, "build_image_with_buildx", buildx)
    config = tmp_path / "config.toml"
    _write_minimal_config(config)

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
            "example:test",
        ],
    )

    assert result.exit_code == 1
    assert "external-buildkit-cleanup-sentinel" in result.stdout
    assert "Image build complete" not in result.stdout
    assert "Error: Unable to access configured secrets" in result.stderr
    assert "private Secret session could not be cleaned up" in result.stderr
    assert "Failed:" not in result.stderr


@pytest.mark.parametrize(
    "buildx_failure",
    [None, BuildxBuildError("synthetic Buildx failure"), KeyboardInterrupt()],
)
def test_build_fills_distinct_plan_credentials_and_reuses_provider_snapshot(
    buildx_failure: BaseException | None,
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_direct_git_config(config)
    with config.open("a") as stream:
        stream.write(
            """
[secrets.root_token]
env = "CDH_TEST_ROOT_TOKEN"
[secrets.team_token]
env = "CDH_TEST_TEAM_TOKEN"

[[cdh.git.credentials]]
match = "https://example.test/"
username = "root-user"
password = { secret = "root_token" }
[[cdh.git.credentials]]
match = "https://example.test/team/"
username = "team-user"
password = { secret = "root_token" }
[[cdh.git.credentials]]
match = "https://gitlab.example.test/"
username = "oauth2"
password = { secret = "team_token" }

[[cdh.downloader.credentials]]
match = "https://example.test/"
type = "bearer"
token = { secret = "root_token" }
"""
        )
    values = {
        "CDH_TEST_ROOT_TOKEN": "root-secret-marker",
        "CDH_TEST_TEAM_TOKEN": "team-secret-marker",
    }
    reads: list[str] = []
    for locator, value in values.items():
        monkeypatch.setenv(locator, value)
    read_environment_source = secret_session_module._read_environment_source

    def counting_environment_source(locator: str, name: str) -> bytes:
        reads.append(locator)
        return read_environment_source(locator, name)

    monkeypatch.setattr(
        secret_session_module,
        "_read_environment_source",
        counting_environment_source,
    )
    plan = _credential_build_plan(downloader=True)
    observed_root: Path | None = None
    captured_bindings: tuple[FileSecretBinding, ...] = ()

    @contextmanager
    def providers(*, git_credential_binding):
        nonlocal observed_root
        observed_root = Path(
            git_credential_binding.environment[GIT_CREDENTIAL_SESSION_ENV]
        )
        attached = HostSecretSession._attach(observed_root)
        attached.snapshot_git_credential("cdh-git-credential-root_token")
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    def prepare(*_args, **kwargs):
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(
            plan=plan,
            output_plan=BuildxOutputPlan(tags=("example:test",), output="load"),
            warnings=(),
        )

    # Buildx receives every effective route Secret because recursive submodule
    # origins are not all known when the host finishes provider planning.
    def buildx(**kwargs):
        nonlocal captured_bindings
        captured_bindings = tuple(kwargs["file_secret_bindings"])
        assert all(binding.source.is_file() for binding in captured_bindings)
        kwargs["log"]("external-buildkit-sentinel")
        if buildx_failure is not None:
            raise buildx_failure

    monkeypatch.setattr(host_cli, "default_planning_providers", providers)
    monkeypatch.setattr(host_cli, "prepare_render_context", prepare)
    monkeypatch.setattr(host_cli, "build_image_with_buildx", buildx)

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
            "example:test",
        ],
    )

    expected_exit_code = (
        130
        if isinstance(buildx_failure, KeyboardInterrupt)
        else (0 if buildx_failure is None else 1)
    )
    assert result.exit_code == expected_exit_code
    plain_stdout = _plain_output(result.stdout)
    assert "Starting image build" in result.stderr
    assert "Starting image build" not in plain_stdout
    assert "external-buildkit-sentinel" in plain_stdout
    if buildx_failure is None:
        assert plain_stdout.index("external-buildkit-sentinel") < plain_stdout.index(
            "Image build complete"
        )
    else:
        assert "Image build complete" not in plain_stdout
    if isinstance(buildx_failure, BuildxBuildError):
        assert "Error: Image build failed" in result.stderr
        assert "synthetic Buildx failure" in result.stderr
    assert [binding.secret_id for binding in captured_bindings] == [
        "cdh-git-credential-root_token",
        "cdh-git-credential-team_token",
        "cdh-downloader-credential-root_token",
    ]
    assert reads == ["CDH_TEST_ROOT_TOKEN", "CDH_TEST_TEAM_TOKEN"]
    assert observed_root is not None and not observed_root.exists()
    assert "CDH_TEST_ROOT_TOKEN" not in result.output
    assert "CDH_TEST_TEAM_TOKEN" not in result.output
    assert "root-secret-marker" not in result.output
    assert "team-secret-marker" not in result.output


def test_build_missing_plan_credential_fails_before_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_direct_git_config(config)
    missing_locator = "synthetic-missing-credential-source"
    with config.open("a") as stream:
        stream.write(
            f"""
[secrets.root_token]
file = "{missing_locator}"
[secrets.team_token]
env = "CDH_TEST_TEAM_TOKEN"

[[cdh.git.credentials]]
match = "https://example.test/"
username = "root-user"
password = {{ secret = "root_token" }}
[[cdh.git.credentials]]
match = "https://gitlab.example.test/"
username = "oauth2"
password = {{ secret = "team_token" }}
"""
        )
    plan = _credential_build_plan()
    observed_root: Path | None = None

    @contextmanager
    def providers(*, git_credential_binding):
        nonlocal observed_root
        observed_root = Path(
            git_credential_binding.environment[GIT_CREDENTIAL_SESSION_ENV]
        )
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    def prepare(*_args, **kwargs):
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(
            plan=plan,
            output_plan=BuildxOutputPlan(tags=("example:test",), output="load"),
            warnings=(),
        )

    monkeypatch.setattr(host_cli, "default_planning_providers", providers)
    monkeypatch.setattr(host_cli, "prepare_render_context", prepare)
    monkeypatch.setattr(
        host_cli,
        "build_image_with_buildx",
        lambda **_kwargs: pytest.fail("missing Secret reached Docker boundary"),
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
            "example:test",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Unable to access configured secrets" in result.stderr
    assert "Field: secrets.root_token" in result.stderr
    assert "the configured source is unavailable" in result.stderr
    assert missing_locator not in result.stderr
    assert observed_root is not None and not observed_root.exists()


def test_build_invalid_downloader_bearer_fails_content_safe_before_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    with config.open("a") as stream:
        stream.write(
            """
[secrets.root_token]
env = "CDH_TEST_ROOT_TOKEN"

[[cdh.downloader.credentials]]
match = "https://example.test/"
type = "bearer"
token = { secret = "root_token" }

[[files]]
type = "http"
url = "https://example.test/model.bin"
target_dir = "models/checkpoints"
filename = "model.bin"
downloader = "httpx"
"""
        )
    marker = "synthetic sensitive marker with spaces"
    monkeypatch.setenv("CDH_TEST_ROOT_TOKEN", marker)
    plan = _credential_build_plan(downloader=True)
    plan = plan.model_copy(
        update={
            "custom_nodes": plan.custom_nodes.model_copy(update={"git_credentials": ()})
        }
    )

    @contextmanager
    def providers():
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    def prepare(*_args, **kwargs):
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(
            plan=plan,
            output_plan=BuildxOutputPlan(tags=("example:test",), output="load"),
            warnings=(),
        )

    monkeypatch.setattr(host_cli, "default_planning_providers", providers)
    monkeypatch.setattr(host_cli, "prepare_render_context", prepare)
    monkeypatch.setattr(
        host_cli,
        "build_image_with_buildx",
        lambda **_kwargs: pytest.fail("invalid Bearer reached Docker boundary"),
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
            "example:test",
        ],
    )

    assert result.exit_code == 1
    assert "Error: Unable to access configured secrets" in result.stderr
    assert "Field: secrets.root_token" in result.stderr
    assert "one exact RFC 6750 Bearer token" in result.stderr
    assert marker not in result.output


def test_render_keeps_unused_downloader_secret_value_lazy(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    with config.open("a") as stream:
        stream.write(
            """
[secrets.unused_token]
env = "CDH_TEST_MISSING_TOKEN"

[[cdh.downloader.credentials]]
match = "https://example.test/private"
type = "bearer"
token = { secret = "unused_token" }
"""
        )
    monkeypatch.delenv("CDH_TEST_MISSING_TOKEN", raising=False)

    @contextmanager
    def providers():
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    monkeypatch.setattr(host_cli, "default_planning_providers", providers)

    def prepare(*_args, **kwargs):
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(
            warnings=(),
            lock_result=SimpleNamespace(changed=False),
        )

    monkeypatch.setattr(
        host_cli,
        "prepare_render_context",
        prepare,
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 0
    assert "CDH_TEST_MISSING_TOKEN" not in result.output


def test_render_secret_failure_presentation_explains_invalid_value_requirements(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    secret_locator = "CDH_TEST_PRIVATE_GIT"
    with config.open("a") as stream:
        stream.write(f'\n[secrets.private_git]\nenv = "{secret_locator}"\n')

    def fail_session(_result):
        raise HostSecretSessionError("invalid_value", "private_git")

    monkeypatch.setattr(
        secret_session_module,
        "HostSecretSession",
        SimpleNamespace(from_configuration=fail_session),
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Unable to access configured secrets" in result.stderr
    assert "Field: secrets.private_git" in result.stderr
    assert "must be non-empty" in result.stderr
    assert "no more than 65,525 bytes" in result.stderr
    assert "NUL, carriage return, or newline characters" in result.stderr
    assert secret_locator not in result.stderr


@pytest.mark.parametrize(
    ("cache_args", "expected_option", "expected_message"),
    [
        (
            ["--cache-from", "first", "--cache-from", "second"],
            "--cache-from",
            "may be provided at most once",
        ),
        (
            ["--cache-to", "first", "--cache-to", "second"],
            "--cache-to",
            "may be provided at most once",
        ),
        (
            ["--cache-from", ""],
            "--cache-from",
            "must be non-empty and must not contain control characters",
        ),
        (
            ["--cache-from", "   "],
            "--cache-from",
            "must be non-empty and must not contain control characters",
        ),
        (
            ["--cache-to", "   "],
            "--cache-to",
            "must be non-empty and must not contain control characters",
        ),
        (
            ["--cache-to", "type=local,dest=/cache\nbroken"],
            "--cache-to",
            "must be non-empty and must not contain control characters",
        ),
    ],
)
def test_build_cache_options_fail_before_provider_work(
    cache_args: list[str],
    expected_option: str,
    expected_message: str,
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)

    def forbidden_providers():
        raise AssertionError("invalid cache input reached provider work")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers",
        forbidden_providers,
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "--tag",
            "image:test",
            *cache_args,
        ],
    )

    assert result.exit_code == 2
    plain_output = _plain_output(result.output)
    assert expected_option in plain_output
    normalized = " ".join(plain_output.replace("│", " ").split())
    assert expected_message in normalized
    assert "invalid cache input reached provider work" not in plain_output


@pytest.mark.parametrize(
    ("cache_args", "expected_cache_from", "expected_cache_to"),
    [
        (["--cache-from", "type=local,src=/cache"], "type=local,src=/cache", None),
        (["--cache-to", "type=gha,scope=build"], None, "type=gha,scope=build"),
    ],
)
def test_build_cache_import_and_export_are_independent(
    cache_args: list[str],
    expected_cache_from: str | None,
    expected_cache_to: str | None,
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    output_plan = BuildxOutputPlan(
        tags=("image:test",),
        output="load",
    )

    @contextmanager
    def providers():
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    def prepare(*args, **kwargs):
        del args
        assert kwargs["tag_templates"] == output_plan.tags
        assert kwargs["output_mode"] == output_plan.output
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(
            plan=SimpleNamespace(
                toolchain=SimpleNamespace(platform="linux/amd64"),
                custom_nodes=SimpleNamespace(nodes=(), git_credentials=()),
                files=SimpleNamespace(files=(), credentials=()),
            ),
            output_plan=output_plan,
            warnings=(),
        )

    def buildx(**kwargs):
        seen["cache_from"] = kwargs["cache_from"]
        seen["cache_to"] = kwargs["cache_to"]

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers", providers
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.prepare_render_context", prepare
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx", buildx
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "--tag",
            "image:test",
            *cache_args,
        ],
    )

    assert result.exit_code == 0
    assert seen == {
        "cache_from": expected_cache_from,
        "cache_to": expected_cache_to,
    }


# SSH build admission preserves opt-in defaults, validation order, and
# host-data isolation.
def test_build_ssh_without_direct_git_warns_once_and_passes_no_capability(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def prepare(*args, **kwargs):
        del args
        _complete_stubbed_preparation(kwargs)
        return _prepared_build()

    def buildx(**kwargs):
        seen.update(kwargs)

    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(host_cli, "prepare_render_context", prepare)
    monkeypatch.setattr(host_cli, "build_image_with_buildx", buildx)
    monkeypatch.setattr(
        host_cli,
        "_collect_default_known_hosts_bindings",
        lambda: pytest.fail("ignored --ssh must not collect known-hosts sources"),
    )
    config = tmp_path / "config.toml"
    _write_registry_config(config)

    result = cli_runner.invoke(
        app,
        _build_ssh_args(config, context=tmp_path / "context"),
    )

    warning = (
        "Warning: --ssh ignored because the effective configuration has no "
        "direct-Git custom nodes"
    )
    assert result.exit_code == 0
    assert result.stderr.count(warning) == 1
    assert "Starting image build" in result.stderr
    assert "Starting image build" not in result.stdout
    assert "Image build complete" in result.stdout
    assert seen["forward_default_ssh"] is False
    assert seen["file_secret_bindings"] == ()


def test_build_ssh_does_not_replace_configuration_validation(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_providers():
        pytest.fail("invalid configuration must fail before planning providers")

    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(host_cli, "default_planning_providers", fail_providers)
    config = tmp_path / "invalid.toml"
    config.write_text("unknown = true\n")

    result = cli_runner.invoke(
        app,
        _build_ssh_args(config),
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Configuration is invalid" in result.stderr
    assert "Field: unknown" in result.stderr
    assert "SSH_AUTH_SOCK" not in result.stderr


@pytest.mark.parametrize("agent_socket", [None, ""])
def test_build_ssh_on_posix_requires_nonempty_agent_before_planning_providers(
    agent_socket: str | None,
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_providers():
        pytest.fail("SSH input admission must precede planning providers")

    if agent_socket is None:
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    else:
        monkeypatch.setenv("SSH_AUTH_SOCK", agent_socket)
    monkeypatch.setattr(host_cli, "_platform_name", "posix")
    monkeypatch.setattr(host_cli, "default_planning_providers", fail_providers)
    config = tmp_path / "config.toml"
    _write_direct_git_config(config)

    result = cli_runner.invoke(
        app,
        _build_ssh_args(config),
    )

    assert result.exit_code == 2
    output = _plain_output(result.output)
    assert "Invalid value for --ssh" in output
    assert "non-empty SSH_AUTH_SOCK" in output
    assert "Failed: Resolving build inputs" in output
    assert "Failed: Validating configuration" not in output


def test_build_ssh_on_windows_delegates_default_agent_to_buildkit(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def buildx(**kwargs):
        seen.update(kwargs)

    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(host_cli, "_platform_name", "nt")
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(host_cli, "prepare_render_context", _prepared_build_with_events)
    monkeypatch.setattr(host_cli, "build_image_with_buildx", buildx)
    monkeypatch.setattr(
        host_cli,
        "_collect_default_known_hosts_bindings",
        lambda: (),
    )
    config = tmp_path / "config.toml"
    _write_direct_git_config(config)

    result = cli_runner.invoke(
        app,
        _build_ssh_args(config, context=tmp_path / "context"),
    )

    assert result.exit_code == 0
    assert seen["forward_default_ssh"] is True


@pytest.mark.parametrize("existing_indexes", [(0, 3), ()])
def test_build_ssh_forwards_agent_and_only_existing_default_trust_after_context(
    existing_indexes: tuple[int, ...],
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    checked_sources: list[Path] = []
    original_exists = Path.exists

    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
    monkeypatch.setattr(host_cli, "_platform_name", "posix")
    agent_socket = tmp_path / "not-a-real-agent-socket"
    monkeypatch.setenv("SSH_AUTH_SOCK", str(agent_socket))
    default_sources = tuple(
        Path(descriptor.default_source).expanduser()
        for descriptor in KNOWN_HOSTS_MOUNTS
    )
    existing_sources = {default_sources[index] for index in existing_indexes}

    def selective_exists(path: Path) -> bool:
        if path in default_sources:
            checked_sources.append(path)
            return path in existing_sources
        return original_exists(path)

    def prepare(*args, **kwargs):
        del args
        assert checked_sources == []
        _complete_stubbed_preparation(kwargs)
        return _prepared_build()

    def buildx(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(Path, "exists", selective_exists)
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(host_cli, "prepare_render_context", prepare)
    monkeypatch.setattr(host_cli, "build_image_with_buildx", buildx)
    config = tmp_path / "config.toml"
    _write_direct_git_config(config)

    result = cli_runner.invoke(
        app,
        _build_ssh_args(config),
    )

    assert result.exit_code == 0
    assert seen["forward_default_ssh"] is True
    assert seen["file_secret_bindings"] == tuple(
        FileSecretBinding(
            secret_id=KNOWN_HOSTS_MOUNTS[index].secret_id,
            source=default_sources[index],
        )
        for index in existing_indexes
    )
    assert checked_sources == list(default_sources)
    assert agent_socket not in checked_sources


def test_windows_known_hosts_collection_uses_only_user_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_sources: list[Path] = []
    host_home = tmp_path / "Windows User"
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setattr(host_cli, "_platform_name", "nt")
    user_sources = tuple(
        Path(descriptor.default_source).expanduser()
        for descriptor in KNOWN_HOSTS_MOUNTS
        if descriptor.scope == "user"
    )

    def source_exists(path: Path) -> bool:
        checked_sources.append(path)
        return path == user_sources[0]

    monkeypatch.setattr(Path, "exists", source_exists)

    bindings = host_cli._collect_default_known_hosts_bindings()

    assert checked_sources == list(user_sources)
    assert bindings == (
        FileSecretBinding(
            secret_id=KNOWN_HOSTS_MOUNTS[0].secret_id,
            source=user_sources[0],
        ),
    )


def test_build_ssh_host_sources_enter_only_buildx_bindings(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    context = tmp_path / "context"
    host_home = tmp_path / "host-source-marker"
    user_ssh = host_home / ".ssh"
    user_ssh.mkdir(parents=True)
    known_hosts = user_ssh / "known_hosts"
    known_hosts.write_text("host-trust-content-marker")
    agent_socket = host_home / "agent-socket-marker"
    seen: dict[str, object] = {}

    def prepare(output_dir, **kwargs):
        output_plan = BuildxOutputPlan(
            tags=tuple(kwargs["tag_templates"]), output=kwargs["output_mode"]
        )
        Path(output_dir).mkdir(mode=0o700)
        _materialize_private_stage(
            plan,
            output_dir,
            canonical_wheel=canonical_wheel(),
        )
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(plan=plan, output_plan=output_plan, warnings=())

    def buildx(**kwargs):
        seen.update(kwargs)

    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("USERPROFILE", str(host_home))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(agent_socket))
    monkeypatch.setattr(
        host_cli, "default_planning_providers", _stub_planning_providers
    )
    monkeypatch.setattr(host_cli, "prepare_render_context", prepare)
    monkeypatch.setattr(host_cli, "build_image_with_buildx", buildx)
    config = tmp_path / "config.toml"
    _write_direct_git_config(config)

    result = cli_runner.invoke(
        app,
        _build_ssh_args(config, context=context),
    )

    assert result.exit_code == 0
    assert (
        FileSecretBinding(
            secret_id=KNOWN_HOSTS_MOUNTS[0].secret_id,
            source=known_hosts,
        )
        in seen["file_secret_bindings"]
    )
    source_markers = (str(host_home).encode(), b"host-trust-content-marker")
    for path in context.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert all(marker not in content for marker in source_markers)


def test_locked_build_override_does_not_mutate_image_configuration(
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
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    def prepare(*args, **kwargs):
        configuration = kwargs["configuration_result"]
        assert configuration.config.build.tags == ["config:test"]
        assert kwargs["options"].locked is True
        assert kwargs["tag_templates"] == ("cli:test",)
        assert kwargs["output_mode"] == "load"
        output_plan = BuildxOutputPlan(tags=("cli:test",), output="load")
        _complete_stubbed_preparation(kwargs)
        return SimpleNamespace(
            plan=SimpleNamespace(
                toolchain=SimpleNamespace(platform="linux/amd64"),
                custom_nodes=SimpleNamespace(nodes=(), git_credentials=()),
                files=SimpleNamespace(files=(), credentials=()),
            ),
            output_plan=output_plan,
            warnings=(),
        )

    def buildx(**kwargs):
        assert kwargs["image_tags"] == ("cli:test",)
        assert kwargs["output"] == "load"
        assert kwargs["platforms"] == ("linux/amd64",)

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.default_planning_providers", providers
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.prepare_render_context", prepare
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx", buildx
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

    assert result.exit_code == 0


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
    assert usage in _plain_output(result.output)


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


def test_application_error_boundary_escapes_terminal_controls(
    cli_runner: CliRunner,
) -> None:
    probe_app = typer.Typer(cls=ApplicationGroup)

    @probe_app.callback()
    def probe() -> None:
        """Keep the probe as a group so its custom group class is exercised."""

    @probe_app.command()
    def fail() -> None:
        raise ApplicationError("expected\nforged\r\t\x1b\\suffix", exit_code=7)

    result = cli_runner.invoke(probe_app, ["fail"])

    assert result.exit_code == 7
    assert result.stdout == ""
    assert result.stderr == ("Error: expected\\nforged\\r\\t\\x1b\\\\suffix\n")
    assert "\x1b" not in result.stderr
    assert result.stderr.count("\n") == 1


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
