"""Runtime lifecycle integration coverage for entrypoint orchestration."""

from __future__ import annotations

import signal
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from comfyui_docker_helper.config import Diagnostic, RuntimeConfig
from comfyui_docker_helper.container import entrypoint as entrypoint_module
from comfyui_docker_helper.container.entrypoint import EntrypointError, run_entrypoint
from comfyui_docker_helper.container.readiness import ReadinessError
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeDownloadStateObserver,
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
)
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    RuntimeHookPlan,
    RuntimeHookResult,
)


class FakeChild:
    """Minimal child process with controllable wait and termination behavior."""

    def __init__(
        self,
        returncode: int = 0,
        *,
        events: list[str] | None = None,
        wait_event: str | None = None,
    ) -> None:
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self._events = events
        self._wait_event = wait_event
        self.signals: list[signal.Signals] = []
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def wait(self) -> int:
        self.wait_calls += 1
        if self._wait_event is not None and self._events is not None:
            self._events.append(self._wait_event)
        if self.returncode is None:
            self.returncode = self._wait_returncode
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)
        if self._events is not None:
            self._events.append(f"forward:{sig.name}")
        if self._wait_returncode == -int(sig):
            self.returncode = self._wait_returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._wait_returncode

    def kill(self) -> None:
        self.killed = True
        if self._events is not None:
            self._events.append("kill")
        self.returncode = -int(signal.SIGKILL)


class SignalOnFirstWaitChild(FakeChild):
    """Child that receives a graceful shutdown signal during normal wait."""

    def __init__(
        self,
        *,
        handlers: Mapping[signal.Signals, object],
        sig: signal.Signals,
        final_returncode: int,
        events: list[str],
    ) -> None:
        super().__init__(final_returncode, events=events)
        self._handlers = handlers
        self._sig = sig
        self._final_returncode = final_returncode

    def wait(self) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            self._events.append("wait:initial")
            handler = self._handlers[self._sig]
            assert callable(handler)
            handler(self._sig, None)
            raise AssertionError("shutdown handler should interrupt first wait")
        self._events.append("wait:final")
        self.returncode = self._final_returncode
        return self._final_returncode

    def send_signal(self, sig: signal.Signals) -> None:
        super().send_signal(sig)
        self.returncode = self._final_returncode


def _runtime(tmp_path: Path) -> ContainerRuntime:
    return ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )


def _write(path: Path, document: str) -> Path:
    path.write_text(document, encoding="utf-8")
    return path


def _runtime_config(root: Path, *, include_file: bool = False) -> Path:
    files = (
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models/checkpoints"
filename = "model.bin"
"""
        if include_file
        else ""
    )
    return _write(
        root / "runtime.toml",
        """
[comfyui]
port = 8299
"""
        + files,
    )


def _write_hook(root: Path, phase: str, filename: str) -> Path:
    phase_dir = root / f"{phase}.d"
    phase_dir.mkdir(parents=True, exist_ok=True)
    path = phase_dir / filename
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


def _missing_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / f"missing-{name}"


def _capture_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[signal.Signals, object], list[signal.Signals]]:
    handlers: dict[signal.Signals, object] = {}
    restored: list[signal.Signals] = []

    def fake_getsignal(sig: signal.Signals) -> object:
        return f"previous-{signal.Signals(sig).name}"

    def fake_signal(sig: signal.Signals, handler: object) -> object:
        normalized = signal.Signals(sig)
        handlers[normalized] = handler
        if isinstance(handler, str):
            restored.append(normalized)
        return f"previous-{normalized.name}"

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)
    return handlers, restored


def _hook_names(plan: RuntimeHookPlan, phase: str) -> list[str]:
    return [hook.filename for hook in plan.for_phase(phase)]


# Happy-path lifecycle coverage pins the entrypoint order from downloads through
# startup hooks, readiness, and normal child wait.
def test_runtime_lifecycle_happy_path_orders_downloads_hooks_readiness_and_wait(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = _runtime_config(tmp_path, include_file=True)
    baked_hooks = tmp_path / "baked-hooks"
    mounted_hooks = tmp_path / "mounted-hooks"
    _write_hook(baked_hooks, "pre-start", "10-baked-pre.sh")
    _write_hook(mounted_hooks, "pre-start", "20-mounted-pre.py")
    _write_hook(mounted_hooks, "post-start", "10-mounted-post.sh")
    _write_hook(mounted_hooks, "stop", "90-stop.sh")
    events: list[str] = []

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log, state_observer, extra_protected_staging_targets
        assert len(plan.items) == 1
        assert plan.items[0].target == (
            runtime.comfyui_path / "models" / "checkpoints" / "model.bin"
        )
        events.append("download")
        return ()

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log
        assert cancel_requested() is False
        if phase == "pre-start":
            assert events == ["download"]
            assert _hook_names(plan, "pre-start") == [
                "10-baked-pre.sh",
                "20-mounted-pre.py",
            ]
        elif phase == "post-start":
            assert events == ["download", "pre-start", "spawn", "readiness"]
            assert _hook_names(plan, "post-start") == ["10-mounted-post.sh"]
        else:
            raise AssertionError(f"unexpected startup hook phase: {phase}")
        events.append(phase)
        return ()

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        assert events == ["download", "pre-start"]
        events.append("spawn")
        return FakeChild(0, events=events, wait_event="wait")

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        assert port == 8299
        assert child.poll() is None
        assert events == ["download", "pre-start", "spawn"]
        events.append("readiness")

    def runtime_stop_hook_runner(
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log, cancel_requested
        events.append("stop")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            runtime_state_path=tmp_path / "state.json",
            baked_config_path=config,
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=baked_hooks,
            mounted_hooks_path=mounted_hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            readiness_waiter=readiness_waiter,
        )
        == 0
    )

    assert events == [
        "download",
        "pre-start",
        "spawn",
        "readiness",
        "post-start",
        "wait",
    ]


# Startup failure coverage ensures failed pre-start, readiness, and post-start
# phases stop later phases and surface actionable diagnostics.
def test_pre_start_failure_after_download_prevents_spawn_and_later_phases(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = _runtime_config(tmp_path, include_file=True)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start", "10-fail.sh")
    _write_hook(hooks, "post-start", "20-post.sh")
    _write_hook(hooks, "stop", "90-stop.sh")
    events: list[str] = []

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        return ()

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log, cancel_requested
        assert phase == "pre-start"
        assert _hook_names(plan, "pre-start") == ["10-fail.sh"]
        events.append("pre-start")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "pre-start", "10-fail.sh"),
                    code="runtime_hook.execution_failed",
                    message="pre-start failed in integration test",
                ),
            )
        )

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return FakeChild()

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            runtime_state_path=tmp_path / "state.json",
            baked_config_path=config,
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_hook_runner=runtime_hook_runner,
            readiness_waiter=lambda *_args, **_kwargs: events.append("readiness"),
            runtime_stop_hook_runner=lambda *_args, **_kwargs: events.append("stop"),
        )

    assert events == ["download", "pre-start"]
    assert "runtime hook failed" in str(error.value)
    assert "[hooks.mounted.pre-start.10-fail.sh]" in str(error.value)


def test_readiness_failure_after_spawn_prevents_post_start_and_is_startup_failure(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start", "10-post.sh")
    events: list[str] = []
    child = FakeChild(0)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        assert port == 8188
        assert child.poll() is None
        events.append("readiness")
        raise ReadinessError(
            (
                Diagnostic(
                    path=("readiness",),
                    code="readiness.timeout",
                    message="ComfyUI did not become ready before timeout",
                ),
            )
        )

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, phase, runtime, env, log, cancel_requested
        events.append("post-start")
        return ()

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_hook_runner=runtime_hook_runner,
            readiness_waiter=readiness_waiter,
        )

    assert events == ["spawn", "readiness"]
    assert child.terminated is True
    assert "ComfyUI readiness failed" in str(error.value)
    assert "readiness.timeout" in str(error.value)


def test_post_start_failure_after_readiness_terminates_child_as_startup_failure(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start", "10-fail.sh")
    events: list[str] = []
    child = FakeChild(0)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port
        assert child.poll() is None
        events.append("readiness")

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log, cancel_requested
        assert phase == "post-start"
        assert _hook_names(plan, "post-start") == ["10-fail.sh"]
        events.append("post-start")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "post-start", "10-fail.sh"),
                    code="runtime_hook.execution_failed",
                    message="post-start failed in integration test",
                ),
            )
        )

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_hook_runner=runtime_hook_runner,
            readiness_waiter=readiness_waiter,
        )

    assert events == ["spawn", "readiness", "post-start"]
    assert child.terminated is True
    assert "runtime hook failed" in str(error.value)
    assert "[hooks.mounted.post-start.10-fail.sh]" in str(error.value)


# Startup shutdown coverage protects cancellation behavior before ComfyUI is
# fully running, including skipped stop hooks during partial startup.
def test_startup_shutdown_during_download_prevents_spawn_and_stop_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = _runtime_config(tmp_path, include_file=True)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop", "90-stop.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        raise AssertionError("shutdown handler should interrupt downloads")

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return FakeChild()

    assert (
        run_entrypoint(
            runtime=runtime,
            runtime_state_path=tmp_path / "state.json",
            baked_config_path=config,
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_stop_hook_runner=lambda *_args, **_kwargs: events.append("stop"),
        )
        == 143
    )

    assert events == ["download"]
    assert restored == [signal.SIGTERM, signal.SIGINT]


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT], ids=lambda sig: sig.name
)
def test_startup_shutdown_during_pre_start_hook_prevents_spawn_and_stop_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sig: signal.Signals,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start", "10-pre.sh")
    _write_hook(hooks, "pre-start", "20-skip.sh")
    _write_hook(hooks, "post-start", "30-post-skip.sh")
    _write_hook(hooks, "stop", "90-stop.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return FakeChild()

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port, child
        events.append("readiness")

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log
        assert phase == "pre-start"
        assert cancel_requested() is False
        assert _hook_names(plan, "pre-start") == ["10-pre.sh", "20-skip.sh"]
        assert _hook_names(plan, "post-start") == ["30-post-skip.sh"]
        assert _hook_names(plan, "stop") == ["90-stop.sh"]
        events.append("pre-start:10-pre.sh")
        handler = handlers[sig]
        assert callable(handler)
        handler(sig, None)
        assert cancel_requested() is True
        events.append(f"pre-start-cancelled:{sig.name}")
        return ()

    assert run_entrypoint(
        runtime=runtime,
        baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
        mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
        baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
        mounted_hooks_path=hooks,
        environ={"PATH": "/usr/bin"},
        runner=runner,
        runtime_hook_runner=runtime_hook_runner,
        runtime_stop_hook_runner=lambda *_args, **_kwargs: events.append("stop"),
        readiness_waiter=readiness_waiter,
    ) == 128 + int(sig)

    assert events == [
        "pre-start:10-pre.sh",
        f"pre-start-cancelled:{sig.name}",
    ]
    assert restored == [signal.SIGTERM, signal.SIGINT]


def test_startup_shutdown_during_readiness_forwards_to_child_and_skips_stop_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start", "10-post.sh")
    _write_hook(hooks, "stop", "90-stop.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []
    child = FakeChild(-int(signal.SIGINT), events=events, wait_event="wait")

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port
        assert child.poll() is None
        events.append("readiness")
        handler = handlers[signal.SIGINT]
        assert callable(handler)
        handler(signal.SIGINT, None)
        raise AssertionError("shutdown handler should interrupt readiness")

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, phase, runtime, env, log, cancel_requested
        events.append("post-start")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=lambda *_args, **_kwargs: events.append("stop"),
            readiness_waiter=readiness_waiter,
        )
        == 130
    )

    assert events == ["spawn", "readiness", "forward:SIGINT", "wait"]
    assert child.signals == [signal.SIGINT]
    assert restored == [signal.SIGTERM, signal.SIGINT]


def test_startup_shutdown_during_readiness_kills_child_that_ignores_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start", "10-post.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    monkeypatch.setattr(
        entrypoint_module,
        "CHILD_TERMINATION_REAP_GRACE_SECONDS",
        0.0,
    )
    events: list[str] = []

    class IgnoringChild(FakeChild):
        def send_signal(self, sig: signal.Signals) -> None:
            self.signals.append(sig)
            if self._events is not None:
                self._events.append(f"forward:{sig.name}")

    child = IgnoringChild(0, events=events)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port
        assert child.poll() is None
        events.append("readiness")
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        raise AssertionError("shutdown handler should interrupt readiness")

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            readiness_waiter=readiness_waiter,
        )
        == 137
    )

    assert events == ["spawn", "readiness", "forward:SIGTERM", "kill"]
    assert child.signals == [signal.SIGTERM]
    assert child.killed is True
    assert child.returncode == -int(signal.SIGKILL)
    assert restored == [signal.SIGTERM, signal.SIGINT]


# Post-readiness shutdown coverage keeps signal forwarding, hook cancellation,
# stop-hook ordering, and child exit-code precedence stable.
@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT], ids=lambda sig: sig.name
)
def test_startup_shutdown_during_post_start_hook_forwards_to_child_without_stop_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sig: signal.Signals,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start", "10-post.sh")
    _write_hook(hooks, "post-start", "20-skip.sh")
    _write_hook(hooks, "stop", "90-stop.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []
    child = FakeChild(-int(sig), events=events, wait_event="wait")

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        assert port == 8188
        assert child.poll() is None
        events.append("readiness")

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log
        assert phase == "post-start"
        assert cancel_requested() is False
        assert _hook_names(plan, "post-start") == ["10-post.sh", "20-skip.sh"]
        assert _hook_names(plan, "stop") == ["90-stop.sh"]
        assert events == ["spawn", "readiness"]
        events.append("post-start:10-post.sh")
        handler = handlers[sig]
        assert callable(handler)
        handler(sig, None)
        assert cancel_requested() is True
        events.append(f"post-start-cancelled:{sig.name}")
        return ()

    assert run_entrypoint(
        runtime=runtime,
        baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
        mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
        baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
        mounted_hooks_path=hooks,
        environ={"PATH": "/usr/bin"},
        runner=runner,
        runtime_hook_runner=runtime_hook_runner,
        runtime_stop_hook_runner=lambda *_args, **_kwargs: events.append("stop"),
        readiness_waiter=readiness_waiter,
    ) == 128 + int(sig)

    assert events == [
        "spawn",
        "readiness",
        "post-start:10-post.sh",
        f"post-start-cancelled:{sig.name}",
        f"forward:{sig.name}",
        "wait",
    ]
    assert child.signals == [sig]
    assert restored == [signal.SIGTERM, signal.SIGINT]


def test_graceful_shutdown_runs_stop_hooks_before_forwarding_and_child_result_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop", "90-stop.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []
    child = SignalOnFirstWaitChild(
        handlers=handlers,
        sig=signal.SIGTERM,
        final_returncode=23,
        events=events,
    )

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return child

    def runtime_stop_hook_runner(
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log
        assert cancel_requested() is False
        assert _hook_names(plan, "stop") == ["90-stop.sh"]
        events.append("stop:90-stop.sh")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "stop", "90-stop.sh"),
                    code="runtime_hook.timeout",
                    message="stop hook timed out in integration test",
                ),
            )
        )

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 23
    )

    captured = capsys.readouterr()
    assert events == [
        "spawn",
        "wait:initial",
        "stop:90-stop.sh",
        "forward:SIGTERM",
        "wait:final",
    ]
    assert child.signals == [signal.SIGTERM]
    assert "Runtime stop hook failed:" in captured.err
    assert "runtime_hook.timeout" in captured.err
    assert restored == [
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGINT,
    ]
