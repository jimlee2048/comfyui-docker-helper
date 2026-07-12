"""Host command group."""

from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.config.service import (
    ConfigurationResult,
    ConfigurationServiceError,
    load_validate_config_result,
)
from comfyui_docker_helper.config.value_validation import has_control_characters
from comfyui_docker_helper.host.buildx import BuildxOutput, build_image_with_buildx
from comfyui_docker_helper.host.diagnostics import (
    render_configuration_diagnostics,
    render_configuration_warnings,
    render_plan_preview,
)
from comfyui_docker_helper.host.planning_authority import default_planning_providers
from comfyui_docker_helper.host.render_service import (
    HostRenderServiceError,
    PlanningOptions,
    prepare_render_context,
)
from comfyui_docker_helper.host.uv_runner import HostUvError

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
    """Validate configuration locally without providers, Docker, or writes."""
    config_files = _require_at_least_one(config_files, "--file/-f")
    try:
        result = load_validate_config_result(config_files, scripts_dir=scripts_dir)
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
    hooks_dir: Annotated[
        Path | None,
        typer.Option(
            "--hooks-dir",
            help="Directory containing baked runtime lifecycle hook files.",
            metavar="DIR",
        ),
    ] = None,
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
    try:
        options = PlanningOptions(
            locked=locked,
            check=check,
            upgrade_lock=upgrade_lock,
            dry_run=dry_run,
        )
        with default_planning_providers() as providers:
            prepared = prepare_render_context(
                config_files,
                output_dir,
                scripts_dir=scripts_dir,
                hooks_dir=hooks_dir,
                acquirer=providers.acquirer,
                local_acquirer=providers.local_acquirer,
                options=options,
                overwrite=overwrite,
                working_directory=Path.cwd(),
            )
    except (ConfigurationServiceError, HostRenderServiceError, HostUvError) as error:
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
            options=options,
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
        list[str] | None,
        typer.Option(
            "--tag",
            "-t",
            help=(
                "Docker image tag to build. May be repeated; replaces config "
                "build tags when provided."
            ),
            metavar="IMAGE:TAG",
        ),
    ] = None,
    load: Annotated[
        bool,
        typer.Option(
            "--load",
            help="Load the built image into the local Docker image store.",
        ),
    ] = False,
    push: Annotated[
        bool,
        typer.Option(
            "--push",
            help="Push the built image tags to their registry.",
        ),
    ] = False,
    scripts_dir: Annotated[
        Path,
        typer.Option(
            "--scripts-dir",
            help="Directory containing referenced custom-node hook scripts.",
            metavar="DIR",
        ),
    ] = _DEFAULT_SCRIPTS_DIR,
    hooks_dir: Annotated[
        Path | None,
        typer.Option(
            "--hooks-dir",
            help="Directory containing baked runtime lifecycle hook files.",
            metavar="DIR",
        ),
    ] = None,
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
    cli_tags = image_tags or []
    cli_output = _resolve_cli_build_output(load=load, push=push)

    try:
        validated = load_validate_config_result(config_files, scripts_dir=scripts_dir)
    except ConfigurationServiceError as error:
        render_configuration_diagnostics(
            _format_config_files(config_files),
            error.diagnostics,
        )
        raise typer.Exit(code=1) from error
    effective_tags = _resolve_effective_image_tags(
        cli_tags=cli_tags,
        config_tags=validated.config.build.tags,
    )
    effective_output = cli_output or validated.config.build.output
    validated = _apply_build_overrides(
        validated,
        image_tags=effective_tags,
        output=effective_output,
    )

    try:
        options = PlanningOptions(locked=locked, upgrade_lock=upgrade_lock)
        with default_planning_providers() as providers:
            prepared = prepare_render_context(
                config_files,
                context_dir,
                scripts_dir=scripts_dir,
                hooks_dir=hooks_dir,
                acquirer=providers.acquirer,
                local_acquirer=providers.local_acquirer,
                options=options,
                overwrite=True,
                working_directory=Path.cwd(),
                configuration_result=validated,
            )
    except (ConfigurationServiceError, HostRenderServiceError, HostUvError) as error:
        render_configuration_diagnostics(
            _format_config_files(config_files),
            error.diagnostics,
        )
        raise typer.Exit(code=1) from error
    render_configuration_warnings(_format_config_files(config_files), prepared.warnings)

    typer.echo(f"Build context: {context_dir}")
    build_plan = prepared.plan.build
    build_image_with_buildx(
        image_tags=build_plan.tags,
        output=build_plan.output,
        context_dir=context_dir,
        platforms=build_plan.platforms,
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


def _resolve_cli_build_output(*, load: bool, push: bool) -> BuildxOutput | None:
    if load and push:
        raise typer.BadParameter(
            "must not be used together",
            param_hint="--load/--push",
        )
    if push:
        return "push"
    if load:
        return "load"
    return None


def _apply_build_overrides(
    result: ConfigurationResult,
    *,
    image_tags: tuple[str, ...],
    output: BuildxOutput,
) -> ConfigurationResult:
    build = result.config.build.model_copy(
        update={"tags": list(image_tags), "output": output}
    )
    config = result.config.model_copy(update={"build": build})
    return replace(result, config=config)


def _resolve_effective_image_tags(
    *,
    cli_tags: list[str],
    config_tags: list[str],
) -> tuple[str, ...]:
    _validate_cli_image_tags(cli_tags)
    tags = tuple(cli_tags or config_tags)
    if not tags:
        raise typer.BadParameter(
            "must provide at least one image tag with --tag/-t or [build].tags",
            param_hint="--tag/-t",
        )
    return tags


def _validate_cli_image_tags(tags: list[str]) -> None:
    for tag in tags:
        if (
            not tag
            or any(character.isspace() for character in tag)
            or has_control_characters(tag)
        ):
            raise typer.BadParameter(
                "must be non-empty and must not contain whitespace "
                "or control characters",
                param_hint="--tag/-t",
            )


def _format_config_files(config_files: list[Path]) -> str | Path:
    if len(config_files) == 1:
        return config_files[0]
    return ", ".join(str(path) for path in config_files)
