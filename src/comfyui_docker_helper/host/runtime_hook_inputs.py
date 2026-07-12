"""Validated complete baked runtime-hook tree acquisition inputs."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticError
from comfyui_docker_helper.config.runtime_hooks import (
    RUNTIME_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_PHASE_DIRECTORY_NAMES,
    RUNTIME_HOOK_SUPPORTED_SUFFIXES,
    runtime_hook_phase_directory_list,
)
from comfyui_docker_helper.host.identity_providers import (
    LocalExecutableIdentityRequest,
)

_DEFAULT_RUNTIME_HOOKS_DIR = Path("./hooks")
_RUNTIME_HOOK_IDENTITY_ROOT = PurePosixPath(RUNTIME_HOOK_LOCK_PREFIX)


@dataclass(frozen=True, slots=True)
class RuntimeHookInputs:
    source_root: Path | None
    requests: tuple[LocalExecutableIdentityRequest, ...]


class RuntimeHookInputError(DiagnosticError):
    """The baked runtime-hook tree is not one safe executable input tree."""


def discover_runtime_hook_inputs(
    hooks_dir: str | Path | None,
    *,
    working_directory: str | Path | None,
) -> RuntimeHookInputs:
    """Validate and enumerate every baked runtime hook in canonical order."""
    base = Path.cwd() if working_directory is None else Path(working_directory)
    explicit = hooks_dir is not None
    selected = Path(hooks_dir) if explicit else _DEFAULT_RUNTIME_HOOKS_DIR
    candidate = selected if selected.is_absolute() else base / selected
    if not explicit:
        try:
            candidate.lstat()
        except FileNotFoundError:
            return RuntimeHookInputs(None, ())
        except OSError as error:
            raise _error(
                ("hooks_dir",),
                "runtime_hooks.source_inspect_failed",
                "runtime hook source could not be inspected",
                error,
            ) from error

    diagnostics: list[Diagnostic] = []
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError as error:
        raise RuntimeHookInputError(
            (
                Diagnostic(
                    ("hooks_dir",),
                    "runtime_hooks.source_not_directory",
                    "runtime hook source must be an existing real directory",
                ),
            )
        ) from error
    except OSError as error:
        raise _error(
            ("hooks_dir",),
            "runtime_hooks.source_inspect_failed",
            "runtime hook source could not be inspected",
            error,
        ) from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeHookInputError(
            (
                Diagnostic(
                    ("hooks_dir",),
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
            ("hooks_dir",),
            "runtime_hooks.source_read_failed",
            "runtime hook source could not be read",
            error,
        ) from error

    relative_files: list[PurePosixPath] = []
    for child in children:
        child_path = ("hooks_dir", child.name)
        child_mode = _mode(child, child_path, diagnostics)
        if child_mode is None:
            continue
        if stat.S_ISLNK(child_mode):
            diagnostics.append(
                _diagnostic(child_path, "symlink", "must not contain symlinks")
            )
            continue
        if not stat.S_ISDIR(child_mode) and not stat.S_ISREG(child_mode):
            diagnostics.append(
                _diagnostic(
                    child_path, "special_file", "must not contain special files"
                )
            )
            continue
        if child.name not in RUNTIME_HOOK_PHASE_DIRECTORY_NAMES:
            diagnostics.append(
                Diagnostic(
                    child_path,
                    "runtime_hooks.unknown_top_level",
                    "runtime hook source may only contain "
                    f"{runtime_hook_phase_directory_list()} directories",
                )
            )
            continue
        if not stat.S_ISDIR(child_mode):
            diagnostics.append(
                Diagnostic(
                    child_path,
                    "runtime_hooks.phase_not_directory",
                    "runtime hook phase entries must be directories",
                )
            )
            continue
        relative_files.extend(_phase_files(root, child, child_path, diagnostics))
    if diagnostics:
        raise RuntimeHookInputError(tuple(diagnostics))
    requests = tuple(
        LocalExecutableIdentityRequest(
            root,
            relative,
            _RUNTIME_HOOK_IDENTITY_ROOT / relative,
        )
        for relative in sorted(relative_files, key=PurePosixPath.as_posix)
    )
    return RuntimeHookInputs(root, requests)


def _phase_files(
    root: Path,
    phase: Path,
    path: tuple[str, ...],
    diagnostics: list[Diagnostic],
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
    for child in children:
        child_path = (*path, child.name)
        child_mode = _mode(child, child_path, diagnostics)
        if child_mode is None:
            continue
        if stat.S_ISLNK(child_mode):
            diagnostics.append(
                _diagnostic(child_path, "symlink", "must not contain symlinks")
            )
        elif stat.S_ISDIR(child_mode):
            diagnostics.append(
                _diagnostic(
                    child_path, "entry_not_file", "phase entries must be regular files"
                )
            )
        elif not stat.S_ISREG(child_mode):
            diagnostics.append(
                _diagnostic(
                    child_path, "special_file", "must not contain special files"
                )
            )
        elif child.suffix not in RUNTIME_HOOK_SUPPORTED_SUFFIXES:
            diagnostics.append(
                Diagnostic(
                    child_path,
                    "runtime_hooks.unsupported_extension",
                    "runtime hook files must end in .sh or .py",
                )
            )
        else:
            files.append(PurePosixPath(child.relative_to(root).as_posix()))
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
