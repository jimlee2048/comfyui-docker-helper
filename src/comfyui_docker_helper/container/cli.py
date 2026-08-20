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
    from comfyui_docker_helper.container.build_plan_input import (
        BuildPlanInputAdmission,
    )

if sys.platform == "linux":
    from comfyui_docker_helper.container.build_plan_input import (
        MATERIALIZED_BUILD_PLAN_PATH,
        BuildPlanInputAdmission,
    )
    from comfyui_docker_helper.container.comfyui_installer import install_comfyui
    from comfyui_docker_helper.container.custom_node_installer import (
        install_custom_nodes,
    )
    from comfyui_docker_helper.container.download_files import download_files
    from comfyui_docker_helper.container.final_manifest import emit_final_manifest
    from comfyui_docker_helper.container.presentation import (
        default_container_download_invocation,
        default_container_helper_display,
    )
    from comfyui_docker_helper.container.runners import (
        ContainerCommandError,
        ContainerRuntime,
    )
    from comfyui_docker_helper.container.runtime_control_client import (
        follow_runtime,
        read_runtime_status,
        restart_runtime,
    )
    from comfyui_docker_helper.container.runtime_serve import run_runtime_serve
else:
    MATERIALIZED_BUILD_PLAN_PATH = Path("/opt/cdh/build/build-plan.json")

_CONTAINER_PLATFORM_ERROR = (
    "cdh container commands run only inside the project's Linux image; "
    "use 'cdh host' on the host machine"
)

app = typer.Typer(
    name="container",
    help="Run image-internal build and runtime helpers.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=HELP_CONTEXT_SETTINGS,
)

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
    if (
        sys.platform != "linux"
        and ctx.invoked_subcommand is not None
        and ctx.invoked_subcommand != "runtime"
    ):
        _require_linux_container()


@app.command("download-files", context_settings=HELP_CONTEXT_SETTINGS)
def download_files_command(
    ctx: typer.Context,
    build_plan_digest: Annotated[
        str,
        typer.Option(
            "--build-plan-digest",
            help="Expected owning BuildPlan SHA-256 digest.",
        ),
    ],
) -> None:
    """Download files declared by the canonical BuildPlan."""
    files, comfyui_root = _admission(build_plan_digest).file_downloads()
    settings = require_output_settings(ctx)
    with default_container_download_invocation(settings) as invocation:
        download_files(files, comfyui_root, event_sink=invocation)


@app.command("install-comfyui", context_settings=HELP_CONTEXT_SETTINGS)
def install_comfyui_command(
    ctx: typer.Context,
    build_plan_digest: Annotated[
        str,
        typer.Option(
            "--build-plan-digest",
            help="Expected owning BuildPlan SHA-256 digest.",
        ),
    ],
    constraints: Annotated[
        Path,
        typer.Option("--constraints", help="Materialized managed constraints file."),
    ] = Path("/opt/cdh/build/python-package-constraints.txt"),
) -> None:
    """Install exact official ComfyUI and its complete requirements."""
    application, toolchain = _admission(build_plan_digest).comfyui_install()
    runtime = ContainerRuntime.from_env()
    display = default_container_helper_display(require_output_settings(ctx))
    install_comfyui(
        application,
        toolchain,
        runtime=runtime,
        constraints_path=constraints,
        event_sink=display,
    )


@app.command("install-custom-nodes", context_settings=HELP_CONTEXT_SETTINGS)
def install_custom_nodes_command(
    ctx: typer.Context,
    build_plan_digest: Annotated[
        str,
        typer.Option(
            "--build-plan-digest",
            help="Expected owning BuildPlan SHA-256 digest.",
        ),
    ],
    constraints: Annotated[
        Path,
        typer.Option("--constraints", help="Materialized managed constraints file."),
    ] = Path("/opt/cdh/build/python-package-constraints.txt"),
    build_hooks_directory: Annotated[
        Path,
        typer.Option(
            "--build-hooks-directory",
            help="Materialized build hook directory.",
        ),
    ] = Path("/opt/cdh/build/hooks"),
) -> None:
    """Install the exact ordered Registry and direct-Git custom nodes."""
    custom_nodes, application = _admission(build_plan_digest).custom_node_install()
    runtime = ContainerRuntime.from_env()
    display = default_container_helper_display(require_output_settings(ctx))
    install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        constraints_path=constraints,
        build_hooks_directory=build_hooks_directory,
        build_plan_digest=build_plan_digest,
        event_sink=display,
    )


@app.command("emit-final-manifest", context_settings=HELP_CONTEXT_SETTINGS)
def emit_final_manifest_command(
    ctx: typer.Context,
    build_plan_digest: Annotated[
        str,
        typer.Option(
            "--build-plan-digest",
            help="Expected owning BuildPlan SHA-256 digest.",
        ),
    ],
) -> None:
    """Verify final image state and emit its observational manifest."""
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


@runtime_app.command("follow", context_settings=HELP_CONTEXT_SETTINGS)
def runtime_follow_command() -> None:
    """Stream live stdout and stderr from the running container."""
    _require_linux_container()
    raise typer.Exit(code=follow_runtime())


def _require_linux_container() -> None:
    if sys.platform != "linux":
        raise ApplicationError(_CONTAINER_PLATFORM_ERROR)


def _admission(digest: str) -> BuildPlanInputAdmission:
    try:
        return BuildPlanInputAdmission.from_path(
            MATERIALIZED_BUILD_PLAN_PATH,
            expected_build_plan_digest=digest,
        )
    except ValueError as error:
        raise ContainerCommandError(str(error)) from error
