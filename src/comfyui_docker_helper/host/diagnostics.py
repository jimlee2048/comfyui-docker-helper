"""Rich rendering for host-side configuration diagnostics."""

from pathlib import Path

from rich.console import Console
from rich.text import Text

from comfyui_docker_helper.config import Diagnostic, DiagnosticSeverity
from comfyui_docker_helper.config.plan import (
    GitCustomNodePlan,
    RegistryCustomNodePlan,
    RenderPlan,
)


def render_configuration_diagnostics(
    config_path: str | Path,
    diagnostics: tuple[Diagnostic, ...],
    *,
    console: Console | None = None,
) -> None:
    """Render every configuration diagnostic as safe human-readable text."""
    output = console or Console(stderr=True, highlight=False)
    errors = tuple(
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.severity == DiagnosticSeverity.ERROR
    )
    warnings = tuple(
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.severity == DiagnosticSeverity.WARNING
    )
    if errors:
        output.print(Text(f"Configuration is invalid: {config_path}", style="bold red"))
        _render_diagnostic_items(errors, output)
    if warnings:
        output.print(
            Text(f"Configuration has warnings: {config_path}", style="bold yellow")
        )
        _render_diagnostic_items(warnings, output)


def render_configuration_warnings(
    config_path: str | Path,
    diagnostics: tuple[Diagnostic, ...],
    *,
    console: Console | None = None,
) -> None:
    """Render non-fatal configuration warnings as safe human-readable text."""
    warnings = tuple(
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.severity == DiagnosticSeverity.WARNING
    )
    if warnings:
        render_configuration_diagnostics(config_path, warnings, console=console)


def _render_diagnostic_items(
    diagnostics: tuple[Diagnostic, ...],
    output: Console,
) -> None:
    for diagnostic in diagnostics:
        output.print()
        output.print(Text(f"[{_format_path(diagnostic.path)}]", style="bold yellow"))
        output.print(Text(f"  {diagnostic.message} ({diagnostic.code})"))


def render_plan_preview(
    plan: RenderPlan,
    *,
    console: Console | None = None,
) -> None:
    """Render a deterministic human-readable dry-run preview."""
    output = console or Console(highlight=False, markup=False)
    output.print("Build plan preview")
    output.print(f"Base image: {plan.base_image}")
    output.print()
    output.print("Paths:")
    output.print(f"  Workspace: {plan.paths.workspace}")
    output.print(f"  ComfyUI: {plan.paths.comfyui}")
    output.print(f"  Virtualenv: {plan.paths.venv}")
    output.print()
    output.print("OS packages:")
    output.print(f"  {_format_list(plan.os_packages)}")
    output.print()
    output.print("Python:")
    output.print(f"  Version: {plan.python.version}")
    output.print(f"  uv image tag: {plan.python.uv_version}")
    output.print(f"  Extra packages: {_format_list(plan.python.extra_packages)}")
    output.print()
    output.print("PyTorch:")
    output.print(f"  Version: {plan.pytorch.version}")
    output.print(f"  Wheel tag: {plan.pytorch.wheel_tag}")
    output.print(f"  Index URL: {plan.pytorch.index_base_url}/{plan.pytorch.wheel_tag}")
    output.print(f"  Requirements: {_format_list(plan.pytorch.requirements)}")
    output.print()
    output.print("ComfyUI:")
    output.print(f"  comfy-cli: {plan.comfyui.cli_requirement}")
    output.print(f"  ComfyUI version: {plan.comfyui.version}")
    output.print(
        f"  Manager: {'enabled' if plan.comfyui.install_manager else 'disabled'}"
    )
    output.print(f"  Install args: {_format_list(plan.comfyui.install_arguments)}")
    output.print(f"  Launch command: {_format_list(plan.comfyui.launch_command)}")
    output.print()
    output.print("Environment:")
    if plan.environment:
        for variable in plan.environment:
            output.print(f"  {variable.name}={variable.value}")
    else:
        output.print("  none")
    output.print()
    output.print("Custom nodes:")
    output.print(f"  Update cache: {'yes' if plan.custom_nodes.update_cache else 'no'}")
    if plan.custom_nodes.items:
        for index, node in enumerate(plan.custom_nodes.items):
            output.print(f"  [{index}] {_format_node(node)}")
            output.print(f"      target: {node.target}")
            output.print(f"      pre hooks: {_format_list(node.pre_install_scripts)}")
            output.print(f"      post hooks: {_format_list(node.post_install_scripts)}")
    else:
        output.print("  none")
    output.print()
    output.print("Files:")
    output.print(f"  Default downloader: {plan.files.downloader.default}")
    if plan.files.items:
        for index, item in enumerate(plan.files.items):
            output.print(f"  [{index}] {item.url}")
            output.print(f"      target: {item.target}")
            output.print(f"      downloader: {item.downloader}")
            output.print(f"      overwrite: {'yes' if item.overwrite else 'no'}")
    else:
        output.print("  none")
    output.print()
    output.print("Build arguments:")
    for argument in plan.build_arguments:
        output.print(f"  {argument.name}={argument.value}")
    output.print()
    output.print("Layers:")
    for layer in plan.layers:
        output.print(f"  - {layer.value}")
    output.print()
    output.print("Output manifest:")
    output.print("  - config.toml [file]")
    output.print("  - config.lock.toml [file]")
    for artifact in plan.output_manifest.all:
        condition = f" ({artifact.condition.value})" if artifact.condition else ""
        output.print(f"  - {artifact.path} [{artifact.kind.value}]{condition}")


def _format_path(path: tuple[str | int, ...]) -> str:
    return ".".join(str(part) for part in path) if path else "config"


def _format_list(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "none"


def _format_node(node: RegistryCustomNodePlan | GitCustomNodePlan) -> str:
    if isinstance(node, RegistryCustomNodePlan):
        version = f"@{node.version}" if node.version is not None else ""
        return f"registry {node.id}{version}"
    ref = f"#{node.ref}" if node.ref is not None else ""
    return f"git {node.url}{ref}"
