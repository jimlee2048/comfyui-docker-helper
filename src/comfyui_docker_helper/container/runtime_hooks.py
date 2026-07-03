"""Runtime lifecycle hook discovery, validation, and execution."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from comfyui_docker_helper.config import Diagnostic
from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
    run_argv,
)

BAKED_RUNTIME_HOOKS_PATH = Path("/opt/cdh/runtime/hooks")
MOUNTED_RUNTIME_HOOKS_PATH = Path("/etc/cdh/runtime/hooks")

_KNOWN_PHASE_DIRS = {
    "pre-start": "pre-start.d",
    "post-start": "post-start.d",
    "stop": "stop.d",
}
_SUPPORTED_SUFFIXES = frozenset({".sh", ".py"})

type RuntimeHookPhase = Literal["pre-start", "post-start", "stop"]
type RuntimeHookSource = Literal["baked", "mounted"]


class RuntimeHookStatus(StrEnum):
    """Runtime hook execution result."""

    COMPLETED = "completed"


class RuntimeHookError(ValueError):
    """Runtime hook discovery or execution failure with stable diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("runtime hook errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("runtime hook processing failed")


class RuntimeHookCommandRunner(Protocol):
    """Subprocess-compatible hook command runner."""

    def __call__(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class RuntimeHookRoot:
    """One fixed runtime hook source root."""

    source: RuntimeHookSource
    root: Path


@dataclass(frozen=True, slots=True)
class RuntimeHook:
    """One discovered runtime lifecycle hook script."""

    source: RuntimeHookSource
    phase: RuntimeHookPhase
    root: Path
    path: Path
    filename: str


@dataclass(frozen=True, slots=True)
class RuntimeHookPlan:
    """Ordered hooks grouped by lifecycle phase."""

    hooks: tuple[RuntimeHook, ...]

    def for_phase(self, phase: RuntimeHookPhase) -> tuple[RuntimeHook, ...]:
        """Return hooks for one lifecycle phase in execution order."""
        return tuple(hook for hook in self.hooks if hook.phase == phase)


@dataclass(frozen=True, slots=True)
class RuntimeHookResult:
    """One runtime hook execution result."""

    hook: RuntimeHook
    argv: tuple[str, ...]
    status: RuntimeHookStatus


def discover_runtime_hooks(
    *,
    baked_hooks_path: str | Path = BAKED_RUNTIME_HOOKS_PATH,
    mounted_hooks_path: str | Path = MOUNTED_RUNTIME_HOOKS_PATH,
) -> RuntimeHookPlan:
    """Validate fixed hook roots and return discovered hooks in runtime order."""
    roots = (
        RuntimeHookRoot("baked", Path(baked_hooks_path)),
        RuntimeHookRoot("mounted", Path(mounted_hooks_path)),
    )
    diagnostics: list[Diagnostic] = []
    hooks: list[RuntimeHook] = []

    for hook_root in roots:
        if not _root_exists(hook_root.root):
            continue
        root_error = _validate_root(hook_root)
        if root_error is not None:
            diagnostics.append(root_error)
            continue
        for phase, dirname in _KNOWN_PHASE_DIRS.items():
            phase_dir = hook_root.root / dirname
            if not _root_exists(phase_dir):
                continue
            phase_error = _validate_phase_dir(hook_root, phase, phase_dir)
            if phase_error is not None:
                diagnostics.append(phase_error)
                continue
            hooks.extend(
                _discover_phase_hooks(
                    hook_root,
                    phase,
                    phase_dir,
                    diagnostics,
                )
            )

    if diagnostics:
        raise RuntimeHookError(tuple(diagnostics))
    return RuntimeHookPlan(hooks=tuple(hooks))


def run_runtime_hooks(
    plan: RuntimeHookPlan,
    phase: RuntimeHookPhase,
    *,
    runtime: ContainerRuntime,
    env: Mapping[str, str] | None = None,
    log: Callable[[str], object] = print,
    runner: RuntimeHookCommandRunner = run_argv,
) -> tuple[RuntimeHookResult, ...]:
    """Run hooks for one phase in discovered order and stop on first failure."""
    hook_env = runtime.env(env)
    results: list[RuntimeHookResult] = []
    for hook in plan.for_phase(phase):
        argv = _hook_argv(hook, runtime)
        log(
            "Running runtime hook "
            f"source={hook.source} phase={hook.phase} filename={hook.filename}"
        )
        try:
            runner(
                argv,
                cwd=runtime.comfyui_path,
                env=hook_env,
                description=(
                    f"runtime hook {hook.source}/{hook.phase}/{hook.filename}"
                ),
            )
        except ContainerCommandError as error:
            raise RuntimeHookError(
                (
                    Diagnostic(
                        path=_hook_path(hook),
                        code="runtime_hook.execution_failed",
                        message=str(error),
                    ),
                )
            ) from error
        results.append(
            RuntimeHookResult(
                hook=hook,
                argv=tuple(os.fspath(argument) for argument in argv),
                status=RuntimeHookStatus.COMPLETED,
            )
        )
    return tuple(results)


def _discover_phase_hooks(
    hook_root: RuntimeHookRoot,
    phase: RuntimeHookPhase,
    phase_dir: Path,
    diagnostics: list[Diagnostic],
) -> tuple[RuntimeHook, ...]:
    hooks: list[RuntimeHook] = []
    try:
        entries = tuple(sorted(phase_dir.iterdir(), key=lambda item: item.name))
    except OSError as error:
        diagnostics.append(
            Diagnostic(
                path=("hooks", hook_root.source, phase),
                code="runtime_hook.phase_read_failed",
                message=f"runtime hook phase directory could not be read: {error}",
            )
        )
        return ()

    for entry in entries:
        entry_error = _validate_hook_file(hook_root, phase, entry)
        if entry_error is not None:
            diagnostics.append(entry_error)
            continue
        hooks.append(
            RuntimeHook(
                source=hook_root.source,
                phase=phase,
                root=hook_root.root,
                path=entry,
                filename=entry.name,
            )
        )
    return tuple(hooks)


def _validate_root(hook_root: RuntimeHookRoot) -> Diagnostic | None:
    try:
        mode = hook_root.root.lstat().st_mode
    except OSError as error:
        return Diagnostic(
            path=("hooks", hook_root.source),
            code="runtime_hook.root_inspect_failed",
            message=f"runtime hook root could not be inspected: {error}",
        )
    if not stat.S_ISDIR(mode):
        return Diagnostic(
            path=("hooks", hook_root.source),
            code="runtime_hook.root_not_directory",
            message="runtime hook root must be a directory",
        )
    return None


def _validate_phase_dir(
    hook_root: RuntimeHookRoot,
    phase: RuntimeHookPhase,
    phase_dir: Path,
) -> Diagnostic | None:
    try:
        mode = phase_dir.lstat().st_mode
    except OSError as error:
        return Diagnostic(
            path=("hooks", hook_root.source, phase),
            code="runtime_hook.phase_inspect_failed",
            message=f"runtime hook phase directory could not be inspected: {error}",
        )
    if not stat.S_ISDIR(mode):
        return Diagnostic(
            path=("hooks", hook_root.source, phase),
            code="runtime_hook.phase_not_directory",
            message="runtime hook phase path must be a directory",
        )
    return None


def _validate_hook_file(
    hook_root: RuntimeHookRoot,
    phase: RuntimeHookPhase,
    path: Path,
) -> Diagnostic | None:
    diagnostic_path = ("hooks", hook_root.source, phase, path.name)
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        return Diagnostic(
            path=diagnostic_path,
            code="runtime_hook.inspect_failed",
            message=f"runtime hook file could not be inspected: {error}",
        )
    if stat.S_ISLNK(mode):
        return Diagnostic(
            path=diagnostic_path,
            code="runtime_hook.symlink",
            message="runtime hook files must not be symlinks",
        )
    if stat.S_ISDIR(mode):
        return Diagnostic(
            path=diagnostic_path,
            code="runtime_hook.directory",
            message="runtime hook phase entries must be regular files",
        )
    if not stat.S_ISREG(mode):
        return Diagnostic(
            path=diagnostic_path,
            code="runtime_hook.special_file",
            message="runtime hook phase entries must be regular files",
        )
    if path.suffix not in _SUPPORTED_SUFFIXES:
        return Diagnostic(
            path=diagnostic_path,
            code="runtime_hook.unsupported_extension",
            message="runtime hook files must end in .sh or .py",
        )
    return None


def _hook_argv(
    hook: RuntimeHook,
    runtime: ContainerRuntime,
) -> tuple[str | os.PathLike[str], ...]:
    if hook.path.suffix == ".sh":
        return ("bash", hook.path)
    if hook.path.suffix == ".py":
        return (runtime.python, hook.path)
    raise RuntimeHookError(
        (
            Diagnostic(
                path=_hook_path(hook),
                code="runtime_hook.unsupported_extension",
                message="runtime hook files must end in .sh or .py",
            ),
        )
    )


def _root_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _hook_path(hook: RuntimeHook) -> tuple[str, str, str, str]:
    return ("hooks", hook.source, hook.phase, hook.filename)
