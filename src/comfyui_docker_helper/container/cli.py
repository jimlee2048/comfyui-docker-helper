"""Container helper command group."""

from pathlib import Path
from typing import Annotated

import typer

from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.container.download_files import download_files
from comfyui_docker_helper.container.install_custom_nodes import (
    install_custom_nodes,
)
from comfyui_docker_helper.container.runners import ContainerRuntime

app = typer.Typer(
    name="container",
    help="Run build-time helpers inside the image build container.",
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
            help="Generated custom-node helper TOML.",
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
    """Install configured custom nodes inside the build container."""

    install_custom_nodes(
        config,
        scripts_dir=scripts_dir,
        runtime=ContainerRuntime.from_env(),
    )


@app.command("download-files", context_settings=HELP_CONTEXT_SETTINGS)
def download_files_command(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Generated file-download helper TOML.",
            show_default=False,
        ),
    ],
) -> None:
    """Download configured files inside the build container."""

    runtime = ContainerRuntime.from_env()
    download_files(config, comfyui_path=runtime.comfyui_path)
