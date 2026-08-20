"""Shared Typer settings and invocation context access."""

import typer

from comfyui_docker_helper.cli_output import CliOutputSettings

HELP_CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def require_output_settings(context: typer.Context) -> CliOutputSettings:
    """Return the immutable settings inherited from the root callback."""
    settings = context.find_object(CliOutputSettings)
    if settings is None:
        raise RuntimeError("CLI output settings were not initialized")
    return settings
