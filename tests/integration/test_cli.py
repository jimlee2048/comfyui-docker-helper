"""Smoke tests for the CLI skeleton and shared error boundary."""

import inspect
import json
from contextlib import contextmanager
from importlib.metadata import entry_points, version
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from rich.text import Text
from tests.build_plan_support import (
    accepted_resolution,
    build_plan,
    canonical_wheel,
    final_config,
)
from typer.main import get_command
from typer.testing import CliRunner

from comfyui_docker_helper.build_ssh import KNOWN_HOSTS_MOUNTS
from comfyui_docker_helper.cli import app
from comfyui_docker_helper.config.build_plan import build_plan_digest
from comfyui_docker_helper.config.canonical_resolver import CanonicalAcquisitionError
from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticSeverity
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.container import build_plan_input as build_plan_input_module
from comfyui_docker_helper.container import cli as container_cli
from comfyui_docker_helper.container import download_files as download_files_module
from comfyui_docker_helper.errors import ApplicationError, ApplicationGroup
from comfyui_docker_helper.host import cli as host_cli
from comfyui_docker_helper.host.buildx import BuildxOutputPlan, KnownHostsBinding
from comfyui_docker_helper.host.render_service import HostRenderServiceError
from comfyui_docker_helper.host.secret_session import (
    GIT_CREDENTIAL_SESSION_ENV,
    HostSecretSessionError,
)
from comfyui_docker_helper.rendering.final_materializer import (
    _materialize_private_stage,
)


def _plain_output(output: str) -> str:
    return Text.from_ansi(output).plain


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
        warnings=(),
        plan=SimpleNamespace(
            toolchain=SimpleNamespace(platform="linux/amd64"),
        ),
        output_plan=BuildxOutputPlan(tags=("example:test",), output="load"),
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
        (
            ["container", "emit-final-manifest"],
            "Usage: cdh container emit-final-manifest",
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
    assert usage in _plain_output(result.output)


def test_container_helper_help_exposes_build_plan_binding(
    cli_runner: CliRunner,
) -> None:
    """Container build helpers require the Dockerfile-bound plan digest."""
    result = cli_runner.invoke(app, ["container", "download-files", "--help"])

    assert result.exit_code == 0
    assert "--build-plan-digest" in _plain_output(result.output)


def test_registry_helper_help_exposes_only_owned_inputs(
    cli_runner: CliRunner,
) -> None:
    result = cli_runner.invoke(
        app,
        ["container", "install-custom-nodes", "--help"],
    )

    assert result.exit_code == 0
    output = _plain_output(result.output)
    assert "--build-plan-digest" in output
    assert "--constraints" in output
    assert "--build-hooks-directory" in output


@pytest.mark.parametrize(
    "command",
    [
        "download-files",
        "install-comfyui",
        "install-custom-nodes",
        "emit-final-manifest",
    ],
)
def test_container_commands_admit_one_canonical_plan_per_invocation(
    command: str,
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parse one plan and pass only each command's typed projection."""
    plan = build_plan(final_config(), accepted_resolution())
    context = tmp_path / "context"
    context.mkdir(mode=0o700)
    _materialize_private_stage(plan, context, canonical_wheel=canonical_wheel())
    parse_count = 0
    parse = build_plan_input_module.parse_build_plan_json

    def counted_parse(document):
        nonlocal parse_count
        parse_count += 1
        return parse(document)

    observed: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        build_plan_input_module,
        "parse_build_plan_json",
        counted_parse,
    )
    monkeypatch.setattr(
        container_cli, "MATERIALIZED_BUILD_PLAN_PATH", context / "build-plan.json"
    )
    monkeypatch.setattr(
        container_cli,
        "download_files",
        lambda files, root: observed.append((files, root)),
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
    monkeypatch.setattr(
        container_cli,
        "emit_final_manifest",
        lambda projection, **_kwargs: observed.append((projection,)),
    )
    monkeypatch.setenv("WORKSPACE", plan.application.paths.workspace)
    monkeypatch.setenv("COMFYUI_PATH", plan.application.paths.comfyui)
    monkeypatch.setenv("VIRTUAL_ENV", plan.application.paths.venv)
    result = cli_runner.invoke(
        app,
        [
            "container",
            command,
            "--build-plan-digest",
            build_plan_digest(plan),
        ],
    )

    assert result.exit_code == 0, result.output
    assert parse_count == 1
    assert (
        observed
        == {
            "download-files": [
                (plan.files, plan.application.paths.comfyui),
            ],
            "install-comfyui": [(plan.application, plan.toolchain)],
            "install-custom-nodes": [(plan.custom_nodes, plan.application)],
            "emit-final-manifest": [
                (
                    build_plan_input_module.FinalManifestInput(
                        binding=build_plan_input_module.manifest_binding(plan),
                        toolchain=plan.toolchain,
                        application=plan.application,
                        custom_nodes=plan.custom_nodes,
                        files=tuple(
                            build_plan_input_module.FinalManifestFileInput(
                                url=item.url,
                                target=item.target,
                                checksum=item.checksum,
                            )
                            for item in plan.files.files
                        ),
                        materialized_hooks=(),
                        final_probe=build_plan_input_module.FinalCoreProbeInput(
                            workspace=plan.application.paths.comfyui,
                            checks=build_plan_input_module.final_build_check_ids(
                                tuple(
                                    package.name
                                    for package in plan.application.pytorch.packages
                                ),
                                manager_enabled=(
                                    plan.application.comfyui.manager is not None
                                ),
                            ),
                        ),
                        shutdown_timeout=plan.runtime.shutdown_timeout,
                    ),
                )
            ],
        }[command]
    )


def test_download_files_executes_authenticated_plan_with_custom_root(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry a custom root through materialization, admission, and download."""
    document = final_config().model_dump(mode="python")
    custom_root = tmp_path / "custom workspace"
    document["system"]["workspace"] = str(custom_root.parent)
    document["system"]["comfyui_path"] = str(custom_root)
    document["cdh"]["default_downloader"] = "httpx"
    config = FinalConfig.model_validate(document)
    plan = build_plan(config, accepted_resolution())
    custom_root.mkdir(parents=True)
    context = tmp_path / "context"
    context.mkdir(mode=0o700)
    _materialize_private_stage(plan, context, canonical_wheel=canonical_wheel())

    class WritingBackend:
        def download(self, item, settings):
            del settings
            with item.sink.open_for_write() as output:
                output.write(b"authenticated-plan")
            return download_files_module.TransportSuccess(
                length=len(b"authenticated-plan"),
                namespace="httpx",
                http_status=200,
            )

    monkeypatch.setattr(
        container_cli, "MATERIALIZED_BUILD_PLAN_PATH", context / "build-plan.json"
    )
    monkeypatch.setattr(
        download_files_module,
        "HttpxDownloader",
        lambda **_kwargs: WritingBackend(),
    )

    result = cli_runner.invoke(
        app,
        [
            "container",
            "download-files",
            "--build-plan-digest",
            build_plan_digest(plan),
        ],
    )

    assert result.exit_code == 0, result.output
    target = custom_root / "models" / "checkpoints" / "model.safetensors"
    assert target.read_bytes() == b"authenticated-plan"


# CLI admission reports canonical-plan failures without disclosing plan values.
def test_container_plan_admission_hides_invalid_plan_secret_values(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "password-sentinel-do-not-print"
    plan = build_plan(final_config(), accepted_resolution())
    context = tmp_path / "context"
    context.mkdir(mode=0o700)
    _materialize_private_stage(plan, context, canonical_wheel=canonical_wheel())
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
            "--build-plan-digest",
            build_plan_digest(plan),
        ],
    )

    assert result.exit_code == 1
    assert result.output == "Error: canonical BuildPlan is invalid\n"
    assert sentinel not in result.output
    assert sentinel not in str(result.exception)


def test_container_cli_rejects_registry_without_manager(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    context = tmp_path / "context"
    context.mkdir(mode=0o700)
    _materialize_private_stage(plan, context, canonical_wheel=canonical_wheel())
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
    assert "hook.build_hooks_dir_required" in result.output
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
    assert "render.input_output_overlap" in result.output
    assert "build hook source" in result.output
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
    assert "Unable to process configuration" in result.output
    assert "hook.build_hooks_dir_not_directory" in result.output
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

    def prepare(_output_dir, *, build_hook_source_root, **_kwargs):
        observed.append(build_hook_source_root)
        return SimpleNamespace(warnings=())

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
    assert result.output == ""


@pytest.mark.parametrize("command", ["validate", "render", "build"])
def test_http_credential_warning_precedes_provider_or_docker_initialization(
    command: str,
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
    args = ["host", command, "-f", str(config)]
    if command == "render":
        args.extend(("-o", str(tmp_path / "context")))
    elif command == "build":
        args.extend(("-t", "example:test", "--context-dir", str(tmp_path / "context")))

    result = cli_runner.invoke(app, args)

    assert result.output.count("git_credential.insecure_http") == 1
    if command == "validate":
        assert result.exit_code == 0
        assert result.exception is None
    else:
        assert isinstance(result.exception, BoundaryReached)


def test_render_emits_secret_warning_before_cleanup_failure(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warning = Diagnostic(
        ("secrets", "private_git", "file"),
        "secret.permissive_file_mode",
        "Secret file has group or world permission bits set",
        DiagnosticSeverity.WARNING,
    )

    class FailingCleanupSession:
        drained = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            raise HostSecretSessionError("cleanup_failed")

        def git_binding(self):
            return None

        def drain_warnings(self):
            if self.drained:
                return ()
            self.drained = True
            return (warning,)

    @contextmanager
    def providers():
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    def prepare(*_args, **_kwargs):
        return SimpleNamespace(warnings=())

    monkeypatch.setattr(
        host_cli.HostSecretSession,
        "from_configuration",
        lambda _result: FailingCleanupSession(),
    )
    monkeypatch.setattr(host_cli, "default_planning_providers", providers)
    monkeypatch.setattr(host_cli, "prepare_render_context", prepare)
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
    assert result.output.count("secret.permissive_file_mode") == 1
    assert "secret.cleanup_failed" in result.output


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
    assert "lock.resolve_failed" in result.output
    assert "identity provider request failed" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("selector", "cli_tags", "expected_code"),
    [
        (
            "0.11.0",
            ["example/image:${{comfyui.commit}}"],
            "build.invalid_tag_expression",
        ),
        (
            "0.11.0",
            ["busybox:x", "docker.io/library/busybox:x"],
            "build.duplicate_tag",
        ),
        (
            "nightly",
            ["example/image:v${{ comfyui.release }}"],
            "build.release_unavailable",
        ),
        (
            "1111111111111111111111111111111111111111",
            ["example/image:v${{ comfyui.release }}"],
            "build.release_unavailable",
        ),
    ],
)
def test_cli_tags_use_shared_static_validation_before_provider_construction(
    selector: str,
    cli_tags: list[str],
    expected_code: str,
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
    assert expected_code in _plain_output(result.output)


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
    assert "build.invalid_image_reference" in result.output


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
        return SimpleNamespace(
            warnings=(),
            plan=plan,
            lock_result=resolution,
            output_plan=output_plan,
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

    plain = _plain_output(result.output)
    assert result.exit_code == 0
    assert plain.index("Custom nodes:") < plain.index("Buildx output")
    if output_plan is None:
        assert "Buildx output\n  None" in plain
    else:
        assert "Buildx output\n  Mode: push\n  Tags:" in plain
        assert plain.index(output_plan.tags[0]) < plain.index(output_plan.tags[1])


def test_render_passes_runtime_hooks_dir_through_current_planning_boundary(
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
        return SimpleNamespace(warnings=())

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
    assert seen["runtime_hooks_dir"] == hooks
    assert len(loaded) == 1
    assert seen["configuration_result"] is loaded[0]
    assert seen["build_hook_source_root"] is None
    assert not unused_build_hooks.exists()


def test_render_materialization_error_is_short_and_has_no_traceback(
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
    assert "Unable to process configuration" in result.output
    assert "render.context_write_failed" in result.output
    assert "Traceback" not in result.output


def test_build_overrides_flow_through_plan_and_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

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
        return SimpleNamespace(
            warnings=(),
            plan=SimpleNamespace(toolchain=SimpleNamespace(platform="linux/amd64")),
            output_plan=output_plan,
        )

    def buildx(**kwargs):
        seen["buildx_ssh"] = (
            kwargs["forward_default_ssh"],
            kwargs["known_hosts_bindings"],
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


def test_build_keeps_one_secret_session_alive_through_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    _write_minimal_config(config)
    with config.open("a") as stream:
        stream.write(
            """
[secrets.private_git]
env = "CDH_TEST_PRIVATE_GIT_TOKEN"

[[cdh.git.credentials]]
match = "https://example.test/team/"
username = "token-user"
password = { secret = "private_git" }
"""
        )
    observed_root: Path | None = None

    @contextmanager
    def providers(*, git_credential_binding):
        nonlocal observed_root
        observed_root = Path(
            git_credential_binding.environment[GIT_CREDENTIAL_SESSION_ENV]
        )
        assert observed_root.is_dir()
        yield SimpleNamespace(
            acquirer=object(),
            local_acquirer=object(),
            canonical_wheel=canonical_wheel(),
        )

    def prepare(*_args, **_kwargs):
        assert observed_root is not None and observed_root.is_dir()
        return _prepared_build()

    def buildx(**_kwargs):
        assert observed_root is not None and observed_root.is_dir()

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

    assert result.exit_code == 0
    assert observed_root is not None
    assert not observed_root.exists()


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
        return SimpleNamespace(
            warnings=(),
            plan=SimpleNamespace(toolchain=SimpleNamespace(platform="linux/amd64")),
            output_plan=output_plan,
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
        del args, kwargs
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
        "direct-Git custom nodes."
    )
    assert result.exit_code == 0
    assert result.output.count(warning) == 1
    assert seen["forward_default_ssh"] is False
    assert seen["known_hosts_bindings"] == ()


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
    assert "Unable to process configuration" in result.output
    assert "SSH_AUTH_SOCK" not in result.output


@pytest.mark.parametrize("agent_socket", [None, ""])
def test_build_ssh_requires_nonempty_agent_before_planning_providers(
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
    monkeypatch.setattr(host_cli, "default_planning_providers", fail_providers)
    config = tmp_path / "config.toml"
    _write_direct_git_config(config)

    result = cli_runner.invoke(
        app,
        _build_ssh_args(config),
    )

    assert result.exit_code == 2
    assert "Invalid value for --ssh" in _plain_output(result.output)
    assert "non-empty SSH_AUTH_SOCK" in _plain_output(result.output)


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
        del args, kwargs
        assert checked_sources == []
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
    assert seen["known_hosts_bindings"] == tuple(
        KnownHostsBinding(
            secret_id=KNOWN_HOSTS_MOUNTS[index].secret_id,
            source=default_sources[index],
        )
        for index in existing_indexes
    )
    assert checked_sources == list(default_sources)
    assert agent_socket not in checked_sources


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
        return SimpleNamespace(warnings=(), plan=plan, output_plan=output_plan)

    def buildx(**kwargs):
        seen.update(kwargs)

    monkeypatch.setenv("HOME", str(host_home))
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
        KnownHostsBinding(
            secret_id=KNOWN_HOSTS_MOUNTS[0].secret_id,
            source=known_hosts,
        )
        in seen["known_hosts_bindings"]
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
        return SimpleNamespace(
            warnings=(),
            plan=SimpleNamespace(toolchain=SimpleNamespace(platform="linux/amd64")),
            output_plan=output_plan,
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
