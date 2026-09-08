"""Host-owned selected source admission for local custom nodes."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from docker.utils.build import PatternMatcher, normalize_slashes

from comfyui_docker_helper.config.authored.models import FinalLocalCustomNodeConfig
from comfyui_docker_helper.config.authored.service import ConfigurationResult
from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticSeverity
from comfyui_docker_helper.config.planning.inputs.local_node import (
    LocalNodePlanningInput,
    local_node_context_path,
)
from comfyui_docker_helper.config.planning.local_tree import local_tree_digest
from comfyui_docker_helper.config.planning.request import (
    CustomNodeRequest,
    LocalNodeRequest,
)
from comfyui_docker_helper.filesystem.admission import (
    TreeAdmissionError,
    admit_local_tree,
    read_regular_absolute_file,
)
from comfyui_docker_helper.host.context.local_inputs import (
    LocalInputAdmissionError,
    _absolute_path,
    _admission_diagnostic,
    _ensure_source_output_separation,
    _resolve_source,
)
from comfyui_docker_helper.rendering.final_materializer import (
    LocalMaterializationSource,
)


@dataclass(frozen=True, slots=True)
class DockerIgnoreSelection:
    """One SDK policy frozen from the root control file's original bytes."""

    control_bytes: bytes | None = field(repr=False)
    _matcher: PatternMatcher = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        text = (
            self.control_bytes.decode("utf-8-sig")
            if self.control_bytes is not None
            else ""
        )
        patterns = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        object.__setattr__(self, "_matcher", PatternMatcher(patterns))

    def includes(self, relative_path: str) -> bool:
        return not self._matcher.matches(relative_path)

    def descends(self, relative_path: str) -> bool:
        # Preserve the SDK walker's deliberately limited negation pruning.
        return self.includes(relative_path) or any(
            pattern.exclusion
            and pattern.cleaned_pattern.startswith(normalize_slashes(relative_path))
            for pattern in self._matcher.patterns
        )


def read_node_selection(source: Path) -> DockerIgnoreSelection:
    control = source / ".dockerignore"
    try:
        control.lstat()
    except FileNotFoundError:
        data = None
    else:
        data = read_regular_absolute_file(control)
    return DockerIgnoreSelection(data)


@dataclass(frozen=True, slots=True)
class LocalNodeAdmissionBundle:
    planning_inputs: tuple[LocalNodePlanningInput, ...]
    materialization_sources: tuple[LocalMaterializationSource, ...]
    warnings: tuple[Diagnostic, ...] = ()


def admit_local_node_inputs(
    result: ConfigurationResult,
    graph_nodes: tuple[CustomNodeRequest, ...],
    output: str | Path,
) -> LocalNodeAdmissionBundle:
    inputs: list[LocalNodePlanningInput] = []
    sources: list[LocalMaterializationSource] = []
    warnings: list[Diagnostic] = []
    for index, (item, request) in enumerate(
        zip(result.config.comfyui.custom_nodes, graph_nodes, strict=True)
    ):
        if not isinstance(item, FinalLocalCustomNodeConfig):
            continue
        path = ("comfyui", "custom_nodes", index, "source")
        if not isinstance(request, LocalNodeRequest):
            raise LocalInputAdmissionError(
                (
                    Diagnostic(
                        path,
                        "render.local_source_projection_invalid",
                        "local node planning projection is invalid",
                    ),
                )
            )
        source = _resolve_source(result, item.source)
        _ensure_source_output_separation(_absolute_path(output), source, path)
        try:
            selection = read_node_selection(source)
            inventory = admit_local_tree(
                source, content_lock=item.content_lock, selection=selection
            )
        except (OSError, ValueError, re.error) as error:
            diagnostic = _admission_diagnostic(index, error)
            raise LocalInputAdmissionError(
                (
                    Diagnostic(
                        path,
                        diagnostic.code,
                        diagnostic.message
                        if isinstance(error, TreeAdmissionError)
                        else "local node source must be a readable real directory "
                        "with a safe UTF-8 root .dockerignore",
                    ),
                )
            ) from error
        context = local_node_context_path(request.target_dir)
        inputs.append(
            LocalNodePlanningInput(
                request.target_dir,
                context,
                item.content_lock,
                inventory,
                local_tree_digest(inventory) if item.content_lock else None,
            )
        )
        sources.append(
            LocalMaterializationSource(
                context,
                source,
                kind="tree",
                selection=selection,
                control_file_bytes=selection.control_bytes,
            )
        )
        if inventory.empty:
            warnings.append(
                Diagnostic(
                    path,
                    "render.local_source_empty",
                    "local node source directory is empty; its target directory "
                    "will still be present in the image",
                    DiagnosticSeverity.WARNING,
                )
            )
    return LocalNodeAdmissionBundle(tuple(inputs), tuple(sources), tuple(warnings))
