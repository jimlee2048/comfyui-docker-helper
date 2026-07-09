"""Container helper command group."""

from pathlib import Path
from typing import Annotated

import typer

from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.container.download_files import download_files
from comfyui_docker_helper.container.entrypoint import run_entrypoint
from comfyui_docker_helper.container.install_custom_nodes import (
    install_custom_nodes,
)
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


@app.command("install-custom-nodes", context_settings=HELP_CONTEXT_SETTINGS)
def install_custom_nodes_command(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Root rendered config.toml.",
            show_default=False,
        ),
    ],
    lock: Annotated[
        Path,
        typer.Option(
            "--lock",
            help="Root rendered config.lock.toml.",
            show_default=False,
        ),
    ],
    scripts_dir: Annotated[
        Path | None,
        typer.Option(
            "--scripts-dir",
            help="Mounted directory containing referenced hook scripts.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Install configured custom nodes during image build."""

    install_custom_nodes(
        config,
        lock,
        scripts_dir=scripts_dir,
        runtime=ContainerRuntime.from_env(),
    )


@app.command("download-files", context_settings=HELP_CONTEXT_SETTINGS)
def download_files_command(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Root rendered config.toml.",
            show_default=False,
        ),
    ],
    lock: Annotated[
        Path,
        typer.Option(
            "--lock",
            help="Root rendered config.lock.toml.",
            show_default=False,
        ),
    ],
) -> None:
    """Download configured files during image build."""

    runtime = ContainerRuntime.from_env()
    download_files(config, lock, comfyui_path=runtime.comfyui_path)


@app.command("entrypoint", context_settings=HELP_CONTEXT_SETTINGS)
def entrypoint_command() -> None:
    """Start ComfyUI through the cdh runtime entrypoint."""

    runtime = ContainerRuntime.from_env()
    raise typer.Exit(code=run_entrypoint(runtime=runtime))
