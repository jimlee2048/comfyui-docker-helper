"""Tests for runtime lifecycle hook discovery and pre-start execution."""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from comfyui_docker_helper.container.process_control import ProcessGroupSignalError
from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
)
from comfyui_docker_helper.container.runtime_events import (
    RuntimeHookCompleted,
    RuntimeHookStarted,
)
from comfyui_docker_helper.container.runtime_hooks import (
    STOP_HOOK_POLL_INTERVAL_SECONDS,
    STOP_HOOK_TERMINATION_GRACE_SECONDS,
    RuntimeHookError,
    discover_runtime_hooks,
    run_runtime_startup_hooks,
    run_runtime_stop_hooks,
)
from tests.runtime_event_support import RecordingRuntimeEventSink


def _runtime(tmp_path: Path) -> ContainerRuntime:
    return ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )


def _write_hook(root: Path, phase_dir: str, filename: str, content: str = "") -> Path:
    phase = root / phase_dir
    phase.mkdir(parents=True, exist_ok=True)
    path = phase / filename
    path.write_text(content or f"# {filename}\n", encoding="utf-8")
    return path


class FakeClock:
    """Manual monotonic clock for fast stop-hook timeout tests."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeHookProcess:
    """Minimal process surface used by bounded stop-hook tests."""

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.waits = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        self.waits += 1
        if self.returncode is None:
            raise AssertionError("running fake process cannot be waited")
        return self.returncode


# Discovery protects the selected/ignored boundary, bounded warning aggregation,
# hard filesystem failures, and baked-before-mounted execution order.
def test_discovery_order_is_baked_then_mounted_lexical_and_allows_duplicates(
    tmp_path: Path,
) -> None:
    baked = tmp_path / "baked"
    mounted = tmp_path / "mounted"
    _write_hook(baked, "pre-start.d", "20-second.sh")
    _write_hook(baked, "pre-start.d", "10-first.py")
    _write_hook(mounted, "pre-start.d", "10-first.py")
    _write_hook(mounted, "pre-start.d", "30-third.sh")

    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=mounted,
    )

    assert [
        (hook.source, hook.phase, hook.filename) for hook in plan.for_phase("pre-start")
    ] == [
        ("baked", "pre-start", "10-first.py"),
        ("baked", "pre-start", "20-second.sh"),
        ("mounted", "pre-start", "10-first.py"),
        ("mounted", "pre-start", "30-third.sh"),
    ]


def test_missing_roots_have_no_hooks(tmp_path: Path) -> None:
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=tmp_path / "missing-mounted",
    )

    assert plan.hooks == ()
    assert plan.for_phase("pre-start") == ()


def test_unknown_root_entries_are_ignored_with_an_aggregated_warning(
    tmp_path: Path,
) -> None:
    baked = tmp_path / "baked"
    baked.mkdir()
    (baked / "README.md").write_text("ignored\n", encoding="utf-8")
    (baked / "unknown-start.d").mkdir()
    (baked / "unknown-start.d" / "bad.txt").write_text("ignored\n", encoding="utf-8")
    _write_hook(baked, "pre-start.d", "10-pre.sh")

    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=tmp_path / "missing-mounted",
    )

    assert [(hook.source, hook.phase, hook.filename) for hook in plan.hooks] == [
        ("baked", "pre-start", "10-pre.sh")
    ]
    assert len(plan.warnings) == 1
    warnings = {(warning.path, warning.code): warning for warning in plan.warnings}
    warning = warnings[("hooks", "baked"), "runtime_hook.ignored_top_level"]
    assert "ignored 2 ordinary top-level" in warning.message


def test_ordinary_unselected_phase_entries_warn_without_recursion(
    tmp_path: Path,
) -> None:
    baked = tmp_path / "baked"
    mounted = tmp_path / "mounted"
    _write_hook(baked, "pre-start.d", "10-pre.sh")
    _write_hook(mounted, "pre-start.d", "10-pre.sh")
    _write_hook(mounted, "post-start.d", "notes.txt")
    nested = mounted / "stop.d" / "nested"
    nested.mkdir(parents=True)
    (nested / "20-not-discovered.sh").write_text("ignored\n")

    plan = discover_runtime_hooks(baked_hooks_path=baked, mounted_hooks_path=mounted)

    assert [(hook.source, hook.phase, hook.filename) for hook in plan.hooks] == [
        ("baked", "pre-start", "10-pre.sh"),
        ("mounted", "pre-start", "10-pre.sh"),
    ]
    assert len(plan.warnings) == 2
    warnings = {(warning.path, warning.code): warning for warning in plan.warnings}
    assert set(warnings) == {
        (
            ("hooks", "mounted", "post-start"),
            "runtime_hook.ignored_phase_entries",
        ),
        (
            ("hooks", "mounted", "stop"),
            "runtime_hook.ignored_phase_entries",
        ),
    }


def test_unknown_top_level_symlink_remains_invalid(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "README-link").symlink_to(tmp_path / "outside")

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(
            baked_hooks_path=tmp_path / "missing-baked",
            mounted_hooks_path=mounted,
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "README-link"), "runtime_hook.symlink")
    ]


def test_unknown_top_level_special_file_remains_invalid(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("fifo special files are not supported on this platform")
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    os.mkfifo(mounted / "ordinary-looking-entry")

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(
            baked_hooks_path=tmp_path / "missing-baked",
            mounted_hooks_path=mounted,
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "ordinary-looking-entry"), "runtime_hook.special_file")
    ]


def test_strict_validation_rejects_symlinks(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    mounted = tmp_path / "mounted"
    phase = mounted / "pre-start.d"
    phase.mkdir(parents=True)
    real = tmp_path / "real.sh"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    (phase / "10-link.sh").symlink_to(real)

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(
            baked_hooks_path=tmp_path / "missing-baked",
            mounted_hooks_path=mounted,
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "pre-start", "10-link.sh"), "runtime_hook.symlink")
    ]


def test_discovery_wraps_root_inspection_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    original_lstat = Path.lstat

    def fail_lstat(self: Path) -> os.stat_result:
        if self == mounted:
            raise PermissionError("inspect denied")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", fail_lstat)

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(
            baked_hooks_path=tmp_path / "missing-baked",
            mounted_hooks_path=mounted,
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted"), "runtime_hook.root_inspect_failed")
    ]
    assert error.value.diagnostics[0].message == (
        "runtime hook root could not be inspected"
    )
    assert "inspect denied" not in error.value.diagnostics[0].message


def test_discovery_wraps_root_read_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    original_iterdir = Path.iterdir

    def fail_iterdir(self: Path) -> Iterator[Path]:
        if self == mounted:
            raise PermissionError("read denied")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(
            baked_hooks_path=tmp_path / "missing-baked",
            mounted_hooks_path=mounted,
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted"), "runtime_hook.root_read_failed")
    ]
    assert error.value.diagnostics[0].message == ("runtime hook root could not be read")
    assert "read denied" not in error.value.diagnostics[0].message


def test_strict_validation_rejects_special_files(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("fifo special files are not supported on this platform")
    mounted = tmp_path / "mounted"
    phase = mounted / "stop.d"
    phase.mkdir(parents=True)
    os.mkfifo(phase / "pipe.sh")

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(
            baked_hooks_path=tmp_path / "missing-baked",
            mounted_hooks_path=mounted,
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "stop", "pipe.sh"), "runtime_hook.special_file")
    ]


# Startup hook process tests pin interpreter selection, environment shaping,
# ordering, typed events, and failure reporting before ComfyUI starts.
# A successful terminal result releases each hook's original group without a
# cleanup signal, while cancellation/deadline paths retain exact group authority.
def test_stop_hooks_request_process_group(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    baked = tmp_path / "baked"
    mounted = tmp_path / "mounted"
    _write_hook(baked, "stop.d", "10-baked.sh")
    _write_hook(mounted, "stop.d", "10-mounted.py")
    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=mounted,
    )
    calls: list[tuple[str, bool]] = []
    group_signals: list[tuple[int, signal.Signals]] = []

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> FakeHookProcess:
        del cwd, env, description
        calls.append((Path(argv[-1]).name, start_new_session))
        return FakeHookProcess(pid=100 + len(calls), returncode=0)

    run_runtime_stop_hooks(
        plan,
        runtime=runtime,
        runner=runner,
        process_group_signaler=lambda pid, sig: group_signals.append((pid, sig)),
        event_sink=RecordingRuntimeEventSink(),
    )

    assert calls == [("10-baked.sh", True), ("10-mounted.py", True)]
    assert group_signals == []


def test_runtime_hooks_emit_safe_indexed_facts_without_argv(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "mounted"
    _write_hook(mounted, "pre-start.d", "05-prepare.sh")
    _write_hook(mounted, "stop.d", "10-first.sh")
    _write_hook(mounted, "stop.d", "20-second.py")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )
    recorder = RecordingRuntimeEventSink()

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        **_kwargs: object,
    ) -> FakeHookProcess:
        del argv
        return FakeHookProcess(pid=100, returncode=0)

    run_runtime_startup_hooks(
        plan,
        "pre-start",
        runtime=runtime,
        runner=runner,  # type: ignore[arg-type]
        event_sink=recorder,
    )
    run_runtime_stop_hooks(
        plan,
        runtime=runtime,
        runner=runner,  # type: ignore[arg-type]
        event_sink=recorder,
    )

    assert recorder.events == [
        RuntimeHookStarted(1, 1, "pre-start", "mounted", "05-prepare.sh"),
        RuntimeHookCompleted(1, 1, "pre-start", "mounted", "05-prepare.sh"),
        RuntimeHookStarted(1, 2, "stop", "mounted", "10-first.sh"),
        RuntimeHookCompleted(1, 2, "stop", "mounted", "10-first.sh"),
        RuntimeHookStarted(2, 2, "stop", "mounted", "20-second.py"),
        RuntimeHookCompleted(2, 2, "stop", "mounted", "20-second.py"),
    ]
    assert os.fspath(tmp_path) not in repr(recorder.events)


def test_runtime_hook_failure_diagnostics_omit_argv_and_runner_reason(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "raw-argv-sentinel" / "mounted"
    _write_hook(mounted, "pre-start.d", "10-fail.py")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )

    with pytest.raises(RuntimeHookError) as error:
        run_runtime_startup_hooks(
            plan,
            "pre-start",
            runtime=runtime,
            runner=lambda *_args, **_kwargs: FakeHookProcess(
                pid=101,
                returncode=7,
            ),
            event_sink=RecordingRuntimeEventSink(),
        )

    diagnostic = error.value.diagnostics[0]
    assert diagnostic.path == ("hooks", "mounted", "pre-start", "10-fail.py")
    assert diagnostic.message == "runtime hook failed with exit code 7"
    assert "raw-argv-sentinel" not in diagnostic.message

    def fail_start(*_args: object, **_kwargs: object) -> FakeHookProcess:
        raise ContainerCommandError("raw-runner-credential-sentinel")

    with pytest.raises(RuntimeHookError) as start_error:
        run_runtime_startup_hooks(
            plan,
            "pre-start",
            runtime=runtime,
            runner=fail_start,  # type: ignore[arg-type]
            event_sink=RecordingRuntimeEventSink(),
        )

    start_diagnostic = start_error.value.diagnostics[0]
    assert start_diagnostic.message == "runtime hook could not be started"
    assert isinstance(start_error.value.__cause__, ContainerCommandError)
    assert "raw-runner-credential-sentinel" not in start_diagnostic.message


@pytest.mark.parametrize("phase", ["pre-start", "post-start"])
def test_startup_hook_cancellation_uses_outer_deadline_and_skips_remaining(
    tmp_path: Path,
    phase: str,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "mounted"
    _write_hook(mounted, f"{phase}.d", "10-hang.sh")
    _write_hook(mounted, f"{phase}.d", "20-skip.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )
    clock = FakeClock()
    process = FakeHookProcess(pid=4141)
    started: list[str] = []
    signals: list[tuple[int, signal.Signals]] = []

    class DeadlineCancellation:
        cancelled = False

        def __call__(self) -> bool:
            return self.cancelled

        def shutdown_deadline(self) -> float:
            return 0.05

        def wait(self, timeout: float) -> bool:
            del timeout
            clock.sleep(0.01)
            self.cancelled = True
            return True

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> FakeHookProcess:
        del cwd, env, description
        assert start_new_session is True
        started.append(Path(argv[-1]).name)
        return process

    def signaler(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            process.returncode = -int(signal.SIGKILL)

    with pytest.raises(RuntimeHookError) as error:
        run_runtime_startup_hooks(
            plan,
            phase,  # type: ignore[arg-type]
            runtime=runtime,
            runner=runner,
            cancel_requested=DeadlineCancellation(),
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            process_group_signaler=signaler,
            event_sink=RecordingRuntimeEventSink(),
        )

    assert started == ["10-hang.sh"]
    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", phase, "10-hang.sh"), "runtime_hook.cancelled")
    ]
    assert signals == [(4141, signal.SIGTERM), (4141, signal.SIGKILL)]
    assert process.waits == 1
    assert clock.now <= 0.05


def test_stop_hook_deadline_kills_process_group_and_skips_remaining(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "mounted"
    _write_hook(mounted, "stop.d", "10-hang.sh")
    _write_hook(mounted, "stop.d", "20-skip.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )
    clock = FakeClock()
    process = FakeHookProcess(pid=4242)
    started: list[str] = []
    signals: list[tuple[int, signal.Signals]] = []

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> FakeHookProcess:
        del cwd, env, description
        assert start_new_session is True
        started.append(Path(argv[-1]).name)
        return process

    def signaler(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            process.returncode = -int(signal.SIGKILL)

    with pytest.raises(RuntimeHookError) as error:
        run_runtime_stop_hooks(
            plan,
            runtime=runtime,
            runner=runner,
            deadline=0.2,
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            process_group_signaler=signaler,
            event_sink=RecordingRuntimeEventSink(),
        )

    assert started == ["10-hang.sh"]
    assert locations_and_codes(error.value) == [
        (
            ("hooks", "mounted", "stop", "10-hang.sh"),
            "runtime_hook.shutdown_deadline",
        )
    ]
    assert signals == [(4242, signal.SIGKILL)]
    assert process.waits == 1


# A failed deadline signal reports the owned group without entering an unbounded wait.
def test_stop_hook_deadline_signal_failure_does_not_wait(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "mounted"
    _write_hook(mounted, "stop.d", "10-hang.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )
    clock = FakeClock()
    process = FakeHookProcess(pid=4243)

    def signaler(pid: int, sig: signal.Signals) -> None:
        del pid, sig
        raise OSError("signal unavailable")

    with pytest.raises(RuntimeHookError) as error:
        run_runtime_stop_hooks(
            plan,
            runtime=runtime,
            runner=lambda *_args, **_kwargs: process,
            deadline=0.1,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            process_group_signaler=signaler,
            event_sink=RecordingRuntimeEventSink(),
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "stop", "10-hang.sh"), "runtime_hook.termination_failed")
    ]
    assert error.value.diagnostics[0].message == (
        "runtime hook process group could not be signaled with SIGKILL"
    )
    signal_error = error.value.__cause__
    assert isinstance(signal_error, ProcessGroupSignalError)
    assert isinstance(signal_error.__cause__, OSError)
    assert "signal unavailable" not in error.value.diagnostics[0].message
    assert process.waits == 0


# A hook that finishes at the shared cutoff does not grant a fresh timeout to
# the next ordered hook; remaining hooks are skipped without being spawned.
def test_stop_hooks_do_not_start_later_hook_after_shared_deadline(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "mounted"
    _write_hook(mounted, "stop.d", "10-finish.sh")
    _write_hook(mounted, "stop.d", "20-skip.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )
    clock = FakeClock()
    started: list[str] = []

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> FakeHookProcess:
        del cwd, env, description
        assert start_new_session is True
        started.append(Path(argv[-1]).name)
        clock.sleep(0.2)
        return FakeHookProcess(pid=4545, returncode=0)

    run_runtime_stop_hooks(
        plan,
        runtime=runtime,
        runner=runner,
        deadline=0.2,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        event_sink=RecordingRuntimeEventSink(),
    )

    assert started == ["10-finish.sh"]


def test_stop_hook_cancellation_terminates_group_and_skips_remaining(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "mounted"
    _write_hook(mounted, "stop.d", "10-hang.sh")
    _write_hook(mounted, "stop.d", "20-skip.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )
    clock = FakeClock()
    process = FakeHookProcess(pid=4343)
    started: list[str] = []
    signals: list[tuple[int, signal.Signals]] = []

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> FakeHookProcess:
        del cwd, env, description
        assert start_new_session is True
        started.append(Path(argv[-1]).name)
        return process

    def signaler(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            process.returncode = -int(signal.SIGKILL)

    with pytest.raises(RuntimeHookError) as error:
        run_runtime_stop_hooks(
            plan,
            runtime=runtime,
            runner=runner,
            cancel_requested=lambda: clock.now >= 0.1,
            deadline=1.0,
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            process_group_signaler=signaler,
            event_sink=RecordingRuntimeEventSink(),
        )

    assert started == ["10-hang.sh"]
    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "stop", "10-hang.sh"), "runtime_hook.cancelled")
    ]
    assert signals == [
        (4343, signal.SIGTERM),
        (4343, signal.SIGKILL),
    ]


# Once cancellation wins, the recorded group remains owned even if its leader
# exits before cleanup signals the group; the leader is then reaped as cancelled.
def test_stop_hook_cancel_winner_signals_group_after_leader_exit(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "mounted"
    _write_hook(mounted, "stop.d", "10-race.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )
    process = FakeHookProcess(pid=4393)
    signals: list[tuple[int, signal.Signals]] = []

    class LeaderExitCancellation:
        calls = 0

        def __call__(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                return False
            process.returncode = 0
            return True

    with pytest.raises(RuntimeHookError) as error:
        run_runtime_stop_hooks(
            plan,
            runtime=runtime,
            runner=lambda *_args, **_kwargs: process,
            cancel_requested=LeaderExitCancellation(),
            process_group_signaler=lambda pid, sig: signals.append((pid, sig)),
            event_sink=RecordingRuntimeEventSink(),
        )

    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "stop", "10-race.sh"), "runtime_hook.cancelled")
    ]
    assert signals == [(4393, signal.SIGTERM)]
    assert process.waits == 1


# Repeated-signal cancellation bypasses the cooperative TERM grace and sends
# SIGKILL once before the hook runner can start another hook.
def test_stop_hook_force_cancellation_skips_termination_grace(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "mounted"
    _write_hook(mounted, "stop.d", "10-hang.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )
    process = FakeHookProcess(pid=4444)
    signals: list[tuple[int, signal.Signals]] = []

    class ForceCancellation:
        calls = 0

        def __call__(self) -> bool:
            self.calls += 1
            return self.calls > 1

        def force_requested(self) -> bool:
            return True

    def signaler(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        process.returncode = -int(signal.SIGKILL)

    cancellation = ForceCancellation()
    with pytest.raises(RuntimeHookError) as error:
        run_runtime_stop_hooks(
            plan,
            runtime=runtime,
            runner=lambda *_args, **_kwargs: process,
            cancel_requested=cancellation,
            process_group_signaler=signaler,
            event_sink=RecordingRuntimeEventSink(),
        )
    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "stop", "10-hang.sh"), "runtime_hook.cancelled")
    ]
    assert signals == [(4444, signal.SIGKILL)]
    assert process.waits == 1


def test_stop_hook_process_cleanup_constants_are_bounded() -> None:
    assert STOP_HOOK_POLL_INTERVAL_SECONDS > 0
    assert STOP_HOOK_TERMINATION_GRACE_SECONDS > 0


def locations_and_codes(
    error: RuntimeHookError,
) -> list[tuple[tuple[object, ...], str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]
