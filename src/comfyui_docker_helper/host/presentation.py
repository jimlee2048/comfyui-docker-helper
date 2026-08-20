"""Bounded results and diagnostics for operator-facing Host commands."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from comfyui_docker_helper.cli_output.policy import (
    CliOutputSettings,
    OutputContextKind,
    OutputDetail,
    OutputPolicy,
    OutputStream,
    StreamCapabilities,
    detect_stream_capabilities,
)
from comfyui_docker_helper.cli_output.text import control_safe_text
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
from comfyui_docker_helper.host.path_display import display_host_path
from comfyui_docker_helper.host.render_service import PlanningOptions
from comfyui_docker_helper.host.workflow_display import (
    HostWorkflowDisplay,
    HostWorkflowSummary,
    host_phase_label,
)

_WIDE_COMPARISON_MIN_WIDTH = 100


@dataclass(frozen=True, slots=True)
class HostPresenter:
    """Present one host command through independently routed output streams."""

    stdout: Console
    stderr: Console
    policy: OutputPolicy
    working_directory: PurePath

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

    def workflow(self, title: str) -> HostWorkflowDisplay:
        """Create one command-scoped workflow display on the owned stderr."""
        return HostWorkflowDisplay(
            title=title,
            stderr=self.stderr,
            policy=self.policy,
        )

    def validate_success(self, config_files: Sequence[str | Path]) -> None:
        """Render ordered configuration layers in the admitted output modes."""
        if not self.policy.includes(OutputDetail.NORMAL):
            return
        capabilities = self.policy.capabilities(OutputStream.STDOUT)
        if not capabilities.is_terminal and not self.policy.includes(
            OutputDetail.VERBOSE
        ):
            return

        paths = tuple(self._display_path(path) for path in config_files)
        if capabilities.supports_unicode:
            root = Tree(self._stdout_text("Configuration valid", "bold green"))
            layers = root.add(
                self._stdout_text(
                    f"Configuration layers ({len(paths)}, merge order)",
                    "bold",
                )
            )
            for index, path in enumerate(paths, start=1):
                layers.add(Text(f"[{index}/{len(paths)}] {path}"))
            self.stdout.print(root, soft_wrap=True)
            return

        self.stdout.print(self._stdout_text("Configuration valid", "bold green"))
        self.stdout.print(Text(f"`-- Configuration layers ({len(paths)}, merge order)"))
        for index, path in enumerate(paths, start=1):
            branch = "`--" if index == len(paths) else "|--"
            self.stdout.print(Text(f"    {branch} [{index}/{len(paths)}] {path}"))

    def render_success(
        self,
        output_dir: str | Path,
        *,
        options: PlanningOptions,
        lock_changed: bool,
        workflow_summary: HostWorkflowSummary | None = None,
    ) -> None:
        """Render the optional context result, excluding dry runs."""
        if options.dry_run or not self.policy.includes(OutputDetail.NORMAL):
            return
        if options.check:
            title = "Build context is up to date"
        elif options.locked:
            title = "Build context verified"
        else:
            title = "Build context rendered"
        context = self._display_path(output_dir)
        lock = "updated" if lock_changed else "unchanged"
        if not self.policy.capabilities(OutputStream.STDOUT).is_terminal:
            self.stdout.print(Text(f"{title}: {context} (lock {lock})"), soft_wrap=True)
            return
        summary = workflow_summary or HostWorkflowSummary(())
        capabilities = self.policy.capabilities(OutputStream.STDOUT)
        if not capabilities.supports_unicode:
            lines: list[RenderableType] = [self._stdout_text(title, "bold green")]
            if summary.phases:
                lines.append(Text("|-- Preparation"))
                for index, completed in enumerate(summary.phases):
                    branch = "`--" if index == len(summary.phases) - 1 else "|--"
                    value = f"Completed: {host_phase_label(completed.phase)}"
                    if self.policy.includes(OutputDetail.VERBOSE):
                        value += f" ({completed.duration:.2f}s)"
                    lines.append(Text(f"|   {branch} {value}"))
            lines.append(Text(f"|-- Context: {context}"))
            lines.append(Text(f"`-- Lock: {lock}"))
            self.stdout.print(Group(*lines), soft_wrap=True)
            return

        tree = Tree(self._stdout_text(title, "bold green"))
        if summary.phases:
            preparation = tree.add(self._stdout_text("Preparation", "bold"))
            for completed in summary.phases:
                value = f"Completed: {host_phase_label(completed.phase)}"
                if self.policy.includes(OutputDetail.VERBOSE):
                    value += f" ({completed.duration:.2f}s)"
                preparation.add(self._stdout_text(value, "green"))
        tree.add(Text(f"Context: {context}"))
        tree.add(Text(f"Lock: {lock}"))
        self.stdout.print(tree, soft_wrap=True)

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
        if not self.policy.includes(OutputDetail.NORMAL):
            return
        self.stderr.print(
            self._stderr_text("Starting image build", "bold cyan"),
            soft_wrap=True,
        )
        if not self.policy.includes(OutputDetail.VERBOSE):
            return
        self._stderr_row("Context", self._display_path(context_dir), safe=True)
        self._stderr_row("Output", output_plan.output)
        self._stderr_row("Platforms", ", ".join(platforms))
        self._stderr_row("Tags", ", ".join(output_plan.tags))

    def build_complete(self, *, output_plan: BuildxOutputPlan) -> None:
        """Render the host-owned summary after a successful BuildKit stream."""
        if not self.policy.includes(OutputDetail.NORMAL):
            return
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
        if self.policy.includes(OutputDetail.DEBUG):
            self.stderr.print(
                Text(f"Code: {_safe_text(diagnostic.code)}"),
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
            if self.policy.capabilities(OutputStream.STDERR).supports_unicode:
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
                self._site_renderable("Earlier", context.earlier),
                self._site_renderable("Later", context.later),
            )
        else:
            layout.add_column(ratio=1)
            layout.add_row(self._site_renderable("Earlier", context.earlier))
            layout.add_row(Text())
            layout.add_row(self._site_renderable("Later", context.later))
        return Panel(
            layout,
            box=box.ROUNDED,
            border_style="yellow",
            padding=(0, 1),
            safe_box=True,
        )

    def _site_renderable(
        self,
        label: str,
        site: DiagnosticComparisonSite,
    ) -> RenderableType:
        rows: list[RenderableType] = [Text(label, style="bold")]
        rows.append(
            _labelled_safe_text(
                "File",
                self._display_path(site.location.source.label),
            )
        )
        rows.append(_labelled_text("Field", _raw_path(site.location.path)))
        if site.display_value is not None:
            rows.append(_labelled_text("Value", site.display_value))
        return Group(*rows)

    def _render_plain_site(
        self,
        label: str,
        site: DiagnosticComparisonSite,
    ) -> None:
        self.stderr.print(Text(f"{label}:", style="bold"))
        self.stderr.print(
            Text(f"  File: {self._display_path(site.location.source.label)}"),
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
        self.stdout.print(self._stdout_text(_safe_text(value), style), soft_wrap=True)

    def _stdout_row(self, label: str, value: str, *, safe: bool = False) -> None:
        self.stdout.print(
            Text(f"  {label}: {value if safe else _safe_text(value)}"),
            soft_wrap=True,
        )

    def _stdout_text(self, value: str, style: str) -> Text:
        capabilities = self.policy.capabilities(OutputStream.STDOUT)
        return Text(value, style=style if capabilities.supports_color else None)

    def _stderr_row(self, label: str, value: str, *, safe: bool = False) -> None:
        self.stderr.print(
            Text(f"  {label}: {value if safe else _safe_text(value)}"),
            soft_wrap=True,
        )

    def _stderr_text(self, value: str, style: str) -> Text:
        capabilities = self.policy.capabilities(OutputStream.STDERR)
        return Text(value, style=style if capabilities.supports_color else None)

    def _display_path(self, path: str | PurePath) -> str:
        return display_host_path(path, working_directory=self.working_directory)


def default_host_presenter(
    settings: CliOutputSettings | None = None,
) -> HostPresenter:
    """Bind one presenter to the command's current process streams."""
    settings = CliOutputSettings() if settings is None else settings
    stdout_capabilities = detect_stream_capabilities(sys.stdout)
    stderr_capabilities = detect_stream_capabilities(sys.stderr)
    policy = OutputPolicy(
        settings=settings,
        stdout=stdout_capabilities,
        stderr=stderr_capabilities,
        context=OutputContextKind.ONE_SHOT,
    )
    return HostPresenter(
        stdout=_default_console(sys.stdout, stdout_capabilities),
        stderr=_default_console(sys.stderr, stderr_capabilities),
        policy=policy,
        working_directory=Path.cwd(),
    )


def _default_console(
    stream: TextIO,
    capabilities: StreamCapabilities,
) -> Console:
    return Console(
        file=stream,
        force_terminal=capabilities.is_terminal,
        color_system="auto" if capabilities.supports_color else None,
        no_color=not capabilities.supports_color,
        highlight=False,
        markup=False,
    )


def _labelled_text(label: str, value: str) -> Text:
    result = Text(f"{label}: ", style="bold")
    result.append(_safe_text(value))
    return result


def _labelled_safe_text(label: str, value: str) -> Text:
    result = Text(f"{label}: ", style="bold")
    result.append(value)
    return result


def _format_path(path: DiagnosticPath) -> str:
    return _safe_text(_raw_path(path))


def _raw_path(path: DiagnosticPath) -> str:
    if not path:
        return "config"
    return ".".join(str(part) for part in path)


_safe_text = control_safe_text
