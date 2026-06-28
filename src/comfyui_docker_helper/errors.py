"""Shared application error handling for CLI commands."""

from typing import Any

import typer
from typer.core import TyperGroup


class ApplicationError(Exception):
    """A user-facing failure raised by application services or commands."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        if exit_code <= 0:
            raise ValueError("Application error exit codes must be positive.")
        super().__init__(message)
        self.exit_code = exit_code


class ApplicationGroup(TyperGroup):
    """Translate expected application failures into concise CLI errors."""

    def invoke(self, ctx: typer.Context) -> Any:
        try:
            return super().invoke(ctx)
        except ApplicationError as error:
            typer.echo(f"Error: {error}", err=True)
            raise typer.Exit(code=error.exit_code) from error
