"""Validated complete baked runtime-hook tree acquisition inputs."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticError,
    DiagnosticSeverity,
)
from comfyui_docker_helper.config.hook_validation import (
    hook_lock_identity,
    validate_hook_relative_path,
)
from comfyui_docker_helper.config.runtime_hooks import (
    RUNTIME_HOOK_PHASE_DIRECTORY_NAMES,
    RuntimeHookEntryKind,
    classify_runtime_hook_entry,
    runtime_hook_phase_directory_list,
)
from comfyui_docker_helper.host.identity_providers import (
    LocalExecutableIdentityRequest,
)


@dataclass(frozen=True, slots=True)
class RuntimeHookInputs:
    source_root: Path | None
    requests: tuple[LocalExecutableIdentityRequest, ...]
    warnings: tuple[Diagnostic, ...] = ()


class RuntimeHookInputError(DiagnosticError):
    """The baked runtime-hook tree is not one safe executable input tree."""


def discover_runtime_hook_inputs(
    runtime_hooks_dir: str | Path | None,
    *,
    working_directory: str | Path | None,
) -> RuntimeHookInputs:
    """Validate and enumerate every baked runtime hook in canonical order."""
    if runtime_hooks_dir is None:
        return RuntimeHookInputs(None, ())
    base = Path.cwd() if working_directory is None else Path(working_directory)
    selected = Path(runtime_hooks_dir)
    candidate = selected if selected.is_absolute() else base / selected
    diagnostics: list[Diagnostic] = []
    warnings: list[Diagnostic] = []
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError as error:
        raise RuntimeHookInputError(
            (
                Diagnostic(
                    ("runtime_hooks_dir",),
                    "runtime_hooks.source_not_directory",
                    "runtime hook source must be an existing real directory",
                ),
            )
        ) from error
    except OSError as error:
        raise _error(
            ("runtime_hooks_dir",),
            "runtime_hooks.source_inspect_failed",
            "runtime hook source could not be inspected",
            error,
        ) from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeHookInputError(
            (
                Diagnostic(
                    ("runtime_hooks_dir",),
                    "runtime_hooks.source_not_directory",
                    "runtime hook source must be an existing real directory",
                ),
            )
        )
    try:
        root = candidate.resolve(strict=True)
        children = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    except OSError as error:
        raise _error(
            ("runtime_hooks_dir",),
            "runtime_hooks.source_read_failed",
            "runtime hook source could not be read",
            error,
        ) from error

    relative_files: list[PurePosixPath] = []
    ignored_top_level = 0
    for child in children:
        child_path = ("runtime_hooks_dir", child.name)
        child_mode = _mode(child, child_path, diagnostics)
        if child_mode is None:
            continue
        child_kind = classify_runtime_hook_entry(child_mode, child.suffix)
        if child_kind == RuntimeHookEntryKind.SYMLINK:
            diagnostics.append(
                _diagnostic(child_path, "symlink", "must not contain symlinks")
            )
            continue
        if child_kind == RuntimeHookEntryKind.SPECIAL:
            diagnostics.append(
                _diagnostic(
                    child_path, "special_file", "must not contain special files"
                )
            )
            continue
        if child.name not in RUNTIME_HOOK_PHASE_DIRECTORY_NAMES:
            ignored_top_level += 1
            continue
        if child_kind != RuntimeHookEntryKind.DIRECTORY:
            diagnostics.append(
                Diagnostic(
                    child_path,
                    "runtime_hooks.phase_not_directory",
                    "runtime hook phase entries must be directories",
                )
            )
            continue
        relative_files.extend(
            _phase_files(root, child, child_path, diagnostics, warnings)
        )
    if ignored_top_level:
        warnings.append(
            Diagnostic(
                ("runtime_hooks_dir",),
                "runtime_hooks.ignored_top_level",
                f"ignored {ignored_top_level} ordinary top-level "
                "runtime hook entries outside "
                f"{runtime_hook_phase_directory_list()}",
                severity=DiagnosticSeverity.WARNING,
            )
        )
    if diagnostics:
        raise RuntimeHookInputError(tuple(diagnostics))
    requests = tuple(
        LocalExecutableIdentityRequest(
            root,
            PurePosixPath(validate_hook_relative_path(relative.as_posix())),
            PurePosixPath(hook_lock_identity("runtime", relative.as_posix())),
        )
        for relative in sorted(relative_files, key=PurePosixPath.as_posix)
    )
    return RuntimeHookInputs(root, requests, tuple(warnings))


def _phase_files(
    root: Path,
    phase: Path,
    path: tuple[str, ...],
    diagnostics: list[Diagnostic],
    warnings: list[Diagnostic],
) -> list[PurePosixPath]:
    try:
        children = tuple(sorted(phase.iterdir(), key=lambda item: item.name))
    except OSError as error:
        diagnostics.append(
            Diagnostic(
                path,
                "runtime_hooks.phase_read_failed",
                f"phase directory could not be read: {error}",
            )
        )
        return []
    files: list[PurePosixPath] = []
    ignored = 0
    for child in children:
        child_path = (*path, child.name)
        child_mode = _mode(child, child_path, diagnostics)
        if child_mode is None:
            continue
        child_kind = classify_runtime_hook_entry(child_mode, child.suffix)
        if child_kind == RuntimeHookEntryKind.SYMLINK:
            diagnostics.append(
                _diagnostic(child_path, "symlink", "must not contain symlinks")
            )
        elif child_kind in {
            RuntimeHookEntryKind.DIRECTORY,
            RuntimeHookEntryKind.OTHER_REGULAR_FILE,
        }:
            ignored += 1
        elif child_kind == RuntimeHookEntryKind.SPECIAL:
            diagnostics.append(
                _diagnostic(
                    child_path, "special_file", "must not contain special files"
                )
            )
        elif child_kind == RuntimeHookEntryKind.SELECTABLE_FILE:
            files.append(PurePosixPath(child.relative_to(root).as_posix()))
    if ignored:
        warnings.append(
            Diagnostic(
                path,
                "runtime_hooks.ignored_phase_entries",
                f"ignored {ignored} ordinary non-hook phase entries; only direct "
                "regular .sh and .py files are selected",
                severity=DiagnosticSeverity.WARNING,
            )
        )
    return files


def _mode(
    path: Path,
    diagnostic_path: tuple[str, ...],
    diagnostics: list[Diagnostic],
) -> int | None:
    try:
        return path.lstat().st_mode
    except OSError as error:
        diagnostics.append(
            Diagnostic(
                diagnostic_path,
                "runtime_hooks.entry_inspect_failed",
                f"runtime hook entry could not be inspected: {error}",
            )
        )
        return None


def _diagnostic(path: tuple[str, ...], suffix: str, message: str) -> Diagnostic:
    return Diagnostic(path, f"runtime_hooks.{suffix}", f"runtime hook source {message}")


def _error(
    path: tuple[str, ...],
    code: str,
    message: str,
    error: OSError,
) -> RuntimeHookInputError:
    return RuntimeHookInputError((Diagnostic(path, code, f"{message}: {error}"),))
