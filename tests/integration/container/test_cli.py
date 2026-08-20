"""Linux image-helper CLI execution contracts."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.text import Text
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.cli_output import CliOutputSettings, OutputDetail
from comfyui_docker_helper.config.build_plan import HttpFilePlan, build_plan_digest
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.container import build_plan_input as build_plan_input_module
from comfyui_docker_helper.container import cli as container_cli
from comfyui_docker_helper.container import download_files as download_files_module
from comfyui_docker_helper.container.download_events import (
    DownloadAttemptStarted,
    DownloadBackendName,
    DownloadBatchCompleted,
    DownloadFinalVerificationCompleted,
    DownloadFinalVerificationStarted,
    DownloadItemCompleted,
    DownloadItemStarted,
    DownloadItemStatus,
    DownloadPlacementCompleted,
    DownloadPlacementStarted,
    DownloadRetryReason,
    DownloadRetryScheduled,
    DownloadVerificationCompleted,
    DownloadVerificationStarted,
)
from comfyui_docker_helper.container.helper_events import (
    ComfyUIInstallCompleted,
    ContainerHelperPhase,
    ContainerHelperPhaseCompleted,
    ContainerHelperPhaseStarted,
    CustomNodesInstallCompleted,
    RegistryCustomNodeStarted,
)
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.transfer_core import DownloadFilesError
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


def _materialized_download_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    plan = build_plan(final_config(), accepted_resolution())
    context = tmp_path / "download-context"
    context.mkdir(mode=0o700)
    _materialize_private_stage(plan, context, canonical_wheel=canonical_wheel())
    monkeypatch.setattr(
        container_cli,
        "MATERIALIZED_BUILD_PLAN_PATH",
        context / "build-plan.json",
    )
    return plan


def _emit_download_success(event_sink, *, retry: bool = False) -> None:
    event_sink.emit(
        DownloadItemStarted(
            index=1,
            total=1,
            target="models/checkpoints/model.safetensors",
            backend=DownloadBackendName.HTTPX,
            max_attempts=3,
            checksum_expected=True,
        )
    )
    event_sink.emit(DownloadAttemptStarted(1))
    if retry:
        event_sink.emit(
            DownloadRetryScheduled(
                failed_attempt=1,
                next_attempt=2,
                delay_seconds=1,
                reason=DownloadRetryReason.TEMPORARY_SERVER,
                http_status=503,
            )
        )
        event_sink.emit(DownloadAttemptStarted(2))
    event_sink.emit(DownloadVerificationStarted())
    event_sink.emit(DownloadVerificationCompleted())
    event_sink.emit(DownloadPlacementStarted())
    event_sink.emit(DownloadPlacementCompleted())
    event_sink.emit(
        DownloadItemCompleted(
            status=DownloadItemStatus.DOWNLOADED,
            observed_bytes=1024,
            checksum_verified=True,
        )
    )
    event_sink.emit(DownloadFinalVerificationStarted(item_count=1, checksum_count=1))
    event_sink.emit(DownloadFinalVerificationCompleted())
    event_sink.emit(DownloadBatchCompleted(item_count=1, checksum_verified_count=1))


def _emit_comfyui_success(event_sink) -> None:
    phase = ContainerHelperPhase.COMFYUI_SOURCE_CHECKOUT
    event_sink.emit(ContainerHelperPhaseStarted(phase))
    event_sink.emit(ContainerHelperPhaseCompleted(phase))
    event_sink.emit(ComfyUIInstallCompleted())


@pytest.mark.parametrize(
    ("args", "usage"),
    [
        (["download-files"], "Usage: cdh container download-files"),
        (["install-comfyui"], "Usage: cdh container install-comfyui"),
        (["install-custom-nodes"], "Usage: cdh container install-custom-nodes"),
        (["emit-final-manifest"], "Usage: cdh container emit-final-manifest"),
        (["runtime"], "Usage: cdh container runtime"),
        (["runtime", "serve"], "Usage: cdh container runtime serve"),
        (["runtime", "restart"], "Usage: cdh container runtime restart"),
        (["runtime", "follow"], "Usage: cdh container runtime follow"),
        (["runtime", "status"], "Usage: cdh container runtime status"),
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
        lambda files, root, *, event_sink: observed.append((files, root)),
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
                            build_plan_input_module.FinalManifestHttpFileInput(
                                type="http",
                                url=item.url,
                                target=item.target,
                                checksum=item.checksum,
                            )
                            for item in plan.files.files
                            if isinstance(item, HttpFilePlan)
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


def test_install_comfyui_constructs_display_after_admission_and_runtime(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wire one admitted invocation to a display after runtime validation."""
    plan = build_plan(final_config(), accepted_resolution())
    expected_runtime = ContainerRuntime(
        workspace=Path(plan.application.paths.workspace),
        comfyui_path=Path(plan.application.paths.comfyui),
        virtual_env=Path(plan.application.paths.venv),
    )
    display = SimpleNamespace(emit=lambda _event: None)
    order: list[str] = []

    class Admission:
        def comfyui_install(self):
            order.append("projection")
            return plan.application, plan.toolchain

    def admit(_digest: str) -> Admission:
        order.append("admission")
        return Admission()

    def runtime_from_env() -> ContainerRuntime:
        order.append("runtime")
        return expected_runtime

    def display_factory(_settings):
        order.append("display")
        return display

    def install(application, toolchain, *, runtime, event_sink, **_kwargs) -> None:
        order.append("service")
        assert application is plan.application
        assert toolchain is plan.toolchain
        assert runtime is expected_runtime
        assert event_sink is display

    monkeypatch.setattr(container_cli, "_admission", admit)
    monkeypatch.setattr(container_cli.ContainerRuntime, "from_env", runtime_from_env)
    monkeypatch.setattr(
        container_cli,
        "default_container_helper_display",
        display_factory,
    )
    monkeypatch.setattr(container_cli, "install_comfyui", install)

    result = cli_runner.invoke(
        app,
        [
            "container",
            "install-comfyui",
            "--build-plan-digest",
            "sha256:" + "a" * 64,
        ],
    )

    assert result.exit_code == 0, result.output
    assert order == ["admission", "projection", "runtime", "display", "service"]


def test_install_comfyui_cli_renders_normal_and_suppresses_quiet_lifecycle(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _materialized_download_plan(tmp_path, monkeypatch)

    def fake_install(_application, _toolchain, *, event_sink, **_kwargs) -> None:
        _emit_comfyui_success(event_sink)

    monkeypatch.setattr(container_cli, "install_comfyui", fake_install)
    monkeypatch.setenv("WORKSPACE", plan.application.paths.workspace)
    monkeypatch.setenv("COMFYUI_PATH", plan.application.paths.comfyui)
    monkeypatch.setenv("VIRTUAL_ENV", plan.application.paths.venv)
    command = [
        "container",
        "install-comfyui",
        "--build-plan-digest",
        build_plan_digest(plan),
    ]

    normal = cli_runner.invoke(app, command)
    quiet = cli_runner.invoke(app, ["--quiet", *command])

    assert normal.exit_code == 0, normal.output
    assert normal.stdout == ""
    assert "Checking out ComfyUI source" in normal.stderr
    assert "ComfyUI installation complete" in normal.stderr
    assert quiet.exit_code == 0, quiet.output
    assert quiet.stdout == ""
    assert quiet.stderr == ""


def test_install_custom_nodes_detail_controls_do_not_own_child_streams(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _materialized_download_plan(tmp_path, monkeypatch)

    def fake_install(_custom_nodes, _application, *, event_sink, **_kwargs) -> None:
        print("manager-child-stdout")
        print("manager-child-stderr", file=sys.stderr)
        event_sink.emit(
            RegistryCustomNodeStarted(
                index=1,
                total=1,
                id="example-node",
                version="1.0.0",
                pre_hook_count=1,
                post_hook_count=2,
            )
        )
        phase = ContainerHelperPhase.CUSTOM_NODE_INSTALLATION
        event_sink.emit(ContainerHelperPhaseStarted(phase))
        event_sink.emit(ContainerHelperPhaseCompleted(phase))
        event_sink.emit(CustomNodesInstallCompleted(node_count=1))

    monkeypatch.setattr(container_cli, "install_custom_nodes", fake_install)
    monkeypatch.setenv("WORKSPACE", plan.application.paths.workspace)
    monkeypatch.setenv("COMFYUI_PATH", plan.application.paths.comfyui)
    monkeypatch.setenv("VIRTUAL_ENV", plan.application.paths.venv)

    command = [
        "container",
        "install-custom-nodes",
        "--build-plan-digest",
        build_plan_digest(plan),
    ]
    result = cli_runner.invoke(app, ["-v", *command])
    quiet = cli_runner.invoke(app, ["--quiet", *command])

    assert result.exit_code == 0, result.output
    assert result.stdout == "manager-child-stdout\n"
    assert "manager-child-stderr" in result.stderr.splitlines()
    assert "[1/1] Custom node: example-node 1.0.0" in result.stderr
    assert "pre-install hooks=1" in result.stderr
    assert "post-install hooks=2" in result.stderr
    assert "Custom-node installation complete: 1 node" in result.stderr
    assert quiet.exit_code == 0, quiet.output
    assert quiet.stdout == "manager-child-stdout\n"
    assert "manager-child-stderr" in quiet.stderr.splitlines()
    assert "example-node" not in quiet.stderr
    assert "Installing the custom node" not in quiet.stderr
    assert "Phase complete" not in quiet.stderr
    assert "Custom-node installation complete" not in quiet.stderr


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


def test_download_files_cli_renders_plain_non_terminal_lifecycle(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _materialized_download_plan(tmp_path, monkeypatch)

    def fake_download(_files, _root, *, event_sink) -> None:
        _emit_download_success(event_sink)

    monkeypatch.setattr(container_cli, "download_files", fake_download)

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
    assert result.stdout == ""
    assert "Required file: models/checkpoints/model.safetensors" in result.stderr
    assert "Attempt [1/3] started" in result.stderr
    assert "Verifying downloaded bytes" in result.stderr
    assert "Placing required file" in result.stderr
    assert "Downloads complete: 1 required file" in result.stderr
    assert "backend=" not in result.stderr


def test_download_files_cli_quiet_suppresses_success_lifecycle(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _materialized_download_plan(tmp_path, monkeypatch)

    def fake_download(_files, _root, *, event_sink) -> None:
        _emit_download_success(event_sink)

    monkeypatch.setattr(container_cli, "download_files", fake_download)

    result = cli_runner.invoke(
        app,
        [
            "--quiet",
            "container",
            "download-files",
            "--build-plan-digest",
            build_plan_digest(plan),
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_download_files_cli_verbose_retry_is_human_oriented(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _materialized_download_plan(tmp_path, monkeypatch)

    def fake_download(_files, _root, *, event_sink) -> None:
        _emit_download_success(event_sink, retry=True)

    monkeypatch.setattr(container_cli, "download_files", fake_download)

    result = cli_runner.invoke(
        app,
        [
            "-v",
            "container",
            "download-files",
            "--build-plan-digest",
            build_plan_digest(plan),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "[1/3] failed; retrying as [2/3] in 1s" in result.stderr
    assert "server is temporarily unavailable" in result.stderr
    assert "backend=" not in result.stderr
    assert "http_status=" not in result.stderr


def test_download_files_cli_controlled_failure_stops_without_success(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _materialized_download_plan(tmp_path, monkeypatch)

    def fail_download(_files, _root, *, event_sink) -> None:
        event_sink.emit(
            DownloadItemStarted(
                index=1,
                total=1,
                target="models/checkpoints/model.safetensors",
                backend=DownloadBackendName.HTTPX,
                max_attempts=3,
                checksum_expected=True,
            )
        )
        event_sink.emit(DownloadAttemptStarted(1))
        raise DownloadFilesError(
            "required download failed for models/checkpoints/model.safetensors"
        )

    monkeypatch.setattr(container_cli, "download_files", fail_download)

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
    assert result.stdout == ""
    assert "Error: required download failed" in result.stderr
    assert "Downloads complete" not in result.stderr
    assert "Traceback" not in result.stderr


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
    monkeypatch.setattr(
        container_cli,
        "default_container_download_invocation",
        lambda *_args, **_kwargs: pytest.fail(
            "presentation must not start before BuildPlan admission"
        ),
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


def test_container_runtime_serve_invokes_service_and_propagates_exit_code(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the serve command wired to the runtime lifecycle service."""
    calls: list[CliOutputSettings] = []

    def fake_run_runtime_serve(settings: CliOutputSettings) -> int:
        calls.append(settings)
        return 17

    monkeypatch.setattr(
        container_cli,
        "run_runtime_serve",
        fake_run_runtime_serve,
    )
    result = cli_runner.invoke(app, ["-vv", "container", "runtime", "serve"])

    assert result.exit_code == 17
    assert calls == [CliOutputSettings(detail=OutputDetail.DEBUG)]


def test_container_runtime_restart_waits_without_detach_options(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_restart_runtime() -> str:
        calls.append("restart")
        return "op-7"

    monkeypatch.setattr(container_cli, "restart_runtime", fake_restart_runtime)

    result = cli_runner.invoke(app, ["container", "runtime", "restart"])
    help_result = cli_runner.invoke(
        app,
        ["container", "runtime", "restart", "--help"],
    )

    assert result.exit_code == 0
    output = result.output.lower()
    assert "restart" in output
    assert "completed" in output
    assert "op-7" in output
    assert calls == ["restart"]
    help_output = _plain_output(help_result.output)
    assert "Restart the managed ComfyUI runtime." in help_output
    assert "generation" not in help_output
    assert "--detach" not in help_output
    assert "--no-wait" not in help_output
    assert "-d" not in help_output


def test_quiet_preserves_restart_operation_result(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_cli, "restart_runtime", lambda: "op-7")

    normal = cli_runner.invoke(app, ["container", "runtime", "restart"])
    quiet = cli_runner.invoke(
        app,
        ["--quiet", "container", "runtime", "restart"],
    )

    assert quiet.exit_code == 0
    assert quiet.stdout == normal.stdout
    assert quiet.stderr == normal.stderr == ""


def test_container_runtime_follow_is_output_only(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_follow_runtime() -> int:
        calls.append("follow")
        sys.stdout.write("runtime-stdout\n")
        sys.stderr.write("runtime-stderr\n")
        return 129

    monkeypatch.setattr(container_cli, "follow_runtime", fake_follow_runtime)

    normal = cli_runner.invoke(app, ["container", "runtime", "follow"])
    quiet = cli_runner.invoke(
        app,
        ["--quiet", "container", "runtime", "follow"],
    )
    help_result = cli_runner.invoke(
        app,
        ["container", "runtime", "follow", "--help"],
    )

    assert normal.exit_code == quiet.exit_code == 129
    assert normal.stdout == quiet.stdout == "runtime-stdout\n"
    assert normal.stderr == quiet.stderr == "runtime-stderr\n"
    assert calls == ["follow", "follow"]
    plain_help = _plain_output(help_result.output)
    assert "live stdout and stderr" in plain_help
    assert "--detach" not in plain_help
    assert "--no-wait" not in plain_help


@pytest.mark.parametrize("json_output", [False, True])
def test_container_runtime_status_renders_minimal_conditional_schema(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    from comfyui_docker_helper.container.runtime_control import (
        RuntimeLastRestart,
        RuntimeStatusResponse,
    )

    monkeypatch.setattr(
        container_cli,
        "read_runtime_status",
        lambda: RuntimeStatusResponse(
            state="running",
            phase=None,
            generation="gen-2",
            operation=None,
            last_restart=RuntimeLastRestart(id="op-1", result="succeeded"),
        ),
    )
    args = ["container", "runtime", "status"]
    if json_output:
        args.append("--json")

    result = cli_runner.invoke(app, args)

    assert result.exit_code == 0
    if json_output:
        assert json.loads(result.output) == {
            "state": "running",
            "phase": None,
            "generation": "gen-2",
            "operation": None,
            "last_restart": {"id": "op-1", "result": "succeeded"},
        }
    else:
        lines = result.output.splitlines()
        assert "state: running" in lines
        assert "runtime: gen-2" in lines
        assert "last_restart: op-1 (succeeded)" in lines
        assert lines.index("state: running") < lines.index("runtime: gen-2")
        assert lines.index("runtime: gen-2") < lines.index(
            "last_restart: op-1 (succeeded)"
        )
        assert all(not line.startswith("generation:") for line in lines)


@pytest.mark.parametrize("json_output", [False, True])
def test_quiet_preserves_runtime_status_result(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    from comfyui_docker_helper.container.runtime_control import RuntimeStatusResponse

    monkeypatch.setattr(
        container_cli,
        "read_runtime_status",
        lambda: RuntimeStatusResponse(
            state="running",
            phase=None,
            generation="gen-2",
            operation=None,
            last_restart=None,
        ),
    )
    command = ["container", "runtime", "status"]
    if json_output:
        command.append("--json")

    normal = cli_runner.invoke(app, command)
    quiet = cli_runner.invoke(app, ["--quiet", *command])

    assert quiet.exit_code == 0
    assert quiet.stdout == normal.stdout
    assert quiet.stderr == normal.stderr == ""
