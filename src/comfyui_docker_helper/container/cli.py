"""Container helper command group."""

from pathlib import Path
from typing import Annotated

import typer

from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.container.download_files import download_files
from comfyui_docker_helper.container.entrypoint import run_entrypoint
from comfyui_docker_helper.container.runners import ContainerRuntime

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
    phase: Annotated[
        Path,
        typer.Option("--phase", help="Materialized files phase JSON."),
    ],
    build_plan_digest: Annotated[
        str,
        typer.Option(
            "--build-plan-digest",
            help="Expected owning BuildPlan SHA-256 digest.",
        ),
    ],
) -> None:
    """Download files from a narrow BuildPlan-owned phase."""
    download_files(phase, expected_build_plan_digest=build_plan_digest)


@app.command("entrypoint", context_settings=HELP_CONTEXT_SETTINGS)
def entrypoint_command() -> None:
    """Start ComfyUI through the cdh runtime entrypoint."""

    runtime = ContainerRuntime.from_env()
    raise typer.Exit(code=run_entrypoint(runtime=runtime))
