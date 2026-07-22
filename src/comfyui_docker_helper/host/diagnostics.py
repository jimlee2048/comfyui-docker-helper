"""Safe host diagnostics and canonical BuildPlan preview."""

from pathlib import Path

from rich.console import Console
from rich.text import Text

from comfyui_docker_helper.config.build_plan import BuildPlan, build_plan_digest
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticSeverity
from comfyui_docker_helper.host.render_service import PlanningOptions


def render_configuration_diagnostics(
    config_path: str | Path,
    diagnostics: tuple[Diagnostic, ...],
    *,
    console: Console | None = None,
) -> None:
    output = console or Console(stderr=True, highlight=False)
    errors = tuple(
        item for item in diagnostics if item.severity == DiagnosticSeverity.ERROR
    )
    warnings = tuple(
        item for item in diagnostics if item.severity == DiagnosticSeverity.WARNING
    )
    if errors:
        output.print(
            Text(f"Unable to process configuration: {config_path}", style="bold red")
        )
        _render_items(errors, output)
    if warnings:
        output.print(
            Text(f"Configuration has warnings: {config_path}", style="bold yellow")
        )
        _render_items(warnings, output)


def render_configuration_warnings(
    config_path: str | Path,
    diagnostics: tuple[Diagnostic, ...],
    *,
    console: Console | None = None,
) -> None:
    warnings = tuple(
        item for item in diagnostics if item.severity == DiagnosticSeverity.WARNING
    )
    if warnings:
        render_configuration_diagnostics(config_path, warnings, console=console)


def render_plan_preview(
    plan: BuildPlan,
    *,
    lock_result: AcceptedCanonicalLock,
    options: PlanningOptions,
    console: Console | None = None,
) -> None:
    """Render only exact BuildPlan authority; never reconstruct from config/lock."""
    output = console or Console(highlight=False, markup=False)
    output.print("Build plan preview")
    output.print(f"  Digest: {build_plan_digest(plan)}")
    output.print(f"  Lock mode: {options.policy.value}")
    output.print(f"  Write: {'yes' if options.writes else 'no'}")
    output.print(f"  Changed: {'yes' if lock_result.changed else 'no'}")
    output.print(f"  Platform: {plan.toolchain.platform}")
    output.print(f"  CUDA image: {plan.toolchain.cuda_image.reference}")
    output.print(f"  uv image: {plan.toolchain.uv_image.reference}")
    output.print(f"  Python: {plan.toolchain.python.version}")
    output.print(f"  PyTorch channel: {plan.toolchain.pytorch_channel}")
    output.print("  PyTorch group:")
    for package in plan.application.pytorch.packages:
        output.print(f"    - {package.requirement}")
    output.print(f"  ComfyUI commit: {plan.application.comfyui.commit}")
    comfy_cli = plan.toolchain.tool_store.comfy_cli
    output.print(
        f"  comfy-cli: {comfy_cli.version if comfy_cli is not None else 'disabled'}"
    )
    output.print("  Custom nodes:")
    for node in plan.custom_nodes.nodes:
        if node.type == "registry":
            output.print(f"    - registry {node.id}@{node.version}")
        else:
            output.print(f"    - git {node.url}@{node.commit} -> {node.target}")


def _render_items(diagnostics: tuple[Diagnostic, ...], output: Console) -> None:
    for diagnostic in diagnostics:
        path = ".".join(str(part) for part in diagnostic.path) or "config"
        output.print()
        output.print(Text(f"[{path}]", style="bold yellow"))
        output.print(Text(f"  {diagnostic.message} ({diagnostic.code})"))
