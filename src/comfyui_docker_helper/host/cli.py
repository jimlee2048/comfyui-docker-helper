"""Host command group."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from comfyui_docker_helper.build_ssh import KNOWN_HOSTS_MOUNTS
from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.config.build_plan import git_credential_secret_ids
from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.final_models import FinalGitCustomNodeConfig
from comfyui_docker_helper.config.git_credentials import (
    GIT_CREDENTIAL_VALUE_MAX_BYTES,
)
from comfyui_docker_helper.config.publication_tags import (
    static_release_availability,
    validate_publication_tags,
)
from comfyui_docker_helper.config.service import (
    ConfigurationResult,
    ConfigurationServiceError,
    load_validate_config_result,
)
from comfyui_docker_helper.config.value_validation import is_argv_value
from comfyui_docker_helper.host.buildx import (
    BuildxBuildError,
    BuildxOutput,
    FileSecretBinding,
    build_image_with_buildx,
)
from comfyui_docker_helper.host.diagnostics import (
    HostPresenter,
    default_host_presenter,
)
from comfyui_docker_helper.host.planning_authority import default_planning_providers
from comfyui_docker_helper.host.release_wheel import CanonicalWheelError
from comfyui_docker_helper.host.render_service import (
    HostRenderServiceError,
    PlanningOptions,
    admit_build_hook_source,
    prepare_render_context,
)

if TYPE_CHECKING:
    from comfyui_docker_helper.host.secret_session import (
        HostSecretSession,
        HostSecretSessionError,
    )

_DEFAULT_CONTEXT_DIR = Path(".cdh/build/current")
_platform_name = os.name

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
            help="TOML configuration layer; repeat to merge in order.",
            metavar="CONFIG.TOML",
        ),
    ],
    build_hooks_dir: Annotated[
        Path | None,
        typer.Option(
            "--build-hooks-dir",
            help="Directory containing referenced build hook files.",
            metavar="DIR",
        ),
    ] = None,
) -> None:
    """Validate configuration locally without network access, Docker, or writes."""
    config_files = _require_at_least_one(config_files, "--file/-f")
    presenter = default_host_presenter()
    try:
        result = load_validate_config_result(
            config_files, build_hooks_dir=build_hooks_dir
        )
    except ConfigurationServiceError as error:
        presenter.diagnostics("Configuration is invalid", error.diagnostics)
        raise typer.Exit(code=1) from error
    presenter.warnings(result.warnings)
    presenter.validate_success(config_files)


@app.command("render", context_settings=HELP_CONTEXT_SETTINGS)
def render(
    config_files: Annotated[
        list[Path],
        typer.Option(
            "--file",
            "-f",
            help="TOML configuration layer; repeat to merge in order.",
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
    build_hooks_dir: Annotated[
        Path | None,
        typer.Option(
            "--build-hooks-dir",
            help="Directory containing referenced build hook files.",
            metavar="DIR",
        ),
    ] = None,
    runtime_hooks_dir: Annotated[
        Path | None,
        typer.Option(
            "--runtime-hooks-dir",
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
            help="Require a matching lock without resolving or updating the context.",
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Verify that an existing build context is up to date without writing.",
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
    """Render a context; Docker may be used when new uv resolution is needed."""
    from comfyui_docker_helper.host.secret_session import (
        HostSecretSession,
        HostSecretSessionError,
    )

    config_files = _require_at_least_one(config_files, "--file/-f")
    output_dir = _require_exactly_one(output_dirs, "--output/-o")
    presenter = default_host_presenter()
    try:
        options = PlanningOptions(
            locked=locked,
            check=check,
            upgrade_lock=upgrade_lock,
            dry_run=dry_run,
        )
        validated = load_validate_config_result(
            config_files, build_hooks_dir=build_hooks_dir
        )
        presenter.warnings(validated.warnings)
        build_hook_source_root = admit_build_hook_source(
            validated,
            build_hooks_dir,
            output_dir,
            working_directory=Path.cwd(),
        )
        secret_session = HostSecretSession.from_configuration(validated)
        try:
            with secret_session:
                with _planning_providers(secret_session) as providers:
                    prepared = prepare_render_context(
                        output_dir,
                        configuration_result=validated,
                        build_hook_source_root=build_hook_source_root,
                        runtime_hooks_dir=runtime_hooks_dir,
                        acquirer=providers.acquirer,
                        local_acquirer=providers.local_acquirer,
                        canonical_wheel=providers.canonical_wheel,
                        tag_templates=validated.config.build.tags,
                        output_mode=validated.config.build.output,
                        options=options,
                        overwrite=overwrite,
                        working_directory=Path.cwd(),
                    )
                presenter.warnings(prepared.warnings)
                presenter.warnings(secret_session.drain_warnings())
        finally:
            presenter.warnings(secret_session.drain_warnings())
    except ConfigurationServiceError as error:
        presenter.diagnostics("Configuration is invalid", error.diagnostics)
        raise typer.Exit(code=1) from error
    except (CanonicalWheelError, HostRenderServiceError) as error:
        presenter.diagnostics("Unable to render build context", error.diagnostics)
        raise typer.Exit(code=1) from error
    except HostSecretSessionError as error:
        _render_secret_session_failure(presenter, error)
        raise typer.Exit(code=1) from error

    if dry_run:
        presenter.plan_preview(
            prepared.plan,
            lock_result=prepared.lock_result,
            options=options,
            output_plan=prepared.output_plan,
        )
        return
    presenter.render_success(
        output_dir,
        options=options,
        lock_changed=prepared.lock_result.changed,
    )


@app.command("build", context_settings=HELP_CONTEXT_SETTINGS)
def build(
    config_files: Annotated[
        list[Path],
        typer.Option(
            "--file",
            "-f",
            help="TOML configuration layer; repeat to merge in order.",
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
    ssh: Annotated[
        bool,
        typer.Option(
            "--ssh",
            help=(
                "Allow custom-node installation to use the host's default SSH "
                "agent and known-hosts trust."
            ),
        ),
    ] = False,
    cache_from_specs: Annotated[
        list[str] | None,
        typer.Option(
            "--cache-from",
            help=(
                "Docker Buildx external cache import specification. "
                "May be provided once."
            ),
            metavar="CACHE-SPEC",
        ),
    ] = None,
    cache_to_specs: Annotated[
        list[str] | None,
        typer.Option(
            "--cache-to",
            help=(
                "Docker Buildx external cache export specification. "
                "May be provided once."
            ),
            metavar="CACHE-SPEC",
        ),
    ] = None,
    build_hooks_dir: Annotated[
        Path | None,
        typer.Option(
            "--build-hooks-dir",
            help="Directory containing referenced build hook files.",
            metavar="DIR",
        ),
    ] = None,
    runtime_hooks_dir: Annotated[
        Path | None,
        typer.Option(
            "--runtime-hooks-dir",
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
            help="Require a matching lock without resolving or updating the context.",
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
    from comfyui_docker_helper.host.secret_session import (
        HostSecretSession,
        HostSecretSessionError,
    )

    config_files = _require_at_least_one(config_files, "--file/-f")
    cache_from = _admit_single_cache_spec(cache_from_specs or [], "--cache-from")
    cache_to = _admit_single_cache_spec(cache_to_specs or [], "--cache-to")
    cli_tags = image_tags or []
    cli_output = _resolve_cli_build_output(load=load, push=push)
    presenter = default_host_presenter()

    try:
        validated = load_validate_config_result(
            config_files, build_hooks_dir=build_hooks_dir
        )
    except ConfigurationServiceError as error:
        presenter.diagnostics("Configuration is invalid", error.diagnostics)
        raise typer.Exit(code=1) from error
    presenter.warnings(validated.warnings)
    effective_tags = _resolve_effective_image_tags(
        cli_tags=cli_tags,
        config_tags=validated.config.build.tags,
        comfyui_selector=validated.config.comfyui.version,
    )
    use_ssh = _prepare_build_ssh_input(
        requested=ssh,
        result=validated,
        presenter=presenter,
    )
    effective_output = cli_output or validated.config.build.output

    try:
        options = PlanningOptions(locked=locked, upgrade_lock=upgrade_lock)
        build_hook_source_root = admit_build_hook_source(
            validated,
            build_hooks_dir,
            context_dir,
            working_directory=Path.cwd(),
        )
        secret_session = HostSecretSession.from_configuration(validated)
        try:
            with secret_session:
                with _planning_providers(secret_session) as providers:
                    prepared = prepare_render_context(
                        context_dir,
                        configuration_result=validated,
                        build_hook_source_root=build_hook_source_root,
                        runtime_hooks_dir=runtime_hooks_dir,
                        acquirer=providers.acquirer,
                        local_acquirer=providers.local_acquirer,
                        canonical_wheel=providers.canonical_wheel,
                        tag_templates=effective_tags,
                        output_mode=effective_output,
                        options=options,
                        overwrite=True,
                        working_directory=Path.cwd(),
                    )
                presenter.warnings(prepared.warnings)
                presenter.warnings(secret_session.drain_warnings())
                credential_bindings = tuple(
                    FileSecretBinding(
                        secret_id,
                        secret_session.snapshot_git_credential(secret_id),
                    )
                    for secret_id in git_credential_secret_ids(
                        prepared.plan.custom_nodes
                    )
                )
                presenter.warnings(secret_session.drain_warnings())

                known_hosts_bindings = (
                    _collect_default_known_hosts_bindings() if use_ssh else ()
                )

                buildx_output = prepared.output_plan
                if buildx_output is None:  # pragma: no cover
                    raise RuntimeError("host build requires a Buildx output plan")
                platforms = (prepared.plan.toolchain.platform,)
                presenter.build_start(
                    context_dir,
                    output_plan=buildx_output,
                    platforms=platforms,
                )
                build_image_with_buildx(
                    image_tags=buildx_output.tags,
                    output=buildx_output.output,
                    context_dir=context_dir,
                    platforms=platforms,
                    cwd=Path.cwd(),
                    log=_write_external_build_line,
                    forward_default_ssh=use_ssh,
                    file_secret_bindings=(
                        *credential_bindings,
                        *known_hosts_bindings,
                    ),
                    cache_from=cache_from,
                    cache_to=cache_to,
                )
                presenter.build_complete(output_plan=buildx_output)
        finally:
            presenter.warnings(secret_session.drain_warnings())
    except ConfigurationServiceError as error:
        presenter.diagnostics("Configuration is invalid", error.diagnostics)
        raise typer.Exit(code=1) from error
    except (CanonicalWheelError, HostRenderServiceError) as error:
        presenter.diagnostics("Unable to prepare image build", error.diagnostics)
        raise typer.Exit(code=1) from error
    except HostSecretSessionError as error:
        _render_secret_session_failure(presenter, error)
        raise typer.Exit(code=1) from error
    except BuildxBuildError as error:
        presenter.failure("Image build failed", str(error))
        raise typer.Exit(code=error.exit_code) from error


def _planning_providers(secret_session: HostSecretSession):
    binding = secret_session.git_binding()
    if binding is None:
        return default_planning_providers()
    return default_planning_providers(git_credential_binding=binding)


def _write_external_build_line(message: str) -> None:
    """Forward one library-yielded BuildKit line without presentation filtering."""
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _render_secret_session_failure(
    presenter: HostPresenter,
    error: HostSecretSessionError,
) -> None:
    messages = {
        "environment_unavailable": "the configured environment source is unavailable",
        "source_unavailable": "the configured source is unavailable",
        "invalid_value": (
            "the configured Git credential value must be non-empty, no more than "
            f"{GIT_CREDENTIAL_VALUE_MAX_BYTES:,} bytes, and contain no NUL, carriage "
            "return, or newline characters"
        ),
        "session_create_failed": "the private Secret session could not be created",
        "snapshot_failed": "the configured Secret could not be prepared",
        "cleanup_failed": "the private Secret session could not be cleaned up",
    }
    path = ("secrets",) if error.secret_name is None else ("secrets", error.secret_name)
    presenter.diagnostics(
        "Unable to access configured secrets",
        (
            Diagnostic(
                path,
                f"secret.{error.code}",
                messages.get(error.code, "the private Secret session failed"),
            ),
        ),
    )


def _prepare_build_ssh_input(
    *,
    requested: bool,
    result: ConfigurationResult,
    presenter: HostPresenter,
) -> bool:
    if not requested:
        return False
    if not any(
        isinstance(node, FinalGitCustomNodeConfig)
        for node in result.config.comfyui.custom_nodes
    ):
        presenter.warning(
            "--ssh ignored because the effective configuration has no direct-Git "
            "custom nodes"
        )
        return False
    # Native Windows has no POSIX socket environment contract. Keep BuildKit's
    # default agent selection opaque and let Docker report unsupported setups.
    if _platform_name != "nt" and not os.environ.get("SSH_AUTH_SOCK"):
        raise typer.BadParameter(
            "requires a non-empty SSH_AUTH_SOCK environment variable",
            param_hint="--ssh",
        )
    return True


def _collect_default_known_hosts_bindings() -> tuple[FileSecretBinding, ...]:
    # Windows has no project-owned system known-hosts discovery contract.
    return tuple(
        FileSecretBinding(
            secret_id=descriptor.secret_id,
            source=source,
        )
        for descriptor in KNOWN_HOSTS_MOUNTS
        if _platform_name != "nt" or descriptor.scope == "user"
        if (source := Path(descriptor.default_source).expanduser()).exists()
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


def _admit_single_cache_spec(values: list[str], param_hint: str) -> str | None:
    if len(values) > 1:
        raise typer.BadParameter(
            "may be provided at most once",
            param_hint=param_hint,
        )
    if not values:
        return None
    value = values[0]
    if not is_argv_value(value):
        raise typer.BadParameter(
            "must be non-empty and must not contain control characters",
            param_hint=param_hint,
        )
    return value


def _resolve_effective_image_tags(
    *,
    cli_tags: list[str],
    config_tags: list[str],
    comfyui_selector: str,
) -> tuple[str, ...]:
    _validate_cli_image_tags(cli_tags, comfyui_selector=comfyui_selector)
    tags = tuple(cli_tags or config_tags)
    if not tags:
        raise typer.BadParameter(
            "must provide at least one image tag with --tag/-t or [build].tags",
            param_hint="--tag/-t",
        )
    return tags


def _validate_cli_image_tags(tags: list[str], *, comfyui_selector: str) -> None:
    issues = validate_publication_tags(
        tags,
        release_available=static_release_availability(comfyui_selector),
    )
    if issues:
        first = issues[0]
        raise typer.BadParameter(
            f"tag {first.index + 1}: {first.message}",
            param_hint="--tag/-t",
        )
