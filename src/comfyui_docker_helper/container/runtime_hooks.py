"""Runtime lifecycle hook discovery, validation, and execution."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from comfyui_docker_helper.cli_output import EventSink
from comfyui_docker_helper.config import Diagnostic, DiagnosticSeverity
from comfyui_docker_helper.config.runtime_hooks import (
    RUNTIME_HOOK_PHASE_DIRECTORIES_BY_PHASE,
    RUNTIME_HOOK_PHASES_BY_DIRECTORY,
    RuntimeHookEntryKind,
    classify_runtime_hook_entry,
)
from comfyui_docker_helper.container.process_control import (
    ProcessGroupSignaler,
    ProcessGroupSignalError,
    SessionLeaderProcess,
    reap_process_if_exited,
    request_force_process_group,
    signal_process_group,
    terminate_process_group_until,
)
from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
    start_argv,
)
from comfyui_docker_helper.container.runtime_event_delivery import (
    safe_runtime_event_sink,
)
from comfyui_docker_helper.container.runtime_events import (
    RuntimeEvent,
    RuntimeHookCompleted,
    RuntimeHookStarted,
)

BAKED_RUNTIME_HOOKS_PATH = Path("/opt/cdh/runtime/hooks")
MOUNTED_RUNTIME_HOOKS_PATH = Path("/etc/cdh/runtime/hooks")
STOP_HOOK_TERMINATION_GRACE_SECONDS = 2.0
STOP_HOOK_POLL_INTERVAL_SECONDS = 0.1

type RuntimeHookPhase = Literal["pre-start", "post-start", "stop"]
type RuntimeHookSource = Literal["baked", "mounted"]
type CancelRequested = Callable[[], bool]
type Monotonic = Callable[[], float]
type Sleep = Callable[[float], object]


class RuntimeHookStatus(StrEnum):
    """Runtime hook execution result."""

    COMPLETED = "completed"


@runtime_checkable
class DeadlineBoundCancellation(Protocol):
    """Cancellation source that exposes the owning absolute deadline."""

    def __call__(self) -> bool: ...

    def shutdown_deadline(self) -> float | None: ...


@runtime_checkable
class WakeableCancellation(Protocol):
    """Cancellation source that wakes a bounded process poll promptly."""

    def wait(self, timeout: float) -> object: ...


@runtime_checkable
class ForceEscalationCancellation(Protocol):
    """Cancellation source that distinguishes a repeated-signal force request."""

    def force_requested(self) -> bool: ...


class RuntimeHookError(ValueError):
    """Runtime hook discovery or execution failure with stable diagnostics."""

    def __init__(
        self,
        diagnostics: tuple[Diagnostic, ...],
        *,
        active_process: SessionLeaderProcess | None = None,
    ) -> None:
        if not diagnostics:
            raise ValueError("runtime hook errors require diagnostics")
        self.diagnostics = diagnostics
        self.active_process = active_process
        super().__init__("runtime hook processing failed")


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
    ) -> SessionLeaderProcess: ...


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
    warnings: tuple[Diagnostic, ...] = ()

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
    warnings: list[Diagnostic] = []
    hooks: list[RuntimeHook] = []

    for hook_root in roots:
        if not _root_exists(hook_root.root):
            continue
        root_error = _validate_root(hook_root)
        if root_error is not None:
            diagnostics.append(root_error)
            continue
        try:
            root_entries = tuple(
                sorted(hook_root.root.iterdir(), key=lambda item: item.name)
            )
        except OSError:
            diagnostics.append(
                Diagnostic(
                    path=("hooks", hook_root.source),
                    code="runtime_hook.root_read_failed",
                    message="runtime hook root could not be read",
                )
            )
            continue
        ignored_top_level = 0
        phase_entries: dict[RuntimeHookPhase, Path] = {}
        for entry in root_entries:
            entry_mode = _root_entry_mode(hook_root, entry, diagnostics)
            if entry_mode is None:
                continue
            entry_kind = classify_runtime_hook_entry(entry_mode, entry.suffix)
            if entry_kind == RuntimeHookEntryKind.SYMLINK:
                diagnostics.append(
                    Diagnostic(
                        path=("hooks", hook_root.source, entry.name),
                        code="runtime_hook.symlink",
                        message="runtime hook entries must not be symlinks",
                    )
                )
                continue
            phase = RUNTIME_HOOK_PHASES_BY_DIRECTORY.get(entry.name)
            if phase is None:
                if entry_kind != RuntimeHookEntryKind.SPECIAL:
                    ignored_top_level += 1
                else:
                    diagnostics.append(
                        Diagnostic(
                            path=("hooks", hook_root.source, entry.name),
                            code="runtime_hook.special_file",
                            message="runtime hook roots must not contain special files",
                        )
                    )
                continue
            if entry_kind != RuntimeHookEntryKind.DIRECTORY:
                diagnostics.append(
                    Diagnostic(
                        path=("hooks", hook_root.source, phase),
                        code="runtime_hook.phase_not_directory",
                        message="runtime hook phase path must be a directory",
                    )
                )
                continue
            phase_entries[phase] = entry
        for phase in RUNTIME_HOOK_PHASE_DIRECTORIES_BY_PHASE:
            phase_dir = phase_entries.get(phase)
            if phase_dir is None:
                continue
            hooks.extend(
                _discover_phase_hooks(
                    hook_root,
                    phase,
                    phase_dir,
                    diagnostics,
                    warnings,
                )
            )
        if ignored_top_level:
            warnings.append(
                Diagnostic(
                    path=("hooks", hook_root.source),
                    code="runtime_hook.ignored_top_level",
                    message=(
                        f"ignored {ignored_top_level} ordinary top-level runtime "
                        "hook entries outside the known phase directories"
                    ),
                    severity=DiagnosticSeverity.WARNING,
                )
            )

    if diagnostics:
        raise RuntimeHookError(tuple(diagnostics))
    return RuntimeHookPlan(hooks=tuple(hooks), warnings=tuple(warnings))


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
    event_sink: EventSink[RuntimeEvent] | None = None,
) -> tuple[RuntimeHookResult, ...]:
    """Run startup hooks with shutdown cancellation and process-group cleanup."""
    event_sink = safe_runtime_event_sink(event_sink)
    _validate_hook_process_bounds(
        termination_grace_seconds=termination_grace_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    hook_env = runtime.env(env)
    results: list[RuntimeHookResult] = []

    phase_hooks = plan.for_phase(phase)
    total = len(phase_hooks)
    for index, hook in enumerate(phase_hooks, start=1):
        _raise_if_hook_cancelled(hook, cancel_requested)
        argv = _hook_argv(hook, runtime)
        if event_sink is None:
            log(
                "Running runtime hook "
                f"source={hook.source} phase={hook.phase} filename={hook.filename}"
            )
        else:
            event_sink.emit(
                RuntimeHookStarted(index, total, hook.phase, hook.source, hook.filename)
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
                    signal_process_group
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
                        message="runtime hook could not be started",
                    ),
                )
            ) from error

        if returncode != 0:
            raise RuntimeHookError(
                (
                    Diagnostic(
                        path=_hook_path(hook),
                        code="runtime_hook.execution_failed",
                        message=(f"runtime hook failed with exit code {returncode}"),
                    ),
                )
            )
        if event_sink is not None:
            event_sink.emit(
                RuntimeHookCompleted(
                    index, total, hook.phase, hook.source, hook.filename
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
    deadline: float | None = None,
    termination_grace_seconds: float = STOP_HOOK_TERMINATION_GRACE_SECONDS,
    poll_interval_seconds: float = STOP_HOOK_POLL_INTERVAL_SECONDS,
    monotonic: Monotonic = time.monotonic,
    sleep: Sleep = time.sleep,
    process_group_signaler: ProcessGroupSignaler | None = None,
    event_sink: EventSink[RuntimeEvent] | None = None,
) -> tuple[RuntimeHookResult, ...]:
    """Run stop hooks within the lifecycle owner's absolute deadline."""
    event_sink = safe_runtime_event_sink(event_sink)
    _validate_hook_process_bounds(
        termination_grace_seconds=termination_grace_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    hook_env = runtime.env(env)
    results: list[RuntimeHookResult] = []

    phase_hooks = plan.for_phase("stop")
    total = len(phase_hooks)
    for index, hook in enumerate(phase_hooks, start=1):
        if deadline is not None and monotonic() >= deadline:
            break
        _raise_if_stop_cancelled(hook, cancel_requested)
        argv = _hook_argv(hook, runtime)
        if event_sink is None:
            log(
                "Running runtime hook "
                f"source={hook.source} phase={hook.phase} filename={hook.filename}"
            )
        else:
            event_sink.emit(
                RuntimeHookStarted(index, total, hook.phase, hook.source, hook.filename)
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
                deadline=deadline,
                termination_grace_seconds=termination_grace_seconds,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
                process_group_signaler=(
                    signal_process_group
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
                        message="runtime hook could not be started",
                    ),
                )
            ) from error

        if returncode != 0:
            raise RuntimeHookError(
                (
                    Diagnostic(
                        path=_hook_path(hook),
                        code="runtime_hook.execution_failed",
                        message=(f"runtime hook failed with exit code {returncode}"),
                    ),
                )
            )
        if event_sink is not None:
            event_sink.emit(
                RuntimeHookCompleted(
                    index, total, hook.phase, hook.source, hook.filename
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
    warnings: list[Diagnostic],
) -> tuple[RuntimeHook, ...]:
    hooks: list[RuntimeHook] = []
    try:
        entries = tuple(sorted(phase_dir.iterdir(), key=lambda item: item.name))
    except OSError:
        diagnostics.append(
            Diagnostic(
                path=("hooks", hook_root.source, phase),
                code="runtime_hook.phase_read_failed",
                message="runtime hook phase directory could not be read",
            )
        )
        return ()

    ignored = 0
    for entry in entries:
        entry_kind = _inspect_hook_file(hook_root, phase, entry, diagnostics)
        if entry_kind is None:
            continue
        if entry_kind in {
            RuntimeHookEntryKind.DIRECTORY,
            RuntimeHookEntryKind.OTHER_REGULAR_FILE,
        }:
            ignored += 1
            continue
        if entry_kind in {
            RuntimeHookEntryKind.SYMLINK,
            RuntimeHookEntryKind.SPECIAL,
        }:
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
    if ignored:
        warnings.append(
            Diagnostic(
                path=("hooks", hook_root.source, phase),
                code="runtime_hook.ignored_phase_entries",
                message=(
                    f"ignored {ignored} ordinary non-hook phase entries; only "
                    "direct regular .sh and .py files are selected"
                ),
                severity=DiagnosticSeverity.WARNING,
            )
        )
    return tuple(hooks)


def _root_entry_mode(
    hook_root: RuntimeHookRoot,
    path: Path,
    diagnostics: list[Diagnostic],
) -> int | None:
    try:
        return path.lstat().st_mode
    except OSError:
        diagnostics.append(
            Diagnostic(
                path=("hooks", hook_root.source, path.name),
                code="runtime_hook.inspect_failed",
                message="runtime hook entry could not be inspected",
            )
        )
        return None


def _validate_root(hook_root: RuntimeHookRoot) -> Diagnostic | None:
    try:
        mode = hook_root.root.lstat().st_mode
    except OSError:
        return Diagnostic(
            path=("hooks", hook_root.source),
            code="runtime_hook.root_inspect_failed",
            message="runtime hook root could not be inspected",
        )
    if not stat.S_ISDIR(mode):
        return Diagnostic(
            path=("hooks", hook_root.source),
            code="runtime_hook.root_not_directory",
            message="runtime hook root must be a directory",
        )
    return None


def _inspect_hook_file(
    hook_root: RuntimeHookRoot,
    phase: RuntimeHookPhase,
    path: Path,
    diagnostics: list[Diagnostic],
) -> RuntimeHookEntryKind | None:
    diagnostic_path = ("hooks", hook_root.source, phase, path.name)
    try:
        mode = path.lstat().st_mode
    except OSError:
        diagnostics.append(
            Diagnostic(
                path=diagnostic_path,
                code="runtime_hook.inspect_failed",
                message="runtime hook file could not be inspected",
            )
        )
        return None
    entry_kind = classify_runtime_hook_entry(mode, path.suffix)
    if entry_kind == RuntimeHookEntryKind.SYMLINK:
        diagnostics.append(
            Diagnostic(
                path=diagnostic_path,
                code="runtime_hook.symlink",
                message="runtime hook files must not be symlinks",
            )
        )
    elif entry_kind == RuntimeHookEntryKind.SPECIAL:
        diagnostics.append(
            Diagnostic(
                path=diagnostic_path,
                code="runtime_hook.special_file",
                message="runtime hook phase entries must be regular files",
            )
        )
    return entry_kind


def _validate_hook_process_bounds(
    *,
    termination_grace_seconds: float,
    poll_interval_seconds: float,
) -> None:
    if termination_grace_seconds <= 0:
        raise ValueError("runtime hook termination grace must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("runtime hook poll interval must be positive")


def _cancellation_deadline(cancel_requested: CancelRequested) -> float | None:
    if isinstance(cancel_requested, DeadlineBoundCancellation):
        return cancel_requested.shutdown_deadline()
    return None


def _force_requested(cancel_requested: CancelRequested) -> bool:
    if isinstance(cancel_requested, ForceEscalationCancellation):
        return cancel_requested.force_requested()
    return False


def _wait_for_hook_poll(
    cancel_requested: CancelRequested,
    timeout: float,
    *,
    sleep: Sleep,
) -> None:
    if isinstance(cancel_requested, WakeableCancellation):
        cancel_requested.wait(timeout)
    else:
        sleep(timeout)


def _wait_for_startup_hook_process(
    process: SessionLeaderProcess,
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
        cancellation_won = cancel_requested()
        if cancellation_won:
            _terminate_hook_process_group(
                process,
                hook=hook,
                termination_grace_seconds=termination_grace_seconds,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
                process_group_signaler=process_group_signaler,
                cancel_requested=cancel_requested,
                deadline=_cancellation_deadline(cancel_requested),
            )
            _raise_hook_cancelled(hook)
        returncode = reap_process_if_exited(process)
        if returncode is not None:
            return returncode
        _wait_for_hook_poll(
            cancel_requested,
            poll_interval_seconds,
            sleep=sleep,
        )


def _wait_for_stop_hook_process(
    process: SessionLeaderProcess,
    *,
    hook: RuntimeHook,
    cancel_requested: CancelRequested,
    deadline: float | None,
    termination_grace_seconds: float,
    poll_interval_seconds: float,
    monotonic: Monotonic,
    sleep: Sleep,
    process_group_signaler: ProcessGroupSignaler,
) -> int:
    while True:
        cancellation_won = cancel_requested()
        if cancellation_won:
            _terminate_hook_process_group(
                process,
                hook=hook,
                termination_grace_seconds=termination_grace_seconds,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
                process_group_signaler=process_group_signaler,
                cancel_requested=cancel_requested,
                deadline=deadline,
            )
            _raise_hook_cancelled(hook)
        returncode = reap_process_if_exited(process)
        if returncode is not None:
            return returncode
        now = monotonic()
        deadline_won = deadline is not None and now >= deadline
        if deadline_won:
            reaped = _request_force_hook_process_group(
                process,
                hook=hook,
                process_group_signaler=process_group_signaler,
            )
            raise RuntimeHookError(
                (
                    Diagnostic(
                        path=_hook_path(hook),
                        code="runtime_hook.shutdown_deadline",
                        message="runtime hook exceeded the shutdown deadline",
                    ),
                ),
                active_process=None if reaped else process,
            )
        _wait_for_hook_poll(
            cancel_requested,
            (
                poll_interval_seconds
                if deadline is None
                else min(poll_interval_seconds, deadline - now)
            ),
            sleep=sleep,
        )


def _request_force_hook_process_group(
    process: SessionLeaderProcess,
    *,
    hook: RuntimeHook,
    process_group_signaler: ProcessGroupSignaler,
) -> bool:
    try:
        request_force_process_group(process, signaler=process_group_signaler)
        return reap_process_if_exited(process) is not None
    except ProcessGroupSignalError as error:
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=_hook_path(hook),
                    code="runtime_hook.termination_failed",
                    message=(
                        "runtime hook process group could not be signaled with "
                        f"{error.sig.name}"
                    ),
                ),
            ),
            active_process=process,
        ) from error


def _terminate_hook_process_group(
    process: SessionLeaderProcess,
    *,
    hook: RuntimeHook,
    termination_grace_seconds: float,
    poll_interval_seconds: float,
    monotonic: Monotonic,
    sleep: Sleep,
    process_group_signaler: ProcessGroupSignaler,
    cancel_requested: CancelRequested,
    deadline: float | None = None,
) -> None:
    termination_deadline = monotonic() + termination_grace_seconds
    if deadline is not None:
        termination_deadline = min(termination_deadline, deadline)
    try:
        terminate_process_group_until(
            process,
            deadline=termination_deadline,
            poll_interval=poll_interval_seconds,
            signaler=process_group_signaler,
            force_requested=lambda: _force_requested(cancel_requested),
            monotonic=monotonic,
            sleep=lambda timeout: _wait_for_hook_poll(
                cancel_requested,
                timeout,
                sleep=sleep,
            ),
        )
    except ProcessGroupSignalError as error:
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=_hook_path(hook),
                    code="runtime_hook.termination_failed",
                    message=(
                        "runtime hook process group could not be signaled with "
                        f"{error.sig.name}"
                    ),
                ),
            ),
            active_process=process,
        ) from error


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
    except OSError:
        return True
    return True


def _hook_path(hook: RuntimeHook) -> tuple[str, str, str, str]:
    return ("hooks", hook.source, hook.phase, hook.filename)
