"""Bounded presentation for operator-facing host commands."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
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

_WIDE_COMPARISON_MIN_WIDTH = 100


@dataclass(frozen=True, slots=True)
class HostPresenter:
    """Present one host command through independently routed output streams."""

    stdout: Console
    stderr: Console

    def diagnostics(
        self,
        title: str,
        items: tuple[Diagnostic, ...],
    ) -> None:
        """Render expected structured failures to stderr."""
        errors = tuple(
            item for item in items if item.severity == DiagnosticSeverity.ERROR
        )
        if errors:
            self._render_diagnostic_group(
                f"Error: {_safe_text(title)}",
                errors,
                style="bold red",
            )

    def warnings(self, items: tuple[Diagnostic, ...]) -> None:
        """Render structured warnings to stderr."""
        warnings = tuple(
            item for item in items if item.severity == DiagnosticSeverity.WARNING
        )
        if warnings:
            self._render_diagnostic_group(
                "Warnings",
                warnings,
                style="bold yellow",
            )

    def warning(self, message: str) -> None:
        """Render one ordinary expected warning to stderr."""
        self.stderr.print(
            Text(f"Warning: {_safe_text(message)}", style="bold yellow"),
            soft_wrap=True,
        )

    def failure(self, title: str, message: str) -> None:
        """Render one ordinary expected failure to stderr."""
        self.stderr.print(
            Text(f"Error: {_safe_text(title)}", style="bold red"),
            soft_wrap=True,
        )
        self.stderr.print(Text(f"  {_safe_text(message)}"), soft_wrap=True)

    def validate_success(self, config_files: Sequence[str | Path]) -> None:
        """Render the interactive-only successful validation summary."""
        if not self.stdout.is_terminal:
            return
        self.stdout.print(Text("Configuration valid", style="bold green"))
        if len(config_files) == 1:
            self.stdout.print(
                Text(f"  File: {_safe_text(str(config_files[0]))}"),
                soft_wrap=True,
            )
        else:
            self.stdout.print(Text(f"  Files: {len(config_files)}"))

    def render_success(
        self,
        output_dir: str | Path,
        *,
        options: PlanningOptions,
        lock_changed: bool,
    ) -> None:
        """Render the interactive-only context result, excluding dry runs."""
        if not self.stdout.is_terminal or options.dry_run:
            return
        if options.check:
            title = "Build context is up to date"
        elif options.locked:
            title = "Build context verified"
        else:
            title = "Build context rendered"
        self.stdout.print(Text(title, style="bold green"))
        self.stdout.print(
            Text(f"  Context: {_safe_text(str(output_dir))}"),
            soft_wrap=True,
        )
        self.stdout.print(Text(f"  Lock: {'updated' if lock_changed else 'unchanged'}"))

    def plan_preview(
        self,
        plan: BuildPlan,
        *,
        lock_result: AcceptedCanonicalLock,
        options: PlanningOptions,
        output_plan: BuildxOutputPlan | None,
    ) -> None:
        """Render exact BuildPlan authority to stdout in every output mode."""
        self._stdout_heading("Build plan preview")
        self._stdout_row("Digest", build_plan_digest(plan))
        self._stdout_row("Lock mode", options.policy.value)
        self._stdout_row("Write", "yes" if options.writes else "no")
        self._stdout_row("Changed", "yes" if lock_result.changed else "no")
        self._stdout_row("Platform", plan.toolchain.platform)
        self._stdout_row("CUDA image", plan.toolchain.cuda_image.reference)
        self._stdout_row("uv image", plan.toolchain.uv_image.reference)
        self._stdout_row("Python", plan.toolchain.python.version)
        self._stdout_row("PyTorch channel", plan.toolchain.pytorch_channel)
        self.stdout.print(Text("  PyTorch group:"))
        for package in plan.application.pytorch.packages:
            self.stdout.print(
                Text(f"    - {_safe_text(package.requirement)}"),
                soft_wrap=True,
            )
        self._stdout_row("ComfyUI commit", plan.application.comfyui.commit)
        comfy_cli = plan.toolchain.tool_store.comfy_cli
        self._stdout_row(
            "comfy-cli",
            comfy_cli.version if comfy_cli is not None else "disabled",
        )
        self.stdout.print(Text("  Custom nodes:"))
        for node in plan.custom_nodes.nodes:
            if node.type == "registry":
                summary = f"registry {node.id}@{node.version}"
            else:
                summary = f"git {node.url}@{node.commit} -> {node.target}"
            self.stdout.print(Text(f"    - {_safe_text(summary)}"), soft_wrap=True)
        self._stdout_heading("Buildx output")
        if output_plan is None:
            self.stdout.print(Text("  None"))
            return
        self._stdout_row("Mode", output_plan.output)
        self.stdout.print(Text("  Tags:"))
        for tag in output_plan.tags:
            self.stdout.print(Text(f"    - {_safe_text(tag)}"), soft_wrap=True)

    def build_start(
        self,
        context_dir: str | Path,
        *,
        output_plan: BuildxOutputPlan,
        platforms: Sequence[str],
    ) -> None:
        """Render the host-owned summary before the external BuildKit stream."""
        self._stdout_heading("Starting image build")
        self._stdout_row("Context", str(context_dir))
        self._stdout_row("Output", output_plan.output)
        self._stdout_row("Platforms", ", ".join(platforms))
        self._stdout_row("Tags", ", ".join(output_plan.tags))

    def build_complete(self, *, output_plan: BuildxOutputPlan) -> None:
        """Render the host-owned summary after a successful BuildKit stream."""
        self._stdout_heading("Image build complete", style="bold green")
        self._stdout_row(
            "Pushed" if output_plan.output == "push" else "Loaded",
            ", ".join(output_plan.tags),
        )

    def _render_diagnostic_group(
        self,
        title: str,
        items: tuple[Diagnostic, ...],
        *,
        style: str,
    ) -> None:
        self.stderr.print(Text(title, style=style), soft_wrap=True)
        for diagnostic in items:
            self.stderr.print()
            self._render_diagnostic(diagnostic)

    def _render_diagnostic(self, diagnostic: Diagnostic) -> None:
        self.stderr.print(
            Text(f"Field: {_format_path(diagnostic.path)}", style="bold"),
            soft_wrap=True,
        )
        self.stderr.print(Text(f"  {_safe_text(diagnostic.message)}"), soft_wrap=True)
        context = diagnostic.source_context
        if isinstance(context, SourceLocation):
            self._render_plain_site(
                "Source",
                DiagnosticComparisonSite(location=context),
            )
        elif isinstance(context, DiagnosticComparison):
            if self.stderr.is_terminal:
                self.stderr.print(self._comparison_card(context))
            else:
                self._render_plain_site("Earlier", context.earlier)
                self._render_plain_site("Later", context.later)
        if diagnostic.hint is not None:
            self.stderr.print(
                Text(f"Hint: {_safe_text(diagnostic.hint)}"),
                soft_wrap=True,
            )

    def _comparison_card(self, context: DiagnosticComparison) -> Panel:
        layout = Table.grid(expand=True)
        if self.stderr.width >= _WIDE_COMPARISON_MIN_WIDTH:
            layout.add_column(ratio=1)
            layout.add_column(ratio=1)
            layout.add_row(
                _site_renderable("Earlier", context.earlier),
                _site_renderable("Later", context.later),
            )
        else:
            layout.add_column(ratio=1)
            layout.add_row(_site_renderable("Earlier", context.earlier))
            layout.add_row(Text())
            layout.add_row(_site_renderable("Later", context.later))
        return Panel(
            layout,
            box=box.ROUNDED,
            border_style="yellow",
            padding=(0, 1),
            safe_box=True,
        )

    def _render_plain_site(
        self,
        label: str,
        site: DiagnosticComparisonSite,
    ) -> None:
        self.stderr.print(Text(f"{label}:", style="bold"))
        self.stderr.print(
            Text(f"  File: {_safe_text(site.location.source.label)}"),
            soft_wrap=True,
        )
        self.stderr.print(
            Text(f"  Field: {_format_path(site.location.path)}"),
            soft_wrap=True,
        )
        if site.display_value is not None:
            self.stderr.print(
                Text(f"  Value: {_safe_text(site.display_value)}"),
                soft_wrap=True,
            )

    def _stdout_heading(self, value: str, *, style: str = "bold cyan") -> None:
        self.stdout.print(Text(_safe_text(value), style=style), soft_wrap=True)

    def _stdout_row(self, label: str, value: str) -> None:
        self.stdout.print(
            Text(f"  {label}: {_safe_text(value)}"),
            soft_wrap=True,
        )


def default_host_presenter() -> HostPresenter:
    """Bind one presenter to the command's current process streams."""
    return HostPresenter(
        stdout=_default_console(sys.stdout),
        stderr=_default_console(sys.stderr),
    )


def _default_console(stream: TextIO) -> Console:
    is_terminal = stream.isatty()
    no_color = "NO_COLOR" in os.environ
    return Console(
        file=stream,
        force_terminal=is_terminal,
        color_system=None if no_color or not is_terminal else "auto",
        no_color=no_color,
        highlight=False,
        markup=False,
    )


def _site_renderable(
    label: str,
    site: DiagnosticComparisonSite,
) -> RenderableType:
    rows: list[RenderableType] = [Text(label, style="bold")]
    rows.append(_labelled_text("File", site.location.source.label))
    rows.append(_labelled_text("Field", _raw_path(site.location.path)))
    if site.display_value is not None:
        rows.append(_labelled_text("Value", site.display_value))
    return Group(*rows)


def _labelled_text(label: str, value: str) -> Text:
    result = Text(f"{label}: ", style="bold")
    result.append(_safe_text(value))
    return result


def _format_path(path: DiagnosticPath) -> str:
    return _safe_text(_raw_path(path))


def _raw_path(path: DiagnosticPath) -> str:
    if not path:
        return "config"
    return ".".join(str(part) for part in path)


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
