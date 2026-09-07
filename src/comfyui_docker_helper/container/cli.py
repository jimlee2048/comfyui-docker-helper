"""Container helper command group."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from comfyui_docker_helper.cli_settings import (
    HELP_CONTEXT_SETTINGS,
    require_output_settings,
)
from comfyui_docker_helper.errors import ApplicationError

if TYPE_CHECKING:
    from comfyui_docker_helper.container.build.admission import (
        BuildPlanInputAdmission,
    )

if sys.platform == "linux":
    from comfyui_docker_helper.container.build.admission import (
        MATERIALIZED_BUILD_PLAN_PATH,
        BuildPlanInputAdmission,
    )
    from comfyui_docker_helper.container.build.comfyui import install_comfyui
    from comfyui_docker_helper.container.build.custom_nodes.orchestrator import (
        install_custom_nodes,
    )
    from comfyui_docker_helper.container.build.downloads import download_files
    from comfyui_docker_helper.container.build.local_trees import (
        normalize_local_trees,
        validate_local_trees,
    )
    from comfyui_docker_helper.container.build.manifest.observer import (
        emit_final_manifest,
    )
    from comfyui_docker_helper.container.presentation.download import (
        default_container_download_invocation,
    )
    from comfyui_docker_helper.container.presentation.helper import (
        default_container_helper_display,
    )
    from comfyui_docker_helper.container.process.runners import (
        ContainerCommandError,
        ContainerRuntime,
    )
    from comfyui_docker_helper.container.runtime.control.client import (
        read_runtime_logs,
        read_runtime_status,
        restart_runtime,
    )
    from comfyui_docker_helper.container.runtime.serve import run_runtime_serve
else:
    MATERIALIZED_BUILD_PLAN_PATH = Path("/opt/cdh/build/build-plan.json")

_CONTAINER_PLATFORM_ERROR = (
    "cdh container commands run only inside the project's Linux image; "
    "use 'cdh host' on the host machine"
)

BuildPlanDigestOption = Annotated[
    str,
    typer.Option(
        "--build-plan-digest",
        help="Expected owning BuildPlan SHA-256 digest.",
    ),
]

app = typer.Typer(
    name="container",
    help="Run image-internal build and runtime helpers.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=HELP_CONTEXT_SETTINGS,
)

build_app = typer.Typer(
    name="build",
    help=(
        "Run image-internal steps during Docker image builds.\n\n"
        "These steps are normally invoked by the generated Dockerfile. "
        "Use 'cdh host build' to build a complete image from the host."
    ),
    short_help="Run image-internal steps during Docker image builds.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=HELP_CONTEXT_SETTINGS,
)
app.add_typer(build_app)

runtime_app = typer.Typer(
    name="runtime",
    help="Control the container runtime.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=HELP_CONTEXT_SETTINGS,
)
app.add_typer(runtime_app)


@app.callback()
def container(ctx: typer.Context) -> None:
    """Run container-side helper commands."""
    ctx.obj = require_output_settings(ctx)


@build_app.command("download-files", context_settings=HELP_CONTEXT_SETTINGS)
def download_files_command(
    ctx: typer.Context,
    build_plan_digest: BuildPlanDigestOption,
) -> None:
    """Download files declared by the canonical BuildPlan."""
    files, comfyui_root = _admission(build_plan_digest).file_downloads()
    settings = require_output_settings(ctx)
    with default_container_download_invocation(settings) as invocation:
        download_files(files, comfyui_root, event_sink=invocation)


@build_app.command("normalize-local-trees", context_settings=HELP_CONTEXT_SETTINGS)
def normalize_local_trees_command(
    build_plan_digest: BuildPlanDigestOption,
) -> None:
    """Create required directories and normalize selected tree modes after COPY."""
    trees, comfyui_root = _admission(build_plan_digest).local_trees()
    normalize_local_trees(trees, comfyui_root)


@build_app.command("validate-tree-targets", context_settings=HELP_CONTEXT_SETTINGS)
def validate_tree_targets_command(
    build_plan_digest: BuildPlanDigestOption,
) -> None:
    """Check selected tree targets before COPY without modifying paths."""
    trees, comfyui_root = _admission(build_plan_digest).local_trees()
    validate_local_trees(trees, comfyui_root)


@build_app.command("install-comfyui", context_settings=HELP_CONTEXT_SETTINGS)
def install_comfyui_command(
    ctx: typer.Context,
    build_plan_digest: BuildPlanDigestOption,
) -> None:
    """Install exact official ComfyUI and its complete requirements."""
    application, toolchain = _admission(build_plan_digest).comfyui_install()
    runtime = ContainerRuntime.from_env()
    display = default_container_helper_display(require_output_settings(ctx))
    install_comfyui(
        application,
        toolchain,
        runtime=runtime,
        event_sink=display,
    )


@build_app.command("install-custom-nodes", context_settings=HELP_CONTEXT_SETTINGS)
def install_custom_nodes_command(
    ctx: typer.Context,
    build_plan_digest: BuildPlanDigestOption,
) -> None:
    """Install the exact ordered Registry and direct-Git custom nodes."""
    custom_nodes, application = _admission(build_plan_digest).custom_node_install()
    runtime = ContainerRuntime.from_env()
    display = default_container_helper_display(require_output_settings(ctx))
    install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        build_plan_digest=build_plan_digest,
        event_sink=display,
    )


@build_app.command("write-final-manifest", context_settings=HELP_CONTEXT_SETTINGS)
def write_final_manifest_command(
    ctx: typer.Context,
    build_plan_digest: BuildPlanDigestOption,
) -> None:
    """Verify final image state, then write its observational manifest."""
    projection = _admission(build_plan_digest).final_manifest()
    runtime = ContainerRuntime.from_env()
    display = default_container_helper_display(require_output_settings(ctx))
    emit_final_manifest(projection, runtime=runtime, event_sink=display)


@runtime_app.command("serve", context_settings=HELP_CONTEXT_SETTINGS)
def runtime_serve_command(ctx: typer.Context) -> None:
    """Run the managed ComfyUI container runtime."""
    _require_linux_container()
    raise typer.Exit(code=run_runtime_serve(require_output_settings(ctx)))


@runtime_app.command("restart", context_settings=HELP_CONTEXT_SETTINGS)
def runtime_restart_command() -> None:
    """Restart the managed ComfyUI runtime."""
    _require_linux_container()
    operation = restart_runtime()
    typer.echo(f"Runtime restart completed: {operation}.")


@runtime_app.command("status", context_settings=HELP_CONTEXT_SETTINGS)
def runtime_status_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the fixed machine-readable status schema."),
    ] = False,
) -> None:
    """Show the ComfyUI runtime and restart status."""
    _require_linux_container()
    status = read_runtime_status()
    last_restart = status.last_restart
    values = {
        "state": status.state,
        "phase": status.phase,
        "generation": status.generation,
        "operation": status.operation,
        "last_restart": (
            None
            if last_restart is None
            else {"id": last_restart.id, "result": last_restart.result}
        ),
    }
    if json_output:
        typer.echo(json.dumps(values, separators=(",", ":")))
        return
    typer.echo(f"state: {status.state}")
    human_fields = (
        ("phase", "phase"),
        ("runtime", "generation"),
        ("operation", "operation"),
    )
    for label, key in human_fields:
        value = values[key]
        if value is not None:
            typer.echo(f"{label}: {value}")
    if last_restart is not None:
        typer.echo(f"last_restart: {last_restart.id} ({last_restart.result})")


def _parse_log_tail(value: str) -> int | None:
    if value == "all":
        return None
    if value.isascii() and value.isdecimal():
        try:
            return int(value)
        except ValueError:
            pass
    raise typer.BadParameter("Expected 'all' or a nonnegative integer.")


@runtime_app.command("logs", context_settings=HELP_CONTEXT_SETTINGS)
def runtime_logs_command(
    tail: Annotated[
        str,
        typer.Option(
            "--tail", "-n", help="Show the last N lines, or all retained output."
        ),
    ] = "all",
    follow: Annotated[
        bool,
        typer.Option(
            "--follow", "-f", help="Follow new output after retained history."
        ),
    ] = False,
) -> None:
    """Read retained merged container logs, optionally following new output."""
    _require_linux_container()
    count = _parse_log_tail(tail)
    raise typer.Exit(code=read_runtime_logs(tail=count, follow=follow))


def _require_linux_container() -> None:
    if sys.platform != "linux":
        raise ApplicationError(_CONTAINER_PLATFORM_ERROR)


def _admission(digest: str) -> BuildPlanInputAdmission:
    _require_linux_container()
    try:
        return BuildPlanInputAdmission.from_path(
            MATERIALIZED_BUILD_PLAN_PATH,
            expected_build_plan_digest=digest,
        )
    except ValueError as error:
        raise ContainerCommandError(str(error)) from error
