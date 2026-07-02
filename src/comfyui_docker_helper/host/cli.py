"""Host command group."""

from pathlib import Path
from typing import Annotated

import typer

from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.config import (
    ConfigurationServiceError,
    LockOptions,
    load_validate_plan_result,
)
from comfyui_docker_helper.host.buildx import build_image_with_buildx
from comfyui_docker_helper.host.diagnostics import (
    render_configuration_diagnostics,
    render_configuration_warnings,
    render_plan_preview,
)
from comfyui_docker_helper.host.render_service import (
    HostRenderServiceError,
    prepare_render_context,
)
from comfyui_docker_helper.host.source_providers import create_default_source_resolvers

_DEFAULT_SCRIPTS_DIR = Path("./scripts")
_DEFAULT_CONTEXT_DIR = Path(".cdh/build/current")

app = typer.Typer(
    name="host",
    help="Validate configuration, render build contexts, and build images.",
    no_args_is_help=True,
    add_completion=False,
    context_settings=HELP_CONTEXT_SETTINGS,
)


@app.callback()
def host() -> None:
    """Run host-side commands."""


@app.command("validate", context_settings=HELP_CONTEXT_SETTINGS)
def validate(
    config_files: Annotated[
        list[Path],
        typer.Option(
            "--file",
            "-f",
            help="TOML configuration file to validate.",
            metavar="CONFIG.TOML",
        ),
    ],
    scripts_dir: Annotated[
        Path,
        typer.Option(
            "--scripts-dir",
            help="Directory containing referenced custom-node hook scripts.",
            metavar="DIR",
        ),
    ] = _DEFAULT_SCRIPTS_DIR,
) -> None:
    """Validate configuration and build its normalized plan without writing files."""
    config_files = _require_at_least_one(config_files, "--file/-f")
    try:
        result = load_validate_plan_result(config_files, scripts_dir=scripts_dir)
    except ConfigurationServiceError as error:
        render_configuration_diagnostics(
            _format_config_files(config_files),
            error.diagnostics,
        )
        raise typer.Exit(code=1) from error
    render_configuration_warnings(_format_config_files(config_files), result.warnings)


@app.command("render", context_settings=HELP_CONTEXT_SETTINGS)
def render(
    config_files: Annotated[
        list[Path],
        typer.Option(
            "--file",
            "-f",
            help="TOML configuration file to render.",
            metavar="CONFIG.TOML",
        ),
    ],
    output_dirs: Annotated[
        list[Path],
        typer.Option(
            "--output",
            "-o",
            help="Build-context output directory.",
            metavar="DIR",
        ),
    ],
    scripts_dir: Annotated[
        Path,
        typer.Option(
            "--scripts-dir",
            help="Directory containing referenced custom-node hook scripts.",
            metavar="DIR",
        ),
    ] = _DEFAULT_SCRIPTS_DIR,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Validate and print the normalized plan without writing files.",
        ),
    ] = False,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Replace an existing valid cdh build context.",
        ),
    ] = False,
    locked: Annotated[
        bool,
        typer.Option(
            "--locked",
            help="Require the existing context lock without updating it.",
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Validate that root render artifacts are already up to date.",
        ),
    ] = False,
    upgrade_lock: Annotated[
        bool,
        typer.Option(
            "--upgrade-lock",
            help="Re-resolve moving source selectors and update the context lock.",
        ),
    ] = False,
) -> None:
    """Render a Docker build context from configuration file(s)."""
    config_files = _require_at_least_one(config_files, "--file/-f")
    output_dir = _require_exactly_one(output_dirs, "--output/-o")
    lock_options = LockOptions(
        locked=locked,
        check=check,
        upgrade_lock=upgrade_lock,
        dry_run=dry_run,
    )
    try:
        prepared = prepare_render_context(
            config_files,
            output_dir,
            scripts_dir=scripts_dir,
            resolvers=create_default_source_resolvers(),
            lock_options=lock_options,
            overwrite=overwrite,
            working_directory=Path.cwd(),
        )
    except (ConfigurationServiceError, HostRenderServiceError) as error:
        render_configuration_diagnostics(
            _format_config_files(config_files),
            error.diagnostics,
        )
        raise typer.Exit(code=1) from error
    render_configuration_warnings(_format_config_files(config_files), prepared.warnings)

    if dry_run:
        render_plan_preview(
            prepared.plan,
            lock_result=prepared.lock_result,
            lock_options=lock_options,
        )
        return


@app.command("build", context_settings=HELP_CONTEXT_SETTINGS)
def build(
    config_files: Annotated[
        list[Path],
        typer.Option(
            "--file",
            "-f",
            help="TOML configuration file to build.",
            metavar="CONFIG.TOML",
        ),
    ],
    image_tags: Annotated[
        list[str],
        typer.Option(
            "--tag",
            "-t",
            help="Docker image tag to load.",
            metavar="IMAGE:TAG",
        ),
    ],
    scripts_dir: Annotated[
        Path,
        typer.Option(
            "--scripts-dir",
            help="Directory containing referenced custom-node hook scripts.",
            metavar="DIR",
        ),
    ] = _DEFAULT_SCRIPTS_DIR,
    context_dir: Annotated[
        Path,
        typer.Option(
            "--context-dir",
            help="Build-context directory to render and build.",
            metavar="DIR",
        ),
    ] = _DEFAULT_CONTEXT_DIR,
    locked: Annotated[
        bool,
        typer.Option(
            "--locked",
            help="Require the existing context lock without updating it.",
        ),
    ] = False,
    upgrade_lock: Annotated[
        bool,
        typer.Option(
            "--upgrade-lock",
            help="Re-resolve moving source selectors and update the context lock.",
        ),
    ] = False,
) -> None:
    """Render a build context and build it with Docker Buildx."""
    config_files = _require_at_least_one(config_files, "--file/-f")
    image_tag = _require_exactly_one(image_tags, "--tag/-t")

    try:
        prepared = prepare_render_context(
            config_files,
            context_dir,
            scripts_dir=scripts_dir,
            resolvers=create_default_source_resolvers(),
            lock_options=LockOptions(locked=locked, upgrade_lock=upgrade_lock),
            overwrite=True,
            working_directory=Path.cwd(),
        )
    except (ConfigurationServiceError, HostRenderServiceError) as error:
        render_configuration_diagnostics(
            _format_config_files(config_files),
            error.diagnostics,
        )
        raise typer.Exit(code=1) from error
    render_configuration_warnings(_format_config_files(config_files), prepared.warnings)

    typer.echo(f"Build context: {context_dir}")
    build_image_with_buildx(
        image_tag=image_tag,
        context_dir=context_dir,
        cwd=Path.cwd(),
        log=typer.echo,
    )


def _require_at_least_one[T](values: list[T], param_hint: str) -> list[T]:
    if not values:
        raise typer.BadParameter(
            "must be provided at least once",
            param_hint=param_hint,
        )
    return values


def _require_exactly_one[T](values: list[T], param_hint: str) -> T:
    if len(values) != 1:
        raise typer.BadParameter(
            "must be provided exactly once",
            param_hint=param_hint,
        )
    return values[0]


def _format_config_files(config_files: list[Path]) -> str | Path:
    if len(config_files) == 1:
        return config_files[0]
    return ", ".join(str(path) for path in config_files)
