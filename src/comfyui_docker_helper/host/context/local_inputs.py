"""Host-only admission bundle for local build-file inputs."""

from __future__ import annotations

import hashlib
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from comfyui_docker_helper.config.authored.models import FinalLocalFileConfig
from comfyui_docker_helper.config.authored.service import ConfigurationResult
from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticError,
    DiagnosticSeverity,
)
from comfyui_docker_helper.config.planning.local_tree import (
    LocalTreeInventory,
    local_tree_digest,
)
from comfyui_docker_helper.config.planning.request import (
    FileRequest,
    LocalFileRequest,
)
from comfyui_docker_helper.filesystem.admission import (
    TreeAdmissionError,
    admit_local_source,
)
from comfyui_docker_helper.rendering.final_materializer import (
    LocalMaterializationSource,
)


class LocalInputAdmissionError(DiagnosticError):
    """Expected failure while admitting a configured local build source."""


@dataclass(frozen=True, slots=True)
class LocalFilePlanningInput:
    """Shape and optional content identity for one admitted regular file."""

    relative_target: PurePosixPath
    context_path: PurePosixPath
    content_lock: bool
    digest: str | None
    kind: Literal["file"] = "file"


@dataclass(frozen=True, slots=True)
class LocalTreePlanningInput:
    """Complete structural inventory for one admitted directory source."""

    relative_target: PurePosixPath
    context_path: PurePosixPath
    content_lock: bool
    root_mode: Literal["0755"]
    inventory: LocalTreeInventory
    tree_digest: str | None
    kind: Literal["tree"] = "tree"


type LocalPlanningInput = LocalFilePlanningInput | LocalTreePlanningInput


@dataclass(frozen=True, slots=True)
class LocalAdmissionBundle:
    """One process-local planning/materialization bundle for local declarations."""

    planning_inputs: tuple[LocalPlanningInput, ...]
    materialization_sources: tuple[LocalMaterializationSource, ...]
    warnings: tuple[Diagnostic, ...] = ()


def admit_local_inputs(
    result: ConfigurationResult,
    graph_files: tuple[FileRequest, ...],
    output: str | Path,
) -> LocalAdmissionBundle:
    """Admit all effective local declarations and return one shared bundle.

    This adapter owns configuration locations, source/output separation, and
    user-facing diagnostics.  The filesystem package only reports safe
    source-relative tree facts and never receives configuration policy.
    """
    output_path = _absolute_path(output)
    planning_inputs: list[LocalPlanningInput] = []
    materialization_sources: list[LocalMaterializationSource] = []
    warnings: list[Diagnostic] = []

    for index, (item, normalized, request) in enumerate(
        zip(result.config.files, result.domains.files, graph_files, strict=True)
    ):
        if not isinstance(item, FinalLocalFileConfig):
            continue
        if not isinstance(request, LocalFileRequest):
            raise LocalInputAdmissionError(
                (
                    Diagnostic(
                        ("files", index, "source"),
                        "render.local_source_projection_invalid",
                        "local source planning projection is invalid",
                    ),
                )
            )
        source = _resolve_source(result, item.source)
        source_path = ("files", index, "source")
        _ensure_source_output_separation(
            output_path,
            source,
            source_path,
        )
        try:
            admitted = admit_local_source(source, content_lock=item.content_lock)
        except (OSError, ValueError) as error:
            raise LocalInputAdmissionError(
                (_admission_diagnostic(index, error),)
            ) from error

        relative_target = PurePosixPath(normalized.relative_target)
        if admitted.kind == "file":
            if relative_target == PurePosixPath("."):
                raise LocalInputAdmissionError(
                    (
                        Diagnostic(
                            ("files", index, "target"),
                            "render.local_file_target_invalid",
                            "local file target must name an exact file below "
                            "COMFYUI_PATH",
                        ),
                    )
                )
            if admitted.file is None:
                raise AssertionError("file admission omitted its file record")
            context_path = PurePosixPath(request.context_path)
            planning_inputs.append(
                LocalFilePlanningInput(
                    relative_target=relative_target,
                    context_path=context_path,
                    content_lock=item.content_lock,
                    digest=admitted.file.digest,
                )
            )
        else:
            if admitted.tree is None:
                raise AssertionError("tree admission omitted its inventory")
            context_path = PurePosixPath(
                "build",
                "trees",
                hashlib.sha256(relative_target.as_posix().encode("utf-8")).hexdigest(),
            )
            tree_digest = (
                local_tree_digest(admitted.tree) if item.content_lock else None
            )
            planning_inputs.append(
                LocalTreePlanningInput(
                    relative_target=relative_target,
                    context_path=context_path,
                    content_lock=item.content_lock,
                    root_mode=admitted.tree.root_mode,
                    inventory=admitted.tree,
                    tree_digest=tree_digest,
                )
            )
            if admitted.tree.empty:
                warnings.append(
                    Diagnostic(
                        source_path,
                        "render.local_source_empty",
                        "local source directory is empty; its target directory "
                        "will still be present in the image",
                        DiagnosticSeverity.WARNING,
                    )
                )
        materialization_sources.append(LocalMaterializationSource(context_path, source))

    return LocalAdmissionBundle(
        tuple(planning_inputs), tuple(materialization_sources), tuple(warnings)
    )


def _resolve_source(result: ConfigurationResult, value: str) -> Path:
    locator = Path(value)
    source = locator if locator.is_absolute() else result.secret_file_base / locator
    return Path(os.path.abspath(source))


def _absolute_path(value: str | Path) -> Path:
    return Path(os.path.abspath(Path(value)))


def _ensure_source_output_separation(
    output: Path,
    source: Path,
    path: tuple[str | int, ...],
) -> None:
    try:
        output_resolved = output.resolve(strict=False)
        source_resolved = source.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise LocalInputAdmissionError(
            (
                Diagnostic(
                    path,
                    "render.input_output_inspect_failed",
                    "local source and output could not be compared",
                ),
            )
        ) from error
    if (
        output_resolved == source_resolved
        or output_resolved in source_resolved.parents
        or source_resolved in output_resolved.parents
    ):
        raise LocalInputAdmissionError(
            (
                Diagnostic(
                    path,
                    "render.input_output_overlap",
                    "output and local source must not overlap",
                ),
            )
        )


def _admission_diagnostic(index: int, error: BaseException) -> Diagnostic:
    path = ("files", index, "source")
    if isinstance(error, TreeAdmissionError):
        relative = error.relative_path
        if error.code == "reserved_member" and relative is not None:
            return Diagnostic(
                path,
                "file.reserved_source_component",
                f"local source member {relative.as_posix()!r} uses the reserved "
                "staging path component; rename or remove it (reserved for HTTP "
                "download staging)",
            )
        if error.code in {"member_name", "inventory_invalid"}:
            if relative is not None and _safe_relative_display(relative):
                return Diagnostic(
                    path,
                    "file.invalid_source_member",
                    f"local source member {relative.as_posix()!r} has an unsafe or "
                    "unrepresentable name",
                )
            return Diagnostic(
                path,
                "file.invalid_source_member",
                "local source contains an unsafe or unrepresentable member name",
            )
        if relative is not None:
            return Diagnostic(
                path,
                "file.invalid_source_member",
                f"local source member {relative.as_posix()!r} could not be admitted",
            )
    return Diagnostic(
        path,
        "render.local_source_unavailable",
        "local source must be a readable regular file or real directory without "
        "links or special nodes",
    )


def _safe_relative_display(path: PurePosixPath) -> bool:
    """Keep source-relative diagnostics free of controls and path escapes."""
    return all(
        component
        and component not in {".", ".."}
        and "\\" not in component
        and not any(unicodedata.category(character) == "Cc" for character in component)
        for component in path.parts
    )


__all__ = [
    "LocalAdmissionBundle",
    "LocalFilePlanningInput",
    "LocalInputAdmissionError",
    "LocalPlanningInput",
    "LocalTreePlanningInput",
    "admit_local_inputs",
]
