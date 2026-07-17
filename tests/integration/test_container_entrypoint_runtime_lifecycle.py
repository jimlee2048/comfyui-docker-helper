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
    RuntimeFileDownloadError,
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
    runtime = ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )
    runtime.comfyui_path.mkdir(parents=True)
    return runtime


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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log, state_observer
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer
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


# A synchronous download failure aborts before any long-lived service starts.
def test_file_infrastructure_failure_prevents_application_spawn(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "sync"

[[files]]
url = "https://example.com/model.bin"
dir = "models/checkpoints"
filename = "model.bin"
""",
    )
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

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: RuntimeDownloadStateObserver | None = None,
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log, state_observer
        assert len(plan.items) == 1
        events.append("sync-download")
        raise RuntimeFileDownloadError(
            (
                Diagnostic(
                    path=("files", 0, "target"),
                    code="runtime_file.transfer_failed",
                    message="download failed",
                ),
            )
        )

    with pytest.raises(EntrypointError) as raised:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=config,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=tmp_path / "state.json",
        )

    assert events == ["sync-download"]
    assert "failed" in str(raised.value)


# Startup failure cleanup stops every already-owned async/SSH resource in order
# before the error returns to the container boundary.
@pytest.mark.parametrize(
    "failure_point",
    ["async-start", "spawn", "readiness", "post-start"],
)
def test_owned_resources_are_stopped_at_each_startup_failure_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[system.ssh]
enable = true
password = "secret"

[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/model.bin"
dir = "models/checkpoints"
filename = "model.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start", "10-post.sh")
    events: list[str] = []

    class OwnedSshd:
        returncode: int | None = None
        waited = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            events.append("ssh:stop")
            self.returncode = 0

        def kill(self) -> None:
            pytest.fail("cooperative sshd cleanup must not require SIGKILL")

        def wait(self) -> int:
            events.append("ssh:wait")
            self.waited = True
            assert self.returncode is not None
            return self.returncode

    class OwnedAsyncQueue:
        alive = True

        def request_stop(self) -> None:
            events.append("async:stop")

        def terminate_backends(self) -> None:
            pytest.fail("cooperative async cleanup must not terminate backends")

        def join(self, timeout: float | None = None) -> None:
            del timeout
            events.append("async:join")
            self.alive = False

        def is_alive(self) -> bool:
            return self.alive

    sshd = OwnedSshd()
    async_queue = OwnedAsyncQueue()
    child: FakeChild | None = None
    monkeypatch.setattr(
        entrypoint_module,
        "_monitor_sshd_after_comfyui_start",
        lambda *_args, **_kwargs: None,
    )

    def runtime_ssh_starter(
        config: RuntimeConfig,
        **kwargs: object,
    ) -> OwnedSshd:
        del config, kwargs
        events.append("ssh:start")
        return sshd

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        **kwargs: object,
    ) -> OwnedAsyncQueue:
        del kwargs
        assert len(plan.items) == 1
        events.append("async:start")
        if failure_point == "async-start":
            raise entrypoint_module.RuntimeAsyncQueueStartupError("queue unavailable")
        return async_queue

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        nonlocal child
        del argv, cwd, env, shell
        events.append("spawn")
        if failure_point == "spawn":
            raise FileNotFoundError("missing application")
        child = FakeChild(0, events=events)
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port, child
        events.append("readiness")
        if failure_point == "readiness":
            raise ReadinessError(
                (
                    Diagnostic(
                        path=("readiness",),
                        code="readiness.timeout",
                        message="application did not become ready",
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
        del plan, runtime, env, log, cancel_requested
        assert phase == "post-start"
        events.append("post-start")
        if failure_point == "post-start":
            raise RuntimeHookError(
                (
                    Diagnostic(
                        path=("hooks", "mounted", "post-start", "10-post.sh"),
                        code="runtime_hook.execution_failed",
                        message="post-start failed",
                    ),
                )
            )
        pytest.fail(f"unexpected successful startup for {failure_point}")

    with pytest.raises(EntrypointError):
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=config,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_hook_runner=runtime_hook_runner,
            readiness_waiter=readiness_waiter,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_state_path=tmp_path / "state.json",
        )

    prefix = ["ssh:start", "async:start"]
    if failure_point != "async-start":
        prefix.append("spawn")
    if failure_point in {"readiness", "post-start"}:
        prefix.append("readiness")
    if failure_point == "post-start":
        prefix.append("post-start")
    expected_cleanup = ["ssh:stop", "ssh:wait"]
    if failure_point != "async-start":
        expected_cleanup = ["async:stop", "async:join", *expected_cleanup]
    assert events == [*prefix, *expected_cleanup]
    assert sshd.returncode == 0
    assert sshd.waited is True
    assert (failure_point == "async-start") or async_queue.alive is False
    if child is not None:
        assert child.returncode == 0
        assert child.terminated is True


# An sshd exit detected after async acceptance stops the queue before reporting
# the startup failure.
def test_early_sshd_exit_after_async_acceptance_stops_queue(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[system.ssh]
enable = true
password = "secret"

[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/model.bin"
dir = "models/checkpoints"
filename = "model.bin"
""",
    )
    events: list[str] = []

    class ExitingSshd:
        def __init__(self) -> None:
            self.polls = iter((None, 44))
            self.returncode: int | None = None

        def poll(self) -> int | None:
            self.returncode = next(self.polls, 44)
            return self.returncode

        def terminate(self) -> None:
            pytest.fail("an exited sshd must not be terminated again")

        def kill(self) -> None:
            pytest.fail("an exited sshd must not be killed")

        def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

    class OwnedAsyncQueue:
        alive = True

        def request_stop(self) -> None:
            events.append("async:stop")

        def terminate_backends(self) -> None:
            pytest.fail("cooperative async cleanup must not terminate backends")

        def join(self, timeout: float | None = None) -> None:
            del timeout
            events.append("async:join")
            self.alive = False

        def is_alive(self) -> bool:
            return self.alive

    async_queue = OwnedAsyncQueue()

    def runtime_ssh_starter(
        config: RuntimeConfig,
        **kwargs: object,
    ) -> ExitingSshd:
        del config, kwargs
        events.append("ssh:start")
        return ExitingSshd()

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        **kwargs: object,
    ) -> OwnedAsyncQueue:
        del kwargs
        assert len(plan.items) == 1
        events.append("async:start")
        return async_queue

    with pytest.raises(EntrypointError, match="SSH runtime service exited"):
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=config,
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: pytest.fail(
                "application must not spawn after sshd exit"
            ),
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_state_path=tmp_path / "state.json",
        )

    assert events == ["ssh:start", "async:start", "async:stop", "async:join"]
    assert async_queue.alive is False


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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer
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


# A repeated catchable signal immediately cancels pre-stop work while preserving
# the original signal forwarded to ComfyUI.
def test_repeated_shutdown_signal_forces_stop_hook_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop", "10-hang.sh")
    _write_hook(hooks, "stop", "20-skip.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class RepeatedSignalChild(FakeChild):
        def __init__(self) -> None:
            super().__init__(-int(signal.SIGTERM), events=events)

        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown handler should interrupt initial wait")
            events.append("wait:final")
            self.returncode = self._wait_returncode
            return self._wait_returncode

    child = RepeatedSignalChild()

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
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
        assert _hook_names(plan, "stop") == ["10-hang.sh", "20-skip.sh"]
        assert cancel_requested() is False
        events.append("stop:10-hang.sh")
        handler = handlers[signal.SIGINT]
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert cancel_requested() is True
        events.append("stop:cancelled")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "stop", "10-hang.sh"),
                    code="runtime_hook.cancelled",
                    message="runtime hook was cancelled by a shutdown signal",
                ),
            )
        )

    assert run_entrypoint(
        runtime=runtime,
        baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
        mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
        baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
        mounted_hooks_path=hooks,
        environ={"PATH": "/usr/bin"},
        runner=runner,
        runtime_stop_hook_runner=runtime_stop_hook_runner,
    ) == 128 + int(signal.SIGTERM)

    captured = capsys.readouterr()
    assert events == [
        "stop:10-hang.sh",
        "stop:cancelled",
        "forward:SIGTERM",
        "wait:final",
    ]
    assert child.signals == [signal.SIGTERM]
    assert "runtime_hook.cancelled" in captured.err


# A child that ignores the forwarded shutdown signal is killed and reaped within
# the owned grace period.
def test_shutdown_kills_child_that_ignores_forwarded_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    monkeypatch.setattr(
        entrypoint_module,
        "CHILD_TERMINATION_REAP_GRACE_SECONDS",
        0.0,
    )

    class IgnoringSignalChild(FakeChild):
        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown handler should interrupt initial wait")
            if self.returncode is None:
                self.returncode = self._wait_returncode
            return self.returncode

    child = IgnoringSignalChild(0)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        return child

    assert run_entrypoint(
        runtime=runtime,
        baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
        mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
        environ={"PATH": "/usr/bin"},
        runner=runner,
    ) == 128 + int(signal.SIGKILL)

    assert child.signals == [signal.SIGTERM]
    assert child.killed is True
    assert child.returncode == -int(signal.SIGKILL)


# Auxiliary shutdown is time-bounded and escalates both sshd and transfer
# backends that do not stop cooperatively.
def test_auxiliary_stop_timeouts_force_owned_cleanup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []

    class StuckSshd:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            events.append("ssh:terminate")

        def kill(self) -> None:
            events.append("ssh:kill")
            self.returncode = -int(signal.SIGKILL)

        def wait(self) -> int:
            events.append("ssh:wait")
            assert self.returncode is not None
            return self.returncode

    class StuckAsyncQueue:
        def request_stop(self) -> None:
            events.append("async:stop")

        def terminate_backends(self) -> None:
            events.append("async:terminate")

        def join(self, timeout: float | None = None) -> None:
            del timeout
            events.append("async:join")

        def is_alive(self) -> bool:
            return True

    def clock() -> tuple[Callable[[], float], Callable[[float], None]]:
        now = 0.0

        def monotonic() -> float:
            return now

        def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds if seconds > 0 else 0.1

        return monotonic, sleep

    ssh_monotonic, ssh_sleep = clock()
    stopped_ssh = entrypoint_module._stop_sshd_runtime_service(
        StuckSshd(),
        cancel_requested=lambda: False,
        shutdown_requested=entrypoint_module.threading.Event(),
        timeout=0.2,
        poll_interval=0.1,
        monotonic=ssh_monotonic,
        sleep=ssh_sleep,
    )
    async_monotonic, async_sleep = clock()
    stopped_async = entrypoint_module._stop_runtime_async_download_queue(
        StuckAsyncQueue(),
        cancel_requested=lambda: False,
        timeout=0.2,
        poll_interval=0.1,
        monotonic=async_monotonic,
        sleep=async_sleep,
    )

    captured = capsys.readouterr()
    assert stopped_ssh is False
    assert stopped_async is False
    assert events == [
        "ssh:terminate",
        "ssh:kill",
        "ssh:wait",
        "async:stop",
        "async:join",
        "async:join",
        "async:terminate",
        "async:join",
    ]
    assert "SSH runtime service did not stop in 0.2s" in captured.err
    assert "Async runtime download queue did not stop in 0.2s" in captured.out
    assert "remained alive after backend termination" in captured.out


# Graceful shutdown runs stop hooks before signal forwarding while preserving
# the final child result as the container exit code.
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
