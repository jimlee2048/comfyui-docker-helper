"""Container helper command group."""

from pathlib import Path
from typing import Annotated

import typer

from comfyui_docker_helper.cli_settings import HELP_CONTEXT_SETTINGS
from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    CustomNodesPhase,
    FilesPhase,
    ToolchainPhase,
)
from comfyui_docker_helper.container.comfyui_installer import install_comfyui
from comfyui_docker_helper.container.custom_node_installer import install_custom_nodes
from comfyui_docker_helper.container.download_files import download_files
from comfyui_docker_helper.container.entrypoint import run_entrypoint
from comfyui_docker_helper.container.phase_inputs import (
    MATERIALIZED_BUILD_PLAN_PATH,
    PhaseInputAdmission,
    PhasePayload,
)
from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
)

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
    """Download files declared by the generated build phase."""
    (files,) = _admit_phases(
        ((phase, "files", FilesPhase),),
        build_plan_digest,
    )
    download_files(files)


@app.command("install-comfyui", context_settings=HELP_CONTEXT_SETTINGS)
def install_comfyui_command(
    application_phase: Annotated[
        Path,
        typer.Option(
            "--application-phase", help="Materialized application phase JSON."
        ),
    ],
    toolchain_phase: Annotated[
        Path,
        typer.Option("--toolchain-phase", help="Materialized toolchain phase JSON."),
    ],
    build_plan_digest: Annotated[
        str,
        typer.Option(
            "--build-plan-digest",
            help="Expected owning BuildPlan SHA-256 digest.",
        ),
    ],
    constraints: Annotated[
        Path,
        typer.Option("--constraints", help="Materialized managed constraints file."),
    ] = Path("/opt/cdh/build/python-package-constraints.txt"),
    resolution_manifest: Annotated[
        Path,
        typer.Option(
            "--resolution-manifest",
            help="Materialized PyTorch source-routing manifest.",
        ),
    ] = Path("/opt/cdh/build/pyproject.toml"),
) -> None:
    """Install exact official ComfyUI and its complete requirements."""
    application, toolchain = _admit_phases(
        (
            (application_phase, "application", ApplicationPhase),
            (toolchain_phase, "toolchain", ToolchainPhase),
        ),
        build_plan_digest,
    )
    install_comfyui(
        application,
        toolchain,
        runtime=ContainerRuntime.from_env(),
        constraints_path=constraints,
        resolution_manifest_path=resolution_manifest,
    )


@app.command("install-custom-nodes", context_settings=HELP_CONTEXT_SETTINGS)
def install_custom_nodes_command(
    custom_nodes_phase: Annotated[
        Path,
        typer.Option(
            "--custom-nodes-phase",
            help="Materialized custom-nodes phase JSON.",
        ),
    ],
    application_phase: Annotated[
        Path,
        typer.Option(
            "--application-phase",
            help="Materialized application phase JSON.",
        ),
    ],
    build_plan_digest: Annotated[
        str,
        typer.Option(
            "--build-plan-digest",
            help="Expected owning BuildPlan SHA-256 digest.",
        ),
    ],
    constraints: Annotated[
        Path,
        typer.Option("--constraints", help="Materialized managed constraints file."),
    ] = Path("/opt/cdh/build/python-package-constraints.txt"),
    hooks_directory: Annotated[
        Path,
        typer.Option(
            "--hooks-directory",
            help="Materialized custom-node hook directory.",
        ),
    ] = Path("/opt/cdh/build/inputs"),
) -> None:
    """Install the exact ordered Registry and direct-Git custom nodes."""
    custom_nodes, application = _admit_phases(
        (
            (custom_nodes_phase, "custom-nodes", CustomNodesPhase),
            (application_phase, "application", ApplicationPhase),
        ),
        build_plan_digest,
    )
    install_custom_nodes(
        custom_nodes,
        application,
        runtime=ContainerRuntime.from_env(),
        constraints_path=constraints,
        hooks_directory=hooks_directory,
    )


@app.command("entrypoint", context_settings=HELP_CONTEXT_SETTINGS)
def entrypoint_command() -> None:
    """Start ComfyUI through the cdh runtime entrypoint."""

    runtime = ContainerRuntime.from_env()
    raise typer.Exit(code=run_entrypoint(runtime=runtime))


def _admit_phases(
    inputs: tuple[tuple[Path, str, type[PhasePayload]], ...],
    digest: str,
) -> tuple[PhasePayload, ...]:
    try:
        admission = PhaseInputAdmission.from_path(
            MATERIALIZED_BUILD_PLAN_PATH,
            expected_build_plan_digest=digest,
        )
        phases = tuple(admission.load(path, name) for path, name, _ in inputs)
    except ValueError as error:
        raise ContainerCommandError(str(error)) from error
    for phase, (_, name, expected_type) in zip(phases, inputs, strict=True):
        if not isinstance(phase, expected_type):  # pragma: no cover - schema owns it.
            raise ContainerCommandError(f"{name} phase input has the wrong payload")
    return phases
