"""Runtime lifecycle hook discovery, validation, and execution."""

from __future__ import annotations

import os
import signal
import stat
import time
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
    start_argv,
)

BAKED_RUNTIME_HOOKS_PATH = Path("/opt/cdh/runtime/hooks")
MOUNTED_RUNTIME_HOOKS_PATH = Path("/etc/cdh/runtime/hooks")
STOP_HOOK_TIMEOUT_SECONDS = 30.0
STOP_HOOK_TERMINATION_GRACE_SECONDS = 2.0
STOP_HOOK_POLL_INTERVAL_SECONDS = 0.1

_KNOWN_PHASE_DIRS = {
    "pre-start": "pre-start.d",
    "post-start": "post-start.d",
    "stop": "stop.d",
}
_SUPPORTED_SUFFIXES = frozenset({".sh", ".py"})

type RuntimeHookPhase = Literal["pre-start", "post-start", "stop"]
type RuntimeHookSource = Literal["baked", "mounted"]
type CancelRequested = Callable[[], bool]
type Monotonic = Callable[[], float]
type ProcessGroupSignaler = Callable[[int, signal.Signals], object]
type Sleep = Callable[[float], object]


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
        start_new_session: bool = False,
    ) -> object: ...


class RuntimeHookProcess(Protocol):
    """Running hook process controlled during graceful shutdown."""

    pid: int

    def poll(self) -> int | None: ...

    def wait(self) -> int: ...


class RuntimeHookProcessRunner(Protocol):
    """Subprocess-compatible hook process starter."""

    def __call__(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> RuntimeHookProcess: ...


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
    start_new_session: bool = False,
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
            runner_kwargs = {
                "cwd": runtime.comfyui_path,
                "env": hook_env,
                "description": (
                    f"runtime hook {hook.source}/{hook.phase}/{hook.filename}"
                ),
            }
            if start_new_session:
                runner_kwargs["start_new_session"] = True
            runner(argv, **runner_kwargs)
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


def run_runtime_startup_hooks(
    plan: RuntimeHookPlan,
    phase: RuntimeHookPhase,
    *,
    runtime: ContainerRuntime,
    env: Mapping[str, str] | None = None,
    log: Callable[[str], object] = print,
    runner: RuntimeHookProcessRunner = start_argv,
    cancel_requested: CancelRequested = lambda: False,
    termination_grace_seconds: float = STOP_HOOK_TERMINATION_GRACE_SECONDS,
    poll_interval_seconds: float = STOP_HOOK_POLL_INTERVAL_SECONDS,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
    process_group_signaler: ProcessGroupSignaler | None = None,
) -> tuple[RuntimeHookResult, ...]:
    """Run startup hooks with shutdown cancellation and process-group cleanup."""
    _validate_hook_process_bounds(
        termination_grace_seconds=termination_grace_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    hook_env = runtime.env(env)
    results: list[RuntimeHookResult] = []

    for hook in plan.for_phase(phase):
        _raise_if_hook_cancelled(hook, cancel_requested)
        argv = _hook_argv(hook, runtime)
        log(
            "Running runtime hook "
            f"source={hook.source} phase={hook.phase} filename={hook.filename}"
        )
        try:
            process = runner(
                argv,
                cwd=runtime.comfyui_path,
                env=hook_env,
                description=f"runtime hook {hook.source}/{hook.phase}/{hook.filename}",
                start_new_session=True,
            )
            returncode = _wait_for_startup_hook_process(
                process,
                hook=hook,
                cancel_requested=cancel_requested,
                termination_grace_seconds=termination_grace_seconds,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
                process_group_signaler=(
                    _signal_process_group
                    if process_group_signaler is None
                    else process_group_signaler
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

        if returncode != 0:
            raise RuntimeHookError(
                (
                    Diagnostic(
                        path=_hook_path(hook),
                        code="runtime_hook.execution_failed",
                        message=(
                            "runtime hook failed with exit code "
                            f"{returncode}: {_format_hook_argv(argv)}"
                        ),
                    ),
                )
            )
        results.append(
            RuntimeHookResult(
                hook=hook,
                argv=tuple(os.fspath(argument) for argument in argv),
                status=RuntimeHookStatus.COMPLETED,
            )
        )

    return tuple(results)


def run_runtime_stop_hooks(
    plan: RuntimeHookPlan,
    *,
    runtime: ContainerRuntime,
    env: Mapping[str, str] | None = None,
    log: Callable[[str], object] = print,
    runner: RuntimeHookProcessRunner = start_argv,
    cancel_requested: CancelRequested = lambda: False,
    timeout_seconds: float = STOP_HOOK_TIMEOUT_SECONDS,
    termination_grace_seconds: float = STOP_HOOK_TERMINATION_GRACE_SECONDS,
    poll_interval_seconds: float = STOP_HOOK_POLL_INTERVAL_SECONDS,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
    process_group_signaler: ProcessGroupSignaler | None = None,
) -> tuple[RuntimeHookResult, ...]:
    """Run stop hooks with bounded cancellation and process-group cleanup."""
    _validate_stop_hook_bounds(
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    hook_env = runtime.env(env)
    results: list[RuntimeHookResult] = []

    for hook in plan.for_phase("stop"):
        _raise_if_stop_cancelled(hook, cancel_requested)
        argv = _hook_argv(hook, runtime)
        log(
            "Running runtime hook "
            f"source={hook.source} phase={hook.phase} filename={hook.filename}"
        )
        try:
            process = runner(
                argv,
                cwd=runtime.comfyui_path,
                env=hook_env,
                description=f"runtime hook {hook.source}/{hook.phase}/{hook.filename}",
                start_new_session=True,
            )
            returncode = _wait_for_stop_hook_process(
                process,
                hook=hook,
                cancel_requested=cancel_requested,
                timeout_seconds=timeout_seconds,
                termination_grace_seconds=termination_grace_seconds,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
                process_group_signaler=(
                    _signal_process_group
                    if process_group_signaler is None
                    else process_group_signaler
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

        if returncode != 0:
            raise RuntimeHookError(
                (
                    Diagnostic(
                        path=_hook_path(hook),
                        code="runtime_hook.execution_failed",
                        message=(
                            "runtime hook failed with exit code "
                            f"{returncode}: {_format_hook_argv(argv)}"
                        ),
                    ),
                )
            )
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


def _validate_stop_hook_bounds(
    *,
    timeout_seconds: float,
    termination_grace_seconds: float,
    poll_interval_seconds: float,
) -> None:
    if timeout_seconds <= 0:
        raise ValueError("stop hook timeout must be positive")
    _validate_hook_process_bounds(
        termination_grace_seconds=termination_grace_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def _validate_hook_process_bounds(
    *,
    termination_grace_seconds: float,
    poll_interval_seconds: float,
) -> None:
    if termination_grace_seconds <= 0:
        raise ValueError("runtime hook termination grace must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("runtime hook poll interval must be positive")


def _wait_for_startup_hook_process(
    process: RuntimeHookProcess,
    *,
    hook: RuntimeHook,
    cancel_requested: CancelRequested,
    termination_grace_seconds: float,
    poll_interval_seconds: float,
    monotonic: Monotonic,
    sleep: Sleep,
    process_group_signaler: ProcessGroupSignaler,
) -> int:
    while True:
        returncode = process.poll()
        if returncode is not None:
            return process.wait()
        if cancel_requested():
            _terminate_hook_process_group(
                process,
                hook=hook,
                termination_grace_seconds=termination_grace_seconds,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
                process_group_signaler=process_group_signaler,
            )
            _raise_hook_cancelled(hook)
        sleep(poll_interval_seconds)


def _wait_for_stop_hook_process(
    process: RuntimeHookProcess,
    *,
    hook: RuntimeHook,
    cancel_requested: CancelRequested,
    timeout_seconds: float,
    termination_grace_seconds: float,
    poll_interval_seconds: float,
    monotonic: Monotonic,
    sleep: Sleep,
    process_group_signaler: ProcessGroupSignaler,
) -> int:
    deadline = monotonic() + timeout_seconds
    while True:
        returncode = process.poll()
        if returncode is not None:
            return process.wait()
        if cancel_requested():
            _terminate_hook_process_group(
                process,
                hook=hook,
                termination_grace_seconds=termination_grace_seconds,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
                process_group_signaler=process_group_signaler,
            )
            _raise_hook_cancelled(hook)
        now = monotonic()
        if now >= deadline:
            _terminate_hook_process_group(
                process,
                hook=hook,
                termination_grace_seconds=termination_grace_seconds,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
                process_group_signaler=process_group_signaler,
            )
            raise RuntimeHookError(
                (
                    Diagnostic(
                        path=_hook_path(hook),
                        code="runtime_hook.timeout",
                        message=(
                            f"runtime hook timed out after {timeout_seconds:g} seconds"
                        ),
                    ),
                )
            )
        sleep(min(poll_interval_seconds, deadline - now))


def _terminate_hook_process_group(
    process: RuntimeHookProcess,
    *,
    hook: RuntimeHook,
    termination_grace_seconds: float,
    poll_interval_seconds: float,
    monotonic: Monotonic,
    sleep: Sleep,
    process_group_signaler: ProcessGroupSignaler,
) -> None:
    if process.poll() is not None:
        return

    if not _send_hook_process_group_signal(
        process,
        hook=hook,
        sig=signal.SIGTERM,
        process_group_signaler=process_group_signaler,
    ):
        return
    deadline = monotonic() + termination_grace_seconds
    while True:
        if process.poll() is not None:
            return
        now = monotonic()
        if now >= deadline:
            _send_hook_process_group_signal(
                process,
                hook=hook,
                sig=signal.SIGKILL,
                process_group_signaler=process_group_signaler,
            )
            return
        sleep(min(poll_interval_seconds, deadline - now))


def _send_hook_process_group_signal(
    process: RuntimeHookProcess,
    *,
    hook: RuntimeHook,
    sig: signal.Signals,
    process_group_signaler: ProcessGroupSignaler,
) -> bool:
    try:
        process_group_signaler(process.pid, sig)
    except ProcessLookupError:
        return False
    except OSError as error:
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=_hook_path(hook),
                    code="runtime_hook.termination_failed",
                    message=(
                        "runtime hook process group could not be signaled with "
                        f"{sig.name}: {error}"
                    ),
                ),
            )
        ) from error
    return True


def _raise_if_stop_cancelled(
    hook: RuntimeHook,
    cancel_requested: CancelRequested,
) -> None:
    _raise_if_hook_cancelled(hook, cancel_requested)


def _raise_if_hook_cancelled(
    hook: RuntimeHook,
    cancel_requested: CancelRequested,
) -> None:
    if cancel_requested():
        _raise_hook_cancelled(hook)


def _raise_hook_cancelled(hook: RuntimeHook) -> None:
    raise RuntimeHookError(
        (
            Diagnostic(
                path=_hook_path(hook),
                code="runtime_hook.cancelled",
                message="runtime hook was cancelled by a shutdown signal",
            ),
        )
    )


def _signal_process_group(pid: int, sig: signal.Signals) -> None:
    os.killpg(os.getpgid(pid), sig)


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


def _format_hook_argv(argv: Sequence[str | os.PathLike[str]]) -> str:
    return " ".join(os.fspath(argument) for argument in argv)
