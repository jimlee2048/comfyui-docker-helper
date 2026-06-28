"""Top-level command-line interface."""

from typing import Annotated

import typer

from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.container.cli import app as container_app
from comfyui_docker_helper.errors import ApplicationGroup
from comfyui_docker_helper.host.cli import app as host_app
from comfyui_docker_helper.version import package_version

app = typer.Typer(
    cls=ApplicationGroup,
    name="cdh",
    help=(
        "Choose whether cdh commands run on the host machine or inside Docker "
        "build containers."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings=HELP_CONTEXT_SETTINGS,
)


def _version_callback(show_version: bool) -> None:
    if show_version:
        typer.echo(f"cdh {package_version()}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            help="Show the version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Route commands to host or container execution contexts."""


app.add_typer(host_app, name="host", help="commands executed on the host machine")
app.add_typer(
    container_app,
    name="container",
    help="helpers executed inside Docker build containers",
)
