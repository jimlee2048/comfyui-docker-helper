"""Safe host diagnostics and canonical BuildPlan preview."""

from pathlib import Path

from rich.console import Console
from rich.text import Text

from comfyui_docker_helper.config.build_plan import BuildPlan, build_plan_digest
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticComparison,
    DiagnosticComparisonSite,
    DiagnosticPath,
    DiagnosticSeverity,
    SourceLocation,
)
from comfyui_docker_helper.host.buildx import BuildxOutputPlan
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
            Text(
                f"Unable to process configuration: {_safe_text(str(config_path))}",
                style="bold red",
            )
        )
        _render_items(errors, output)
    if warnings:
        output.print(
            Text(
                f"Configuration has warnings: {_safe_text(str(config_path))}",
                style="bold yellow",
            )
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
    output_plan: BuildxOutputPlan | None,
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
    output.print("Buildx output")
    if output_plan is None:
        output.print("  None")
    else:
        output.print(f"  Mode: {output_plan.output}")
        output.print("  Tags:")
        for tag in output_plan.tags:
            output.print(f"    - {tag}")


def _render_items(diagnostics: tuple[Diagnostic, ...], output: Console) -> None:
    for diagnostic in diagnostics:
        output.print()
        output.print(Text(f"[{_format_path(diagnostic.path)}]", style="bold yellow"))
        output.print(
            Text(f"  {_safe_text(diagnostic.message)} ({_safe_text(diagnostic.code)})")
        )
        context = diagnostic.source_context
        if isinstance(context, SourceLocation):
            _render_site(
                "Source",
                DiagnosticComparisonSite(location=context),
                output,
            )
        elif isinstance(context, DiagnosticComparison):
            _render_site("Earlier", context.earlier, output)
            _render_site("Later", context.later, output)
        if diagnostic.hint is not None:
            output.print(Text(f"  Hint: {_safe_text(diagnostic.hint)}"))


def _render_site(
    label: str,
    site: DiagnosticComparisonSite,
    output: Console,
) -> None:
    output.print(Text(f"  {label}:"))
    output.print(Text(f"    File: {_safe_text(site.location.source.label)}"))
    output.print(Text(f"    Field: {_format_path(site.location.path)}"))
    if site.display_value is not None:
        output.print(Text(f"    Value: {_safe_text(site.display_value)}"))


def _format_path(path: DiagnosticPath) -> str:
    if not path:
        return "config"
    return ".".join(_safe_text(str(part)) for part in path)


def _safe_text(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        if character == "\\":
            escaped.append("\\\\")
            continue
        if character.isprintable():
            escaped.append(character)
            continue
        codepoint = ord(character)
        if character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif codepoint <= 0xFF:
            escaped.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return "".join(escaped)
