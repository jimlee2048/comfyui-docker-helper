"""Linux image-helper CLI execution contracts."""

import json
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.config.build_plan import build_plan_digest
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.container import build_plan_input as build_plan_input_module
from comfyui_docker_helper.container import cli as container_cli
from comfyui_docker_helper.container import download_files as download_files_module
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


@pytest.mark.parametrize(
    ("args", "usage"),
    [
        (["download-files"], "Usage: cdh container download-files"),
        (["install-comfyui"], "Usage: cdh container install-comfyui"),
        (["install-custom-nodes"], "Usage: cdh container install-custom-nodes"),
        (["emit-final-manifest"], "Usage: cdh container emit-final-manifest"),
        (["entrypoint"], "Usage: cdh container entrypoint"),
    ],
)
@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_container_helper_help_succeeds(
    cli_runner: CliRunner,
    args: list[str],
    usage: str,
    help_flag: str,
) -> None:
    result = cli_runner.invoke(app, ["container", *args, help_flag])

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
