"""Container helper command group."""

from pathlib import Path
from typing import Annotated

import typer

from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.container.build_plan_input import (
    MATERIALIZED_BUILD_PLAN_PATH,
    BuildPlanInputAdmission,
)
from comfyui_docker_helper.container.comfyui_installer import install_comfyui
from comfyui_docker_helper.container.custom_node_installer import install_custom_nodes
from comfyui_docker_helper.container.download_files import download_files
from comfyui_docker_helper.container.entrypoint import run_entrypoint
from comfyui_docker_helper.container.final_manifest import emit_final_manifest
from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
)

app = typer.Typer(
    name="container",
    help="Run image-internal build and runtime helpers.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=HELP_CONTEXT_SETTINGS,
)


@app.callback()
def container() -> None:
    """Run container-side helper commands."""


@app.command("download-files", context_settings=HELP_CONTEXT_SETTINGS)
def download_files_command(
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
    download_files(files, comfyui_root)


@app.command("install-comfyui", context_settings=HELP_CONTEXT_SETTINGS)
def install_comfyui_command(
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
    install_comfyui(
        application,
        toolchain,
        runtime=ContainerRuntime.from_env(),
        constraints_path=constraints,
    )


@app.command("install-custom-nodes", context_settings=HELP_CONTEXT_SETTINGS)
def install_custom_nodes_command(
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
    install_custom_nodes(
        custom_nodes,
        application,
        runtime=ContainerRuntime.from_env(),
        constraints_path=constraints,
        build_hooks_directory=build_hooks_directory,
    )


@app.command("emit-final-manifest", context_settings=HELP_CONTEXT_SETTINGS)
def emit_final_manifest_command(
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
    emit_final_manifest(projection, runtime=ContainerRuntime.from_env())


@app.command("entrypoint", context_settings=HELP_CONTEXT_SETTINGS)
def entrypoint_command() -> None:
    """Start ComfyUI through the cdh runtime entrypoint."""

    runtime = ContainerRuntime.from_env()
    raise typer.Exit(code=run_entrypoint(runtime=runtime))


def _admission(digest: str) -> BuildPlanInputAdmission:
    try:
        return BuildPlanInputAdmission.from_path(
            MATERIALIZED_BUILD_PLAN_PATH,
            expected_build_plan_digest=digest,
        )
    except ValueError as error:
        raise ContainerCommandError(str(error)) from error
