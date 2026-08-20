"""Top-level command-line interface."""

from typing import Annotated

import typer

from comfyui_docker_helper.cli_output import CliOutputSettings
from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.container.cli import app as container_app
from comfyui_docker_helper.errors import ApplicationGroup
from comfyui_docker_helper.host.cli import app as host_app
from comfyui_docker_helper.version import package_version

app = typer.Typer(
    cls=ApplicationGroup,
    name="cdh",
    help="Build ComfyUI images and run their image-internal helpers.",
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
    context: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            help="Show the version and exit.",
            is_eager=True,
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Suppress cdh-owned informational progress and summaries.",
        ),
    ] = False,
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            metavar="",
            show_default=False,
            help="Show additional operational detail; repeat for debug detail.",
        ),
    ] = 0,
) -> None:
    """Route host and image-internal commands."""
    try:
        context.obj = CliOutputSettings.from_cli_options(
            quiet=quiet,
            verbosity=verbose,
        )
    except ValueError as error:
        raise typer.BadParameter(
            "--quiet and --verbose cannot be used together",
            param_hint="-q/--quiet, -v/--verbose",
        ) from error


app.add_typer(host_app, name="host", help="commands executed on the host machine")
app.add_typer(
    container_app,
    name="container",
    help="helpers executed inside ComfyUI images",
)
