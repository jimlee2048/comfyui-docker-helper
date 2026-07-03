"""Tests for runtime lifecycle hook discovery and pre-start execution."""

from __future__ import annotations

import os
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
)
from comfyui_docker_helper.container.runtime_hooks import (
    STOP_HOOK_POLL_INTERVAL_SECONDS,
    STOP_HOOK_TERMINATION_GRACE_SECONDS,
    STOP_HOOK_TIMEOUT_SECONDS,
    RuntimeHookError,
    discover_runtime_hooks,
    run_runtime_hooks,
    run_runtime_startup_hooks,
    run_runtime_stop_hooks,
)


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


def test_unknown_root_entries_and_future_phase_dirs_are_ignored(tmp_path: Path) -> None:
    baked = tmp_path / "baked"
    baked.mkdir()
    (baked / "README.md").write_text("ignored\n", encoding="utf-8")
    (baked / "future-start.d").mkdir()
    (baked / "future-start.d" / "bad.txt").write_text("ignored\n", encoding="utf-8")
    _write_hook(baked, "pre-start.d", "10-pre.sh")

    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=tmp_path / "missing-mounted",
    )

    assert [(hook.source, hook.phase, hook.filename) for hook in plan.hooks] == [
        ("baked", "pre-start", "10-pre.sh")
    ]


def test_strict_validation_checks_all_known_phase_dirs(tmp_path: Path) -> None:
    baked = tmp_path / "baked"
    mounted = tmp_path / "mounted"
    _write_hook(baked, "pre-start.d", "10-pre.sh")
    _write_hook(mounted, "pre-start.d", "10-pre.sh")
    _write_hook(mounted, "post-start.d", "notes.txt")
    (mounted / "stop.d" / "nested").mkdir(parents=True)

    with pytest.raises(RuntimeHookError) as error:
        discover_runtime_hooks(baked_hooks_path=baked, mounted_hooks_path=mounted)

    assert locations_and_codes(error.value) == [
        (
            ("hooks", "mounted", "post-start", "notes.txt"),
            "runtime_hook.unsupported_extension",
        ),
        (("hooks", "mounted", "stop", "nested"), "runtime_hook.directory"),
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


def test_run_pre_start_hooks_uses_suffix_mapping_env_cwd_and_logs(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    baked = tmp_path / "baked"
    _write_hook(baked, "pre-start.d", "10-shell.sh")
    _write_hook(baked, "pre-start.d", "20-python.py")
    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=tmp_path / "missing-mounted",
    )
    calls: list[tuple[list[str], Path, dict[str, str], str]] = []
    logs: list[str] = []

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
    ) -> object:
        calls.append(
            (
                [os.fspath(argument) for argument in argv],
                Path(cwd),
                dict(env),
                description,
            )
        )
        return object()

    results = run_runtime_hooks(
        plan,
        "pre-start",
        runtime=runtime,
        env={"PATH": "/usr/bin", "EXTRA": "1"},
        log=logs.append,
        runner=runner,
    )

    assert [result.status for result in results] == ["completed", "completed"]
    assert calls == [
        (
            ["bash", str(baked / "pre-start.d" / "10-shell.sh")],
            runtime.comfyui_path,
            {
                "PATH": f"{runtime.virtual_env / 'bin'}:/usr/bin",
                "EXTRA": "1",
                "WORKSPACE": str(runtime.workspace),
                "COMFYUI_PATH": str(runtime.comfyui_path),
                "VIRTUAL_ENV": str(runtime.virtual_env),
            },
            "runtime hook baked/pre-start/10-shell.sh",
        ),
        (
            [str(runtime.python), str(baked / "pre-start.d" / "20-python.py")],
            runtime.comfyui_path,
            {
                "PATH": f"{runtime.virtual_env / 'bin'}:/usr/bin",
                "EXTRA": "1",
                "WORKSPACE": str(runtime.workspace),
                "COMFYUI_PATH": str(runtime.comfyui_path),
                "VIRTUAL_ENV": str(runtime.virtual_env),
            },
            "runtime hook baked/pre-start/20-python.py",
        ),
    ]
    assert logs == [
        "Running runtime hook source=baked phase=pre-start filename=10-shell.sh",
        "Running runtime hook source=baked phase=pre-start filename=20-python.py",
    ]


def test_pre_start_hook_failure_stops_phase(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    baked = tmp_path / "baked"
    _write_hook(baked, "pre-start.d", "10-ok.sh")
    _write_hook(baked, "pre-start.d", "20-fail.sh")
    _write_hook(baked, "pre-start.d", "30-skip.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=baked,
        mounted_hooks_path=tmp_path / "missing-mounted",
    )
    calls: list[str] = []

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
    ) -> object:
        del cwd, env, description
        filename = Path(argv[-1]).name
        calls.append(filename)
        if filename == "20-fail.sh":
            raise ContainerCommandError("hook failed", exit_code=12)
        return object()

    with pytest.raises(RuntimeHookError) as error:
        run_runtime_hooks(
            plan,
            "pre-start",
            runtime=runtime,
            env={},
            runner=runner,
        )

    assert calls == ["10-ok.sh", "20-fail.sh"]
    assert locations_and_codes(error.value) == [
        (("hooks", "baked", "pre-start", "20-fail.sh"), "runtime_hook.execution_failed")
    ]


def test_stop_hooks_request_process_group_and_keep_logging(tmp_path: Path) -> None:
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
    logs: list[str] = []

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
        log=logs.append,
        runner=runner,
    )

    assert calls == [("10-baked.sh", True), ("10-mounted.py", True)]
    assert logs == [
        "Running runtime hook source=baked phase=stop filename=10-baked.sh",
        "Running runtime hook source=mounted phase=stop filename=10-mounted.py",
    ]


def test_startup_hook_cancellation_terminates_group_and_skips_remaining(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = tmp_path / "mounted"
    _write_hook(mounted, "pre-start.d", "10-hang.sh")
    _write_hook(mounted, "pre-start.d", "20-skip.sh")
    plan = discover_runtime_hooks(
        baked_hooks_path=tmp_path / "missing-baked",
        mounted_hooks_path=mounted,
    )
    clock = FakeClock()
    process = FakeHookProcess(pid=4141)
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
        run_runtime_startup_hooks(
            plan,
            "pre-start",
            runtime=runtime,
            runner=runner,
            cancel_requested=lambda: clock.now >= 0.1,
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            process_group_signaler=signaler,
        )

    assert started == ["10-hang.sh"]
    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "pre-start", "10-hang.sh"), "runtime_hook.cancelled")
    ]
    assert signals == [(4141, signal.SIGTERM), (4141, signal.SIGKILL)]
    assert process.waits == 1


def test_stop_hook_timeout_cancels_process_group_and_skips_remaining(
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
            timeout_seconds=0.2,
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            process_group_signaler=signaler,
        )

    assert started == ["10-hang.sh"]
    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "stop", "10-hang.sh"), "runtime_hook.timeout")
    ]
    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert process.waits == 1


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
            timeout_seconds=1.0,
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            process_group_signaler=signaler,
        )

    assert started == ["10-hang.sh"]
    assert locations_and_codes(error.value) == [
        (("hooks", "mounted", "stop", "10-hang.sh"), "runtime_hook.cancelled")
    ]
    assert signals == [(4343, signal.SIGTERM), (4343, signal.SIGKILL)]
    assert process.waits == 1


def test_stop_hook_termination_omits_sigkill_when_hook_exits_during_grace(
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
    process = FakeHookProcess(pid=4444)
    signals: list[tuple[int, signal.Signals]] = []

    def runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> FakeHookProcess:
        del argv, cwd, env, description, start_new_session
        return process

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if signals == [(4444, signal.SIGTERM)]:
            process.returncode = -int(signal.SIGTERM)

    def signaler(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    with pytest.raises(RuntimeHookError):
        run_runtime_stop_hooks(
            plan,
            runtime=runtime,
            runner=runner,
            cancel_requested=lambda: clock.now >= 0.1,
            timeout_seconds=1.0,
            termination_grace_seconds=0.5,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=sleep,
            process_group_signaler=signaler,
        )

    assert signals == [(4444, signal.SIGTERM)]
    assert process.waits == 1


def test_stop_hook_timeout_constants_are_bounded() -> None:
    assert 0 < STOP_HOOK_POLL_INTERVAL_SECONDS < STOP_HOOK_TIMEOUT_SECONDS
    assert 0 < STOP_HOOK_TERMINATION_GRACE_SECONDS < STOP_HOOK_TIMEOUT_SECONDS


def locations_and_codes(
    error: RuntimeHookError,
) -> list[tuple[tuple[object, ...], str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]
