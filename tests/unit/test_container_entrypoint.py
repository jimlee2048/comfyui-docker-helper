"""Tests for the container runtime entrypoint service."""

import os
import signal
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_docker_helper.config import Diagnostic, RuntimeConfig
from comfyui_docker_helper.container import entrypoint as entrypoint_module
from comfyui_docker_helper.container import runtime_files as runtime_files_module
from comfyui_docker_helper.container.download_files import (
    DownloaderSettings,
    FileDownloadItem,
    TransferDownloadFilesError,
)
from comfyui_docker_helper.container.entrypoint import EntrypointError, run_entrypoint
from comfyui_docker_helper.container.readiness import ReadinessError
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeFileDownloadError,
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
    RuntimeFilePlanItem,
    process_runtime_file_downloads,
)
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    RuntimeHookPlan,
    RuntimeHookResult,
    run_runtime_startup_hooks,
)
from comfyui_docker_helper.container.runtime_state import load_runtime_state


def _runtime(tmp_path: Path) -> ContainerRuntime:
    return ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )


def _write(path: Path, document: str) -> Path:
    path.write_text(document, encoding="utf-8")
    return path


def _write_hook(root: Path, phase_dir: str, filename: str) -> Path:
    phase = root / phase_dir
    phase.mkdir(parents=True, exist_ok=True)
    path = phase / filename
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


class FakeChild:
    """Minimal fake Popen-compatible child process."""

    def __init__(self, returncode: int) -> None:
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self.signals: list[signal.Signals] = []
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = self._wait_returncode
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)
        if self._wait_returncode == -int(sig):
            self.returncode = self._wait_returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -int(signal.SIGKILL)


class FakeSshdProcess:
    """Minimal fake sshd process for entrypoint monitoring tests."""

    def __init__(
        self,
        returncode: int | None = None,
        *,
        wait_returncode: int | None = None,
        events: list[str] | None = None,
        name: str = "ssh",
        exit_on_terminate: bool = True,
    ) -> None:
        self.returncode = returncode
        self.wait_returncode = wait_returncode
        self.waited = entrypoint_module.threading.Event()
        self.release = entrypoint_module.threading.Event()
        self.events = events
        self.name = name
        self.exit_on_terminate = exit_on_terminate

    def wait(self) -> int:
        if self.wait_returncode is None:
            self.release.wait()
            self.returncode = 0 if self.returncode is None else self.returncode
        else:
            self.returncode = self.wait_returncode
        self.waited.set()
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        if self.events is not None:
            self.events.append(f"{self.name}-terminate")
        if self.exit_on_terminate:
            self.returncode = 0
            self.release.set()

    def kill(self) -> None:
        if self.events is not None:
            self.events.append(f"{self.name}-kill")
        self.returncode = -int(signal.SIGKILL)
        self.release.set()


class PollingSshdProcess:
    """Fake sshd process with scripted poll results."""

    def __init__(self, poll_results: Sequence[int | None]) -> None:
        self.returncode: int | None = None
        self._poll_results = list(poll_results)

    def wait(self) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def poll(self) -> int | None:
        if self._poll_results:
            self.returncode = self._poll_results.pop(0)
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -int(signal.SIGKILL)


class SignalOnPollSshdProcess(FakeSshdProcess):
    """Fake sshd process that raises a startup signal on the first poll."""

    def __init__(self, trigger: Callable[[], object], events: list[str]) -> None:
        super().__init__(events=events)
        self.trigger = trigger
        self._triggered = False

    def poll(self) -> int | None:
        if not self._triggered:
            self._triggered = True
            self.events.append("ssh-poll")
            self.trigger()
        return self.returncode


class FakeHookProcess:
    """Minimal hook process for entrypoint startup cancellation tests."""

    def __init__(
        self,
        *,
        pid: int,
        trigger: Callable[[], object],
    ) -> None:
        self.pid = pid
        self.trigger = trigger
        self.returncode: int | None = None
        self._triggered = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        if not self._triggered:
            self._triggered = True
            self.trigger()
        return self.returncode

    def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            raise AssertionError("running fake hook process cannot be waited")
        return self.returncode


class FakeClock:
    """Manual monotonic clock for startup hook cancellation tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _capture_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[signal.Signals, object], list[signal.Signals]]:
    handlers: dict[signal.Signals, object] = {}
    restored: list[signal.Signals] = []

    def fake_getsignal(sig: signal.Signals) -> object:
        return f"previous-{sig.name}"

    def fake_signal(sig: signal.Signals, handler: object) -> object:
        handlers[sig] = handler
        if isinstance(handler, str):
            restored.append(sig)
        return f"previous-{sig.name}"

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)
    return handlers, restored


class AsyncBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[FileDownloadItem, DownloaderSettings]] = []
        self.payloads: dict[str, bytes] = {}
        self.failures: dict[str, int | None] = {}
        self.entered = entrypoint_module.threading.Event()
        self.release = entrypoint_module.threading.Event()
        self.block = False
        self.observed_final: bytes | None = None
        self.observed_final_event = entrypoint_module.threading.Event()
        self.final_target: Path | None = None

    def download(
        self,
        item: FileDownloadItem,
        settings: DownloaderSettings,
    ) -> None:
        self.calls.append((item, settings))
        self.entered.set()
        if self.block:
            assert self.final_target is not None
            self.observed_final = self.final_target.read_bytes()
            self.observed_final_event.set()
            self.release.wait(timeout=1)
        remaining = self.failures.get(item.filename, 0)
        if remaining is None or remaining > 0:
            if remaining is not None:
                self.failures[item.filename] = remaining - 1
            item.target.write_bytes(b"partial")
            raise TransferDownloadFilesError(f"failed {item.filename}")
        item.target.write_bytes(self.payloads.get(item.filename, b"downloaded"))


class FakeAsyncHandle:
    def __init__(
        self,
        events: list[str],
        *,
        alive: bool = True,
        complete_on_join: bool = True,
        complete_on_terminate: bool = True,
        on_join: Callable[[], object] | None = None,
    ) -> None:
        self.events = events
        self._alive = alive
        self._complete_on_join = complete_on_join
        self._complete_on_terminate = complete_on_terminate
        self._on_join = on_join

    def request_stop(self) -> None:
        self.events.append("async-stop")

    def terminate_backends(self) -> None:
        self.events.append("async-terminate")
        if self._complete_on_terminate:
            self._alive = False

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self.events.append("async-join")
        if self._on_join is not None:
            self._on_join()
        if self._complete_on_join:
            self._alive = False

    def is_alive(self) -> bool:
        return self._alive


def _async_item(runtime: ContainerRuntime, filename: str) -> RuntimeFilePlanItem:
    return RuntimeFilePlanItem(
        url=f"https://example.com/{filename}",
        directory="models",
        filename=filename,
        relative_target=f"models/{filename}",
        target=runtime.comfyui_path / "models" / filename,
        overwrite=False,
        download_mode="async",
        downloader=None,
        action="download",
    )


def _activate_async_plan(
    runtime: ContainerRuntime,
    state_path: Path,
    *items: RuntimeFilePlanItem,
    config: RuntimeConfig | None = None,
) -> RuntimeFilePlan:
    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        return ()

    queues = entrypoint_module._activate_runtime_file_plan(
        RuntimeFilePlan(items=items),
        config=config or RuntimeConfig.model_validate({}),
        runtime=runtime,
        runtime_downloader=runtime_downloader,
        runtime_state_path=state_path,
    )
    assert queues.sync_plan.items == ()
    return queues.async_plan


def _install_async_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: AsyncBackend,
) -> None:
    monkeypatch.setattr(
        runtime_files_module,
        "HttpxDownloader",
        lambda *, log: backend,
    )


def _runtime_file_staging_target(item: RuntimeFilePlanItem) -> Path:
    return runtime_files_module.runtime_file_staging_target(
        item, runtime_files_module.runtime_file_identity_digest(item)
    )


# Exit and signal tests protect the entrypoint's child-process status mapping
# and forwarded shutdown behavior.
@pytest.mark.parametrize("returncode", [0, 17, -15])
def test_child_exit_code_is_returned(tmp_path: Path, returncode: int) -> None:
    runtime = _runtime(tmp_path)
    child = FakeChild(returncode)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        return child

    expected = 143 if returncode == -15 else returncode
    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={},
            runner=runner,
        )
        == expected
    )


@pytest.mark.parametrize("forwarded", [signal.SIGTERM, signal.SIGINT])
def test_signals_are_forwarded_to_spawned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded: signal.Signals,
) -> None:
    runtime = _runtime(tmp_path)
    handlers: dict[signal.Signals, object] = {}
    restored: list[signal.Signals] = []

    def fake_getsignal(sig: signal.Signals) -> object:
        return f"previous-{sig.name}"

    def fake_signal(sig: signal.Signals, handler: object) -> object:
        handlers[sig] = handler
        if isinstance(handler, str):
            restored.append(sig)
        return f"previous-{sig.name}"

    class SignalingChild(FakeChild):
        def wait(self) -> int:
            handler = handlers[forwarded]
            assert callable(handler)
            handler(forwarded, None)
            self.returncode = self._wait_returncode
            return self._wait_returncode

    signaling_child = SignalingChild(-int(forwarded))

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        return signaling_child

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert run_entrypoint(
        runtime=runtime,
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={},
        runner=runner,
    ) == 128 + int(forwarded)
    assert signaling_child.signals == [forwarded]
    assert restored == [
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGINT,
    ]


def test_forwarded_shutdown_signal_ignored_by_child_escalates_to_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    handlers, restored = _capture_signal_handlers(monkeypatch)
    monkeypatch.setattr(
        entrypoint_module,
        "CHILD_TERMINATION_REAP_GRACE_SECONDS",
        0.0,
    )

    class IgnoringShutdownChild(FakeChild):
        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown signal handler should interrupt wait")
            if self.returncode is None:
                self.returncode = self._wait_returncode
            return self.returncode

    child = IgnoringShutdownChild(0)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        return child

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={},
            runner=runner,
        )
        == 137
    )

    assert child.signals == [signal.SIGTERM]
    assert child.killed is True
    assert child.returncode == -int(signal.SIGKILL)
    assert restored == [
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGINT,
    ]


@pytest.mark.parametrize("forwarded", [signal.SIGTERM, signal.SIGINT])
# Shutdown-ordering tests pin cleanup order across async queues, SSH, hooks, and
# child signal forwarding.
def test_shutdown_signal_runs_stop_hooks_before_forwarding_to_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded: signal.Signals,
) -> None:
    runtime = _runtime(tmp_path)
    baked_hooks = tmp_path / "baked-hooks"
    mounted_hooks = tmp_path / "mounted-hooks"
    _write_hook(baked_hooks, "stop.d", "10-baked.sh")
    _write_hook(mounted_hooks, "stop.d", "10-mounted.py")
    handlers: dict[signal.Signals, object] = {}
    restored: list[signal.Signals] = []
    events: list[str] = []

    def fake_getsignal(sig: signal.Signals) -> object:
        return f"previous-{sig.name}"

    def fake_signal(sig: signal.Signals, handler: object) -> object:
        handlers[sig] = handler
        if isinstance(handler, str):
            restored.append(sig)
        return f"previous-{sig.name}"

    class ShutdownChild(FakeChild):
        def __init__(self) -> None:
            super().__init__(-int(forwarded))
            self.wait_calls = 0

        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[forwarded]
                assert callable(handler)
                handler(forwarded, None)
                raise AssertionError("shutdown signal handler should interrupt wait")
            events.append("wait")
            self.returncode = self._wait_returncode
            return self._wait_returncode

        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = ShutdownChild()

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
        assert cancel_requested() is False
        events.append(
            "stop:" + ",".join(hook.filename for hook in plan.for_phase("stop"))
        )
        return ()

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert run_entrypoint(
        runtime=runtime,
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=tmp_path / "missing-mounted.toml",
        baked_hooks_path=baked_hooks,
        mounted_hooks_path=mounted_hooks,
        environ={},
        runner=runner,
        runtime_stop_hook_runner=runtime_stop_hook_runner,
    ) == 128 + int(forwarded)

    assert events == [
        "stop:10-baked.sh,10-mounted.py",
        f"signal:{forwarded.name}",
        "wait",
    ]
    assert child.signals == [forwarded]
    assert restored == [
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGINT,
    ]


def test_stop_hooks_do_not_run_on_natural_child_exit(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
    calls: list[str] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        return FakeChild(0)

    def runtime_stop_hook_runner(
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log, cancel_requested
        calls.append("stop")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 0
    )

    assert calls == []


def test_shutdown_after_startup_stops_async_before_stop_hooks_and_child_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class ShutdownChild(FakeChild):
        def __init__(self) -> None:
            super().__init__(-int(signal.SIGTERM))
            self.wait_calls = 0

        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown signal handler should interrupt wait")
            events.append("wait")
            self.returncode = self._wait_returncode
            return self._wait_returncode

        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = ShutdownChild()

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

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        return FakeAsyncHandle(events)

    def runtime_stop_hook_runner(
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log
        assert cancel_requested() is False
        events.append("stop")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 143
    )

    assert events == [
        "async-start",
        "spawn",
        "async-stop",
        "async-join",
        "stop",
        "signal:SIGTERM",
        "wait",
    ]


def test_shutdown_after_startup_stops_async_then_ssh_before_hooks_and_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"

[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class ShutdownChild(FakeChild):
        def __init__(self) -> None:
            super().__init__(-int(signal.SIGTERM))
            self.wait_calls = 0

        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown signal handler should interrupt wait")
            events.append("wait")
            self.returncode = self._wait_returncode
            return self._wait_returncode

        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = ShutdownChild()

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

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return FakeSshdProcess(events=events)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        return FakeAsyncHandle(events)

    def runtime_stop_hook_runner(
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log
        assert cancel_requested() is False
        events.append("stop")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 143
    )

    assert events == [
        "ssh-start",
        "async-start",
        "spawn",
        "async-stop",
        "async-join",
        "ssh-terminate",
        "stop",
        "signal:SIGTERM",
        "wait",
    ]


def test_stop_hook_failure_is_logged_and_signal_still_forwards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-fail.sh")
    handlers: dict[signal.Signals, object] = {}
    events: list[str] = []

    def fake_getsignal(sig: signal.Signals) -> object:
        return f"previous-{sig.name}"

    def fake_signal(sig: signal.Signals, handler: object) -> object:
        handlers[sig] = handler
        return f"previous-{sig.name}"

    class ShutdownChild(FakeChild):
        def __init__(self) -> None:
            super().__init__(-int(signal.SIGTERM))
            self.wait_calls = 0

        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown signal handler should interrupt wait")
            events.append("wait")
            self.returncode = self._wait_returncode
            return self._wait_returncode

        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = ShutdownChild()

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
        del plan, runtime, env, log
        assert cancel_requested() is False
        events.append("stop")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "stop", "10-fail.sh"),
                    code="runtime_hook.execution_failed",
                    message="stop hook failed in test",
                ),
            )
        )

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 143
    )

    captured = capsys.readouterr()
    assert events == ["stop", "signal:SIGTERM", "wait"]
    assert child.signals == [signal.SIGTERM]
    assert "Runtime stop hook failed:" in captured.err
    assert "[hooks.mounted.stop.10-fail.sh]" in captured.err
    assert "runtime_hook.execution_failed" in captured.err


def test_ssh_stop_failure_logs_warning_without_overriding_comfyui_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    password = "secret-password"
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[system.ssh]
enable = true
password = "{password}"
""",
    )
    events: list[str] = []

    class FailingTerminateSshd(FakeSshdProcess):
        def terminate(self) -> None:
            events.append("ssh-terminate-failed")
            raise OSError("simulated sshd terminate failure")

    class ExitingChild(FakeChild):
        def wait(self) -> int:
            events.append("wait")
            return super().wait()

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FailingTerminateSshd:
        del config, runtime, log
        events.append("ssh-start")
        return FailingTerminateSshd(events=events)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return ExitingChild(7)

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_ssh_starter=runtime_ssh_starter,
        )
        == 7
    )

    captured = capsys.readouterr()
    assert events == ["ssh-start", "spawn", "wait", "ssh-terminate-failed", "ssh-kill"]
    assert "WARNING: SSH runtime service terminate failed" in captured.err
    assert password not in captured.err


def test_stop_hook_failure_does_not_override_child_exit_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-fail.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class ShutdownChild(FakeChild):
        def __init__(self) -> None:
            super().__init__(17)
            self.wait_calls = 0

        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown signal handler should interrupt wait")
            events.append("wait")
            self.returncode = self._wait_returncode
            return self._wait_returncode

        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = ShutdownChild()

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
        del plan, runtime, env, log, cancel_requested
        events.append("stop-failed")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "stop", "10-fail.sh"),
                    code="runtime_hook.execution_failed",
                    message="stop hook failed in test",
                ),
            )
        )

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 17
    )

    assert events == ["stop-failed", "signal:SIGTERM", "wait"]
    assert child.signals == [signal.SIGTERM]


def test_second_shutdown_signal_cancels_stop_hooks_then_forwards_original_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-hang.sh")
    _write_hook(hooks, "stop.d", "20-skip.sh")
    handlers: dict[signal.Signals, object] = {}
    events: list[str] = []

    def fake_getsignal(sig: signal.Signals) -> object:
        return f"previous-{sig.name}"

    def fake_signal(sig: signal.Signals, handler: object) -> object:
        handlers[sig] = handler
        return f"previous-{sig.name}"

    class ShutdownChild(FakeChild):
        def __init__(self) -> None:
            super().__init__(-int(signal.SIGTERM))
            self.wait_calls = 0

        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown signal handler should interrupt wait")
            events.append("wait")
            self.returncode = self._wait_returncode
            return self._wait_returncode

        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = ShutdownChild()

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
        assert [hook.filename for hook in plan.for_phase("stop")] == [
            "10-hang.sh",
            "20-skip.sh",
        ]
        assert cancel_requested() is False
        events.append("stop-start")
        handler = handlers[signal.SIGINT]
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert cancel_requested() is True
        events.append("stop-cancelled")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "stop", "10-hang.sh"),
                    code="runtime_hook.cancelled",
                    message="runtime hook was cancelled by a shutdown signal",
                ),
            )
        )

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 143
    )

    captured = capsys.readouterr()
    assert events == ["stop-start", "stop-cancelled", "signal:SIGTERM", "wait"]
    assert child.signals == [signal.SIGTERM]
    assert "Runtime stop hook failed:" in captured.err
    assert "runtime_hook.cancelled" in captured.err


def test_second_shutdown_signal_terminates_async_wait_and_forwards_first_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class ShutdownChild(FakeChild):
        def __init__(self) -> None:
            super().__init__(-int(signal.SIGTERM))
            self.wait_calls = 0

        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown signal handler should interrupt wait")
            events.append("wait")
            self.returncode = self._wait_returncode
            return self._wait_returncode

        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = ShutdownChild()

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

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log

        def second_signal() -> None:
            handler = handlers[signal.SIGINT]
            assert callable(handler)
            handler(signal.SIGINT, None)

        events.append("async-start")
        return FakeAsyncHandle(
            events,
            complete_on_join=False,
            on_join=second_signal,
        )

    def runtime_stop_hook_runner(
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log, cancel_requested
        raise AssertionError("stop hooks should be skipped after second signal")

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 143
    )

    assert events == [
        "async-start",
        "spawn",
        "async-stop",
        "async-join",
        "async-terminate",
        "signal:SIGTERM",
        "wait",
    ]


def test_second_shutdown_signal_kills_ssh_and_skips_stop_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"

[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class ShutdownChild(FakeChild):
        def __init__(self) -> None:
            super().__init__(-int(signal.SIGTERM))
            self.wait_calls = 0

        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown signal handler should interrupt wait")
            events.append("wait")
            self.returncode = self._wait_returncode
            return self._wait_returncode

        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = ShutdownChild()

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

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return FakeSshdProcess(events=events, exit_on_terminate=False)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log

        def second_signal() -> None:
            handler = handlers[signal.SIGINT]
            assert callable(handler)
            handler(signal.SIGINT, None)

        events.append("async-start")
        return FakeAsyncHandle(
            events,
            complete_on_join=False,
            on_join=second_signal,
        )

    def runtime_stop_hook_runner(
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log, cancel_requested
        raise AssertionError("stop hooks should be skipped after second signal")

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 143
    )

    assert events == [
        "ssh-start",
        "async-start",
        "spawn",
        "async-stop",
        "async-join",
        "async-terminate",
        "ssh-terminate",
        "ssh-kill",
        "signal:SIGTERM",
        "wait",
    ]


def test_ssh_stop_timeout_kills_sshd_and_returns_false(
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    sshd = FakeSshdProcess(events=events, exit_on_terminate=False)
    shutdown_requested = entrypoint_module.threading.Event()
    now = 0.0

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds if seconds > 0 else 0.1

    stopped = entrypoint_module._stop_sshd_runtime_service(
        sshd,
        cancel_requested=lambda: False,
        shutdown_requested=shutdown_requested,
        timeout=0.2,
        poll_interval=0.1,
        monotonic=monotonic,
        sleep=sleep,
    )

    captured = capsys.readouterr()
    assert stopped is False
    assert shutdown_requested.is_set()
    assert events == ["ssh-terminate", "ssh-kill"]
    assert sshd.waited.is_set()
    assert "WARNING: SSH runtime service did not stop in 0.2s" in captured.err


def test_async_stop_timeout_is_bounded(capsys: pytest.CaptureFixture[str]) -> None:
    events: list[str] = []
    handle = FakeAsyncHandle(
        events,
        complete_on_join=False,
        complete_on_terminate=False,
    )
    now = 0.0

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds if seconds > 0 else 0.1

    stopped = entrypoint_module._stop_runtime_async_download_queue(
        handle,
        cancel_requested=lambda: False,
        timeout=0.2,
        poll_interval=0.1,
        monotonic=monotonic,
        sleep=sleep,
    )

    assert stopped is False
    assert events == [
        "async-stop",
        "async-join",
        "async-join",
        "async-terminate",
        "async-join",
    ]
    assert now == pytest.approx(0.3)
    output = capsys.readouterr().out
    assert "Async runtime download queue stop requested" in output
    assert (
        "WARNING: Async runtime download queue did not stop in 0.2s; "
        "terminating backends"
    ) in output
    assert (
        "WARNING: Async runtime download queue remained alive after backend termination"
    ) in output


def test_async_stop_logs_stopped(capsys: pytest.CaptureFixture[str]) -> None:
    events: list[str] = []
    handle = FakeAsyncHandle(events)

    stopped = entrypoint_module._stop_runtime_async_download_queue(
        handle,
        cancel_requested=lambda: False,
    )

    assert stopped is True
    assert events == ["async-stop", "async-join"]
    output = capsys.readouterr().out
    assert "Async runtime download queue stop requested" in output
    assert "Async runtime download queue stopped" in output


def test_async_stop_interrupted_terminates_backends(
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    handle = FakeAsyncHandle(
        events,
        complete_on_join=False,
        complete_on_terminate=False,
    )
    now = 0.0

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds if seconds > 0 else 0.1

    stopped = entrypoint_module._stop_runtime_async_download_queue(
        handle,
        cancel_requested=lambda: True,
        timeout=0.2,
        poll_interval=0.1,
        monotonic=monotonic,
        sleep=sleep,
    )

    assert stopped is False
    assert events == ["async-stop", "async-terminate", "async-join"]
    output = capsys.readouterr().out
    assert "Async runtime download queue stop requested" in output
    assert (
        "WARNING: Async runtime download queue stop interrupted; terminating backends"
    ) in output


# Startup-interruption tests cover signals that arrive before the normal child
# wait loop begins: downloads, hooks, spawn handoff, readiness, and post-start.
@pytest.mark.parametrize("forwarded", [signal.SIGTERM, signal.SIGINT])
def test_shutdown_signal_during_runtime_downloads_exits_without_spawn_or_stop_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded: signal.Signals,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
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
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        handler = handlers[forwarded]
        assert callable(handler)
        try:
            handler(forwarded, None)
        except Exception as error:
            raise RuntimeFileDownloadError(
                (
                    Diagnostic(
                        path=("files", 0),
                        code="runtime_file.wrapped_signal",
                        message="ordinary download errors can be wrapped",
                    ),
                )
            ) from error
        raise AssertionError("shutdown handler should interrupt downloads")

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

    assert run_entrypoint(
        runtime=runtime,
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
        baked_hooks_path=tmp_path / "missing-baked-hooks",
        mounted_hooks_path=hooks,
        environ={},
        runner=runner,
        runtime_downloader=runtime_downloader,
        runtime_stop_hook_runner=runtime_stop_hook_runner,
        runtime_state_path=tmp_path / "state.json",
    ) == 128 + int(forwarded)

    assert events == ["download"]
    assert restored == [signal.SIGTERM, signal.SIGINT]


def test_shutdown_signal_during_pre_start_hook_cancels_hook_and_prevents_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start.d", "10-hang.sh")
    _write_hook(hooks, "pre-start.d", "20-skip.sh")
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    clock = FakeClock()
    events: list[str] = []
    hook_signals: list[tuple[int, signal.Signals]] = []
    hook_processes: list[FakeHookProcess] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return FakeChild(0)

    def hook_process_runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> FakeHookProcess:
        del cwd, env, description
        assert start_new_session is True
        filename = Path(argv[-1]).name
        events.append(f"hook:{filename}")
        process = FakeHookProcess(
            pid=5151,
            trigger=lambda: handlers[signal.SIGTERM](signal.SIGTERM, None),
        )
        hook_processes.append(process)
        return process

    def process_group_signaler(pid: int, sig: signal.Signals) -> None:
        hook_signals.append((pid, sig))
        if sig == signal.SIGKILL:
            hook_processes[-1].returncode = -int(signal.SIGKILL)

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        return run_runtime_startup_hooks(
            plan,
            phase,
            runtime=runtime,
            env=env,
            log=log,
            runner=hook_process_runner,
            cancel_requested=cancel_requested,
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            process_group_signaler=process_group_signaler,
        )

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 143
    )

    assert events == ["hook:10-hang.sh"]
    assert hook_signals == [(5151, signal.SIGTERM), (5151, signal.SIGKILL)]
    assert hook_processes[0].wait_calls == 1
    assert restored == [signal.SIGTERM, signal.SIGINT]


def test_shutdown_signal_during_spawn_handoff_signals_child_without_stop_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    child = FakeChild(-int(signal.SIGTERM))
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
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        return child

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
        del plan, phase, runtime, env, log, cancel_requested
        events.append("post-start")
        return ()

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 143
    )

    assert events == ["spawn"]
    assert child.signals == [signal.SIGTERM]
    assert restored == [signal.SIGTERM, signal.SIGINT]


def test_shutdown_signal_during_readiness_forwards_to_child_without_stop_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    child = FakeChild(-int(signal.SIGINT))
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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 130
    )

    assert events == ["spawn", "readiness"]
    assert child.signals == [signal.SIGINT]
    assert restored == [signal.SIGTERM, signal.SIGINT]


def test_shutdown_signal_during_readiness_stops_async_before_child_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class OrderingChild(FakeChild):
        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = OrderingChild(-int(signal.SIGINT))

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

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        return FakeAsyncHandle(events)

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port, child
        events.append("readiness")
        handler = handlers[signal.SIGINT]
        assert callable(handler)
        handler(signal.SIGINT, None)
        raise AssertionError("shutdown handler should interrupt readiness")

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            readiness_waiter=readiness_waiter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 130
    )

    assert events == [
        "async-start",
        "spawn",
        "readiness",
        "async-stop",
        "async-join",
        "signal:SIGINT",
    ]


def test_shutdown_signal_during_readiness_stops_ssh_before_child_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class OrderingChild(FakeChild):
        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = OrderingChild(-int(signal.SIGINT))

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

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return FakeSshdProcess(events=events)

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port, child
        events.append("readiness")
        handler = handlers[signal.SIGINT]
        assert callable(handler)
        handler(signal.SIGINT, None)
        raise AssertionError("shutdown handler should interrupt readiness")

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_ssh_starter=runtime_ssh_starter,
            readiness_waiter=readiness_waiter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 130
    )

    assert events == [
        "ssh-start",
        "spawn",
        "readiness",
        "ssh-terminate",
        "signal:SIGINT",
    ]


def test_second_startup_signal_during_async_stop_terminates_and_forwards_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class OrderingChild(FakeChild):
        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = OrderingChild(-int(signal.SIGINT))

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

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log

        def second_signal() -> None:
            handler = handlers[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)

        events.append("async-start")
        return FakeAsyncHandle(
            events,
            complete_on_join=False,
            on_join=second_signal,
        )

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port, child
        events.append("readiness")
        handler = handlers[signal.SIGINT]
        assert callable(handler)
        handler(signal.SIGINT, None)
        raise AssertionError("shutdown handler should interrupt readiness")

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            readiness_waiter=readiness_waiter,
            runtime_state_path=tmp_path / "state.json",
        )
        == 130
    )

    assert events == [
        "async-start",
        "spawn",
        "readiness",
        "async-stop",
        "async-join",
        "async-terminate",
        "signal:SIGINT",
    ]
    assert child.signals == [signal.SIGINT]


def test_shutdown_signal_during_post_start_hook_cancels_hook_and_signals_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-hang.sh")
    _write_hook(hooks, "post-start.d", "20-skip.sh")
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, restored = _capture_signal_handlers(monkeypatch)
    clock = FakeClock()
    child = FakeChild(-int(signal.SIGTERM))
    events: list[str] = []
    hook_signals: list[tuple[int, signal.Signals]] = []
    hook_processes: list[FakeHookProcess] = []

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

    def hook_process_runner(
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        description: str,
        start_new_session: bool = False,
    ) -> FakeHookProcess:
        del cwd, env, description
        assert start_new_session is True
        filename = Path(argv[-1]).name
        events.append(f"hook:{filename}")
        process = FakeHookProcess(
            pid=5252,
            trigger=lambda: handlers[signal.SIGTERM](signal.SIGTERM, None),
        )
        hook_processes.append(process)
        return process

    def process_group_signaler(pid: int, sig: signal.Signals) -> None:
        hook_signals.append((pid, sig))
        if sig == signal.SIGKILL:
            hook_processes[-1].returncode = -int(signal.SIGKILL)

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        return run_runtime_startup_hooks(
            plan,
            phase,
            runtime=runtime,
            env=env,
            log=log,
            runner=hook_process_runner,
            cancel_requested=cancel_requested,
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            process_group_signaler=process_group_signaler,
        )

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 143
    )

    assert events == ["spawn", "readiness", "hook:10-hang.sh"]
    assert hook_signals == [(5252, signal.SIGTERM), (5252, signal.SIGKILL)]
    assert hook_processes[0].wait_calls == 1
    assert child.signals == [signal.SIGTERM]
    assert restored == [signal.SIGTERM, signal.SIGINT]


def test_shutdown_signal_during_post_start_stops_async_before_child_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class OrderingChild(FakeChild):
        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = OrderingChild(-int(signal.SIGTERM))

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

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        return FakeAsyncHandle(events)

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
        del plan, phase, runtime, env, log, cancel_requested
        events.append("post-start")
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "post-start", "10-post.sh"),
                    code="runtime_hook.cancelled",
                    message="post-start cancelled in test",
                ),
            )
        )

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 143
    )

    assert events == [
        "async-start",
        "spawn",
        "readiness",
        "post-start",
        "async-stop",
        "async-join",
        "signal:SIGTERM",
    ]


def test_shutdown_signal_during_post_start_stops_ssh_before_child_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class OrderingChild(FakeChild):
        def send_signal(self, sig: signal.Signals) -> None:
            events.append(f"signal:{sig.name}")
            super().send_signal(sig)

    child = OrderingChild(-int(signal.SIGTERM))

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

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return FakeSshdProcess(events=events)

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
        del plan, phase, runtime, env, log, cancel_requested
        events.append("post-start")
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "post-start", "10-post.sh"),
                    code="runtime_hook.cancelled",
                    message="post-start cancelled in test",
                ),
            )
        )

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_ssh_starter=runtime_ssh_starter,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 143
    )

    assert events == [
        "ssh-start",
        "spawn",
        "readiness",
        "post-start",
        "ssh-terminate",
        "signal:SIGTERM",
    ]


def test_runtime_downloads_run_before_spawn_without_root_lock(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    spawn_calls: list[list[str]] = []
    download_calls: list[RuntimeFilePlan] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        assert len(download_calls) == 1
        spawn_calls.append(list(argv))
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log, state_observer, extra_protected_staging_targets
        assert spawn_calls == []
        download_calls.append(plan)
        return ()

    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "config.lock.toml").exists()
    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=tmp_path / "state.json",
        )
        == 0
    )

    assert len(download_calls) == 1
    assert download_calls[0].items[0].target == (
        runtime.comfyui_path / "models" / "model.bin"
    )
    assert download_calls[0].items[0].action == "download"
    assert spawn_calls


def test_runtime_hook_validation_happens_before_downloads_and_spawn(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "notes.txt")
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
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        return ()

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=tmp_path / "state.json",
        )

    assert events == []
    assert "runtime hook configuration is invalid" in str(error.value)
    assert "[hooks.mounted.post-start.notes.txt]" in str(error.value)
    assert "runtime_hook.unsupported_extension" in str(error.value)


def test_pre_start_hooks_run_after_downloads_and_before_spawn(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start.d", "10-pre.sh")
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
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        assert events == []
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
        assert phase == "pre-start"
        assert [hook.filename for hook in plan.for_phase("pre-start")] == ["10-pre.sh"]
        assert events == ["download"]
        events.append("pre-start")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_hook_runner=runtime_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 0
    )

    assert events == ["download", "pre-start", "spawn"]


def test_pre_start_hook_failure_prevents_spawn(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start.d", "10-pre.sh")
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
        return FakeChild(0)

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
        events.append("pre-start")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "pre-start", "10-pre.sh"),
                    code="runtime_hook.execution_failed",
                    message="hook failed in test",
                ),
            )
        )

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_hook_runner=runtime_hook_runner,
        )

    assert events == ["pre-start"]
    assert "runtime hook failed" in str(error.value)
    assert "[hooks.mounted.pre-start.10-pre.sh]" in str(error.value)


def test_absent_hook_roots_do_not_execute_hook_runner(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    calls: list[str] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        calls.append("spawn")
        return FakeChild(0)

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
        calls.append("hook")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={},
            runner=runner,
            runtime_hook_runner=runtime_hook_runner,
        )
        == 0
    )

    assert calls == ["spawn"]


def test_readiness_is_skipped_without_post_start_hooks(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start.d", "10-pre.sh")
    calls: list[str] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        calls.append("spawn")
        return FakeChild(0)

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
        calls.append("pre-start")
        return ()

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port, child
        raise AssertionError("readiness should not run without post-start hooks")

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_hook_runner=runtime_hook_runner,
            readiness_waiter=readiness_waiter,
        )
        == 0
    )

    assert calls == ["pre-start", "spawn"]


def test_readiness_runs_after_spawn_when_post_start_hooks_exist(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted_config = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
port = 8299
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
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
        return FakeChild(0)

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        assert port == 8299
        assert child.poll() is None
        assert events == ["spawn"]
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
        assert cancel_requested() is False
        assert phase == "post-start"
        assert [hook.filename for hook in plan.for_phase("post-start")] == [
            "10-post.sh"
        ]
        assert events == ["spawn", "readiness"]
        events.append("post-start")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted_config,
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
        )
        == 0
    )

    assert events == ["spawn", "readiness", "post-start"]


@pytest.mark.parametrize("returncode", [0, 7])
def test_child_exit_before_readiness_is_startup_failure(
    tmp_path: Path,
    returncode: int,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    child = FakeChild(returncode)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        child.returncode = returncode
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port
        raise ReadinessError(
            (
                Diagnostic(
                    path=("readiness",),
                    code="readiness.child_exited",
                    message=(
                        "ComfyUI exited before readiness succeeded "
                        f"with code {child.poll()}"
                    ),
                ),
            )
        )

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
        )

    assert not child.terminated
    assert "ComfyUI readiness failed" in str(error.value)
    assert "readiness.child_exited" in str(error.value)
    assert f"code {returncode}" in str(error.value)


def test_readiness_failure_terminates_child_and_prevents_normal_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    clock = FakeClock()
    monkeypatch.setattr(entrypoint_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(entrypoint_module.time, "sleep", clock.sleep)
    child = FakeChild(0)
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
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port
        events.append("readiness")
        assert child.poll() is None
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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
        )

    assert events == ["spawn", "readiness"]
    assert child.terminated is True
    assert child.killed is True
    assert child.returncode == -int(signal.SIGKILL)
    assert "readiness.timeout" in str(error.value)


def test_readiness_failure_reaps_child_that_exits_after_terminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    clock = FakeClock()
    monkeypatch.setattr(entrypoint_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(entrypoint_module.time, "sleep", clock.sleep)

    class ExitsOnTerminateChild(FakeChild):
        def __init__(self, returncode: int) -> None:
            super().__init__(returncode)
            self.polls_after_terminate = 0

        def terminate(self) -> None:
            self.terminated = True

        def poll(self) -> int | None:
            if self.terminated and self.returncode is None:
                self.polls_after_terminate += 1
                if self.polls_after_terminate >= 2:
                    self.returncode = self._wait_returncode
            return self.returncode

    child = ExitsOnTerminateChild(0)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port, child
        raise ReadinessError(
            (
                Diagnostic(
                    path=("readiness",),
                    code="readiness.timeout",
                    message="ComfyUI did not become ready before timeout",
                ),
            )
        )

    with pytest.raises(EntrypointError):
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
        )

    assert child.terminated is True
    assert child.wait_calls == 1
    assert child.returncode == 0


def test_post_start_hook_failure_terminates_child_and_prevents_normal_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-fail.sh")
    clock = FakeClock()
    monkeypatch.setattr(entrypoint_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(entrypoint_module.time, "sleep", clock.sleep)
    child = FakeChild(0)
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
        del plan, runtime, env, log, cancel_requested
        assert phase == "post-start"
        events.append("post-start")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "post-start", "10-fail.sh"),
                    code="runtime_hook.execution_failed",
                    message="post hook failed in test",
                ),
            )
        )

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
        )

    assert events == ["spawn", "readiness", "post-start"]
    assert child.terminated is True
    assert child.killed is True
    assert child.returncode == -int(signal.SIGKILL)
    assert "runtime hook failed" in str(error.value)
    assert "[hooks.mounted.post-start.10-fail.sh]" in str(error.value)


def test_post_start_failure_reaps_child_that_exits_after_terminate(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-fail.sh")

    class ExitsOnTerminateChild(FakeChild):
        def terminate(self) -> None:
            self.terminated = True
            self.returncode = self._wait_returncode

    child = ExitsOnTerminateChild(0)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        return child

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port, child

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
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "post-start", "10-fail.sh"),
                    code="runtime_hook.execution_failed",
                    message="post hook failed in test",
                ),
            )
        )

    with pytest.raises(EntrypointError):
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
        )

    assert child.terminated is True
    assert child.wait_calls == 1
    assert child.returncode == 0


def test_post_start_success_returns_natural_child_exit(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    child = FakeChild(17)
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
        del plan, runtime, env, log
        assert cancel_requested() is False
        assert phase == "post-start"
        assert events == ["spawn", "readiness"]
        events.append("post-start")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
        )
        == 17
    )

    assert events == ["spawn", "readiness", "post-start"]
    assert child.terminated is False


def test_runtime_download_failure_prevents_spawn(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    spawn_calls: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        spawn_calls.append(list(argv))
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        raise RuntimeFileDownloadError(
            (
                Diagnostic(
                    path=("files", 0, "target"),
                    code="runtime_file.test_failure",
                    message="download failed in test",
                ),
            )
        )

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=tmp_path / "state.json",
        )

    assert spawn_calls == []
    assert "runtime download failed" in str(error.value)
    assert "[files.0.target]" in str(error.value)
    assert "runtime_file.test_failure" in str(error.value)


def test_no_runtime_files_ignore_invalid_state_and_spawn(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    state_path = tmp_path / "state.json"
    state_path.write_text("{not-json", encoding="utf-8")
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
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=state_path,
        )
        == 0
    )

    assert events == ["spawn"]
    assert state_path.read_text(encoding="utf-8") == "{not-json"


def test_active_corrupt_runtime_state_fails_before_download_hooks_and_spawn(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    state_path = tmp_path / "state.json"
    state_path.write_text("{not-json", encoding="utf-8")
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start.d", "10-pre.sh")
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
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
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
        del plan, phase, runtime, env, log, cancel_requested
        events.append("hook")
        return ()

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_hook_runner=runtime_hook_runner,
            runtime_state_path=state_path,
        )

    assert events == []
    assert "runtime state failed" in str(error.value)
    assert "not valid JSON" in str(error.value)


def test_active_unwritable_runtime_state_fails_before_download_hooks_and_spawn(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    state_path = tmp_path / "state.json"
    state_path.mkdir()
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
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        return ()

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=state_path,
        )

    assert events == []
    assert "runtime state failed" in str(error.value)


def test_existing_final_file_persists_skipped_and_avoids_downloader(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    final_target = runtime.comfyui_path / "models" / "model.bin"
    final_target.parent.mkdir(parents=True)
    final_target.write_bytes(b"existing")
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
overwrite = false
""",
    )
    state_path = tmp_path / "state.json"
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
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=state_path,
        )
        == 0
    )

    state = load_runtime_state(state_path)
    entry = next(iter(state.downloads.entries.values()))
    assert events == ["spawn"]
    assert entry.status == "skipped"
    assert entry.attempts == 0


def test_scheduled_sync_file_downloads_with_state_observer_completed(
    tmp_path: Path,
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[tuple[FileDownloadItem, DownloaderSettings]] = []

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.write_bytes(b"downloaded")

    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_downloader = "httpx"

[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    backend = Backend()
    state_path = tmp_path / "state.json"

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del extra_protected_staging_targets
        return process_runtime_file_downloads(
            plan,
            config=config,
            backends={"httpx": backend},
            log=log,
            state_observer=state_observer,
        )

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=state_path,
        )
        == 0
    )

    assert (runtime.comfyui_path / "models" / "model.bin").read_bytes() == b"downloaded"
    state = load_runtime_state(state_path)
    entry = next(iter(state.downloads.entries.values()))
    assert entry.status == "completed"
    assert entry.attempts == 1
    assert entry.attempt_run_id == state.run_id
    assert entry.last_error is None
    assert backend.calls[0][0].url == "https://example.com/model.bin"


# Typed downloader boundary tests protect the injection contract between the
# entrypoint and runtime file processing.
def test_runtime_downloader_injection_receives_typed_boundary_kwargs(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    state_path = tmp_path / "state.json"
    sync_item = RuntimeFilePlanItem(
        url="https://example.com/sync.bin",
        directory="models",
        filename="sync.bin",
        relative_target="models/sync.bin",
        target=runtime.comfyui_path / "models" / "sync.bin",
        overwrite=False,
        download_mode="sync",
        downloader=None,
        action="download",
    )
    async_item = _async_item(runtime, "async.bin")
    observed_state_observers: list[
        entrypoint_module.RuntimeDownloadStateObserver | None
    ] = []
    observed_protected_targets: list[tuple[Path, ...]] = []

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log
        assert plan.items == (sync_item,)
        observed_state_observers.append(state_observer)
        observed_protected_targets.append(extra_protected_staging_targets)
        return ()

    queues = entrypoint_module._activate_runtime_file_plan(
        RuntimeFilePlan(items=(sync_item, async_item)),
        config=RuntimeConfig.model_validate({}),
        runtime=runtime,
        runtime_downloader=runtime_downloader,
        runtime_state_path=state_path,
    )

    expected_protected_target = entrypoint_module.runtime_file_staging_target(
        async_item,
        entrypoint_module.runtime_file_identity_digest(async_item),
    )
    assert queues.sync_plan.items == (sync_item,)
    assert queues.async_plan.items == (async_item,)
    assert observed_state_observers and observed_state_observers[0] is not None
    assert observed_protected_targets == [(expected_protected_target,)]


def test_stale_short_signature_runtime_downloader_injection_fails(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    state_path = tmp_path / "state.json"
    item = RuntimeFilePlanItem(
        url="https://example.com/model.bin",
        directory="models",
        filename="model.bin",
        relative_target="models/model.bin",
        target=runtime.comfyui_path / "models" / "model.bin",
        overwrite=False,
        download_mode="sync",
        downloader=None,
        action="download",
    )

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
        return ()

    with pytest.raises(TypeError, match="state_observer"):
        entrypoint_module._activate_runtime_file_plan(
            RuntimeFilePlan(items=(item,)),
            config=RuntimeConfig.model_validate({}),
            runtime=runtime,
            runtime_downloader=runtime_downloader,
            runtime_state_path=state_path,
        )


def test_internal_async_plan_exercises_active_state_gate_without_download(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    state_path = tmp_path / "state.json"
    item = RuntimeFilePlanItem(
        url="https://example.com/async.bin",
        directory="models",
        filename="async.bin",
        relative_target="models/async.bin",
        target=runtime.comfyui_path / "models" / "async.bin",
        overwrite=False,
        download_mode="async",
        downloader=None,
        action="download",
    )
    events: list[str] = []

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        return ()

    queues = entrypoint_module._activate_runtime_file_plan(
        RuntimeFilePlan(items=(item,)),
        config=RuntimeConfig.model_validate({}),
        runtime=runtime,
        runtime_downloader=runtime_downloader,
        runtime_state_path=state_path,
    )

    assert queues.sync_plan.items == ()
    assert queues.async_plan.items == (item,)
    state = load_runtime_state(state_path)
    assert events == []
    entry = next(iter(state.downloads.entries.values()))
    assert entry.target == "models/async.bin"
    assert entry.download_mode == "async"
    assert entry.status == "pending"
    output = capsys.readouterr().out
    assert (
        "Runtime download reconcile: mode=async target=models/async.bin "
        "status=pending scheduled=true source_host=example.com identity=sha256:"
    ) in output
    assert (
        "Runtime download reconciliation persisted: entries=1 async_scheduled=1 "
        "async_skipped=0 stale_entries=0 stale_staging=0"
    ) in output


def test_public_async_runtime_file_records_pending_without_sync_download(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    state_path = tmp_path / "state.json"
    events: list[str] = []
    async_plans: list[RuntimeFilePlan] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        assert events == ["async-start"]
        events.append("spawn")
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        return ()

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> None:
        del config, runtime, log
        assert runtime_state_path == state_path
        assert events == []
        events.append("async-start")
        async_plans.append(plan)

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_state_path=state_path,
        )
        == 0
    )

    state = load_runtime_state(state_path)
    entry = next(iter(state.downloads.entries.values()))
    assert events == ["async-start", "spawn"]
    assert len(async_plans) == 1
    assert [item.relative_target for item in async_plans[0].items] == [
        "models/async.bin"
    ]
    output = capsys.readouterr().out
    assert "Async runtime download queue scheduled: items=1 policy=continue" in output
    assert entry.target == "models/async.bin"
    assert entry.download_mode == "async"
    assert entry.status == "pending"


def test_sync_and_async_runtime_file_ordering_before_spawn(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/sync.bin"
dir = "models"
filename = "sync.bin"
download_mode = "sync"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
download_mode = "async"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start.d", "10-pre.sh")
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
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log, state_observer, extra_protected_staging_targets
        assert [item.relative_target for item in plan.items] == ["models/sync.bin"]
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
        del plan, phase, runtime, env, log, cancel_requested
        events.append("pre-start")
        return ()

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> None:
        del config, runtime, runtime_state_path, log
        assert [item.relative_target for item in plan.items] == ["models/async.bin"]
        events.append("async-start")

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 0
    )

    assert events == ["download", "pre-start", "async-start", "spawn"]


def test_async_queue_acceptance_does_not_block_readiness_or_post_start(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    child = FakeChild(0)
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
        return child

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, extra_protected_staging_targets
        events.append("download")
        return ()

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> None:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")

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
        del plan, runtime, env, log, cancel_requested
        assert phase == "post-start"
        events.append("post-start")
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_async_queue_starter=runtime_async_queue_starter,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 0
    )

    assert events == ["async-start", "spawn", "readiness", "post-start"]


def test_async_queue_start_failure_stops_started_ssh_without_spawning(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
download_mode = "async"
""",
    )
    events: list[str] = []

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return FakeSshdProcess(events=events)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        raise entrypoint_module.RuntimeAsyncQueueStartupError("queue refused")

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return FakeChild(0)

    with pytest.raises(EntrypointError) as raised:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_state_path=tmp_path / "state.json",
        )

    assert "async runtime download queue failed to start: queue refused" in str(
        raised.value
    )
    assert events == ["ssh-start", "async-start", "ssh-terminate"]


def test_shutdown_signal_inside_ssh_starter_terminates_returned_sshd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
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
        return FakeChild(0)

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        return FakeSshdProcess(events=events)

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 143
    )

    assert events == ["ssh-start", "ssh-terminate"]


def test_ssh_exit_after_async_start_stops_queue_before_raising(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
download_mode = "async"
""",
    )
    events: list[str] = []

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> PollingSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return PollingSshdProcess([None, 44])

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del config, runtime, runtime_state_path, log
        assert [item.filename for item in plan.items] == ["async.bin"]
        events.append("async-start")
        return FakeAsyncHandle(events)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return FakeChild(0)

    with pytest.raises(EntrypointError) as raised:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_state_path=tmp_path / "state.json",
        )

    assert "SSH runtime service exited before ComfyUI: 44" in str(raised.value)
    assert events == ["ssh-start", "async-start", "async-stop", "async-join"]


# Async starter injection tests cover lifecycle coordination with ComfyUI and SSH
# without running the real queue worker.
def test_normal_child_exit_stops_accepted_async_queue(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
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
        return FakeChild(7)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        return FakeAsyncHandle(events)

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_state_path=tmp_path / "state.json",
        )
        == 7
    )

    assert events == ["async-start", "spawn", "async-stop", "async-join"]


# Verifies accepted async work is cancelled when ComfyUI cannot be spawned.
def test_spawn_failure_after_async_acceptance_stops_async_queue(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
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
        raise FileNotFoundError("missing executable")

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        return FakeAsyncHandle(events)

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_state_path=tmp_path / "state.json",
        )

    assert events == ["async-start", "spawn", "async-stop", "async-join"]
    assert "ComfyUI executable not found" in str(error.value)


# Verifies accepted async work is cancelled when startup gates fail.
@pytest.mark.parametrize("failure_phase", ["readiness", "post-start"])
def test_startup_failure_after_async_acceptance_stops_async_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    clock = FakeClock()
    monkeypatch.setattr(entrypoint_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(entrypoint_module.time, "sleep", clock.sleep)
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
        return FakeChild(0)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        return FakeAsyncHandle(events)

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port
        assert child.poll() is None
        events.append("readiness")
        if failure_phase == "readiness":
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
        del plan, runtime, env, log, cancel_requested
        assert phase == "post-start"
        events.append("post-start")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "post-start", "10-post.sh"),
                    code="runtime_hook.execution_failed",
                    message="post-start failed in test",
                ),
            )
        )

    with pytest.raises(EntrypointError):
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )

    expected = ["async-start", "spawn", "readiness"]
    if failure_phase == "post-start":
        expected.append("post-start")
    assert events == [*expected, "async-stop", "async-join"]


def test_async_accepted_then_startup_signal_before_spawn_stops_async_without_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
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
        return FakeChild(0)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        with suppress(entrypoint_module._StartupShutdownRequested):
            handler(signal.SIGTERM, None)
        return FakeAsyncHandle(events)

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 143
    )

    assert events == ["async-start", "async-stop", "async-join"]


def test_startup_signal_after_ssh_start_terminates_ssh_without_spawn_or_stop_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
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
        return FakeChild(0)

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> SignalOnPollSshdProcess:
        del config, runtime, log
        events.append("ssh-start")

        def trigger() -> None:
            handler = handlers[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)

        return SignalOnPollSshdProcess(trigger, events)

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 143
    )

    assert events == ["ssh-start", "ssh-poll", "ssh-terminate"]


def test_startup_signal_after_async_acceptance_stops_async_and_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"

[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    handlers, _restored = _capture_signal_handlers(monkeypatch)
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
        return FakeChild(0)

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return FakeSshdProcess(events=events)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        with suppress(entrypoint_module._StartupShutdownRequested):
            handler(signal.SIGTERM, None)
        return FakeAsyncHandle(events)

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_state_path=tmp_path / "state.json",
        )
        == 143
    )

    assert events == [
        "ssh-start",
        "async-start",
        "async-stop",
        "async-join",
        "ssh-terminate",
    ]


def test_second_startup_signal_after_async_stop_still_kills_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"

[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop.d", "10-stop.sh")
    handlers, _restored = _capture_signal_handlers(monkeypatch)
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
        return FakeChild(0)

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return FakeSshdProcess(events=events, exit_on_terminate=False)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> FakeAsyncHandle:
        del plan, config, runtime, runtime_state_path, log

        def second_signal() -> None:
            handler = handlers[signal.SIGINT]
            assert callable(handler)
            handler(signal.SIGINT, None)

        events.append("async-start")
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        with suppress(entrypoint_module._StartupShutdownRequested):
            handler(signal.SIGTERM, None)
        return FakeAsyncHandle(events, on_join=second_signal)

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
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_ssh_starter=runtime_ssh_starter,
            runtime_stop_hook_runner=runtime_stop_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )
        == 143
    )

    assert events == [
        "ssh-start",
        "async-start",
        "async-stop",
        "async-join",
        "ssh-terminate",
        "ssh-kill",
    ]


def test_async_queue_infrastructure_failure_prevents_spawn(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start.d", "10-pre.sh")
    _write_hook(hooks, "post-start.d", "10-post.sh")
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
        return FakeChild(0)

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
        events.append(phase)
        return ()

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> None:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")
        raise entrypoint_module.RuntimeAsyncQueueStartupError(
            "queue socket unavailable"
        )

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        del port, child
        events.append("readiness")

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            baked_hooks_path=tmp_path / "missing-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            readiness_waiter=readiness_waiter,
            runtime_hook_runner=runtime_hook_runner,
            runtime_state_path=tmp_path / "state.json",
        )

    assert events == ["pre-start", "async-start"]
    assert "async runtime download queue failed to start" in str(error.value)
    assert "queue socket unavailable" in str(error.value)


def test_runtime_download_policy_fail_prevents_spawn_after_exhausted_transfer(
    tmp_path: Path,
) -> None:
    class FailingBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[FileDownloadItem, DownloaderSettings]] = []

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.write_bytes(b"partial")
            raise TransferDownloadFilesError("failed in test")

    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_downloader = "httpx"
download_max_attempts = 2
download_failure_policy = "fail"

[[files]]
url = "https://example.com/a.bin"
dir = "models"
filename = "a.bin"

[[files]]
url = "https://example.com/b.bin"
dir = "models"
filename = "b.bin"
""",
    )
    backend = FailingBackend()
    spawn_calls: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del cwd, env, shell
        spawn_calls.append(list(argv))
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del extra_protected_staging_targets
        return process_runtime_file_downloads(
            plan,
            config=config,
            backends={"httpx": backend},
            log=log,
            state_observer=state_observer,
        )

    state_path = tmp_path / "state.json"
    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=state_path,
        )

    assert spawn_calls == []
    assert [call[0].url for call in backend.calls] == [
        "https://example.com/a.bin",
        "https://example.com/a.bin",
    ]
    assert "runtime download failed" in str(error.value)
    assert "runtime_file.download_failed" in str(error.value)
    assert not (runtime.comfyui_path / "models" / "a.bin").exists()
    assert not (runtime.comfyui_path / "models" / "b.bin").exists()
    state = load_runtime_state(state_path)
    digest = next(
        digest
        for digest, entry in state.downloads.entries.items()
        if entry.target == "models/a.bin"
    )
    entry = state.downloads.entries[digest]
    assert entry.status == "exhausted"
    assert entry.attempts == 2
    assert entry.attempt_run_id == state.run_id
    assert entry.last_error is not None
    assert "failed in test" in entry.last_error


def test_runtime_download_policy_continue_spawns_after_exhausted_file_then_later_file(
    tmp_path: Path,
) -> None:
    class FirstFileFailingBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[FileDownloadItem, DownloaderSettings]] = []

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            if item.filename == "a.bin":
                item.target.write_bytes(b"partial")
                raise TransferDownloadFilesError("failed in test")
            item.target.write_bytes(b"later")

    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_downloader = "httpx"
download_max_attempts = 2
download_failure_policy = "continue"

[[files]]
url = "https://example.com/a.bin"
dir = "models"
filename = "a.bin"

[[files]]
url = "https://example.com/b.bin"
dir = "models"
filename = "b.bin"
""",
    )
    backend = FirstFileFailingBackend()
    spawn_calls: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del cwd, env, shell
        spawn_calls.append(list(argv))
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del extra_protected_staging_targets
        return process_runtime_file_downloads(
            plan,
            config=config,
            backends={"httpx": backend},
            log=log,
            state_observer=state_observer,
        )

    state_path = tmp_path / "state.json"
    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=state_path,
        )
        == 0
    )

    assert len(spawn_calls) == 1
    assert [call[0].url for call in backend.calls] == [
        "https://example.com/a.bin",
        "https://example.com/a.bin",
        "https://example.com/b.bin",
    ]
    assert not (runtime.comfyui_path / "models" / "a.bin").exists()
    assert (runtime.comfyui_path / "models" / "b.bin").read_bytes() == b"later"
    state = load_runtime_state(state_path)
    statuses = {
        entry.target: entry.status for entry in state.downloads.entries.values()
    }
    assert statuses == {"models/a.bin": "exhausted", "models/b.bin": "completed"}


def test_runtime_staging_file_is_not_treated_as_completed_final_file(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    staging = (
        runtime.comfyui_path / "models" / ".cdh-staging" / "model.bin.cdh-download"
    )
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"partial")
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    actions: list[str] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: object,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log, state_observer, extra_protected_staging_targets
        actions.extend(item.action for item in plan.items)
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=tmp_path / "state.json",
        )
        == 0
    )

    assert actions == ["download"]


def test_env_downloader_default_affects_runtime_files_without_overriding_explicit(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_downloader = "aria2"

[[files]]
url = "https://example.com/default.bin"
dir = "models"
filename = "default.bin"

[[files]]
url = "https://example.com/explicit.bin"
dir = "models"
filename = "explicit.bin"
downloader = "aria2"
download_mode = "sync"
""",
    )
    seen: list[tuple[str | None, str]] = []
    default_downloader: list[str] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        return FakeChild(0)

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: object,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        extra_protected_staging_targets: tuple[Path, ...] = (),
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del log, state_observer, extra_protected_staging_targets
        default_downloader.append(config.cdh.default_downloader)
        seen.extend((item.downloader, item.download_mode) for item in plan.items)
        return ()

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={"CDH_DEFAULT_DOWNLOADER": "httpx"},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_state_path=tmp_path / "state.json",
        )
        == 0
    )

    assert default_downloader == ["httpx"]
    assert seen == [(None, "sync"), ("aria2", "sync")]


def test_default_async_queue_successful_download_updates_state_and_final_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    state_path = tmp_path / "state.json"
    item = _async_item(runtime, "model.bin")
    plan = _activate_async_plan(runtime, state_path, item)
    backend = AsyncBackend()
    backend.payloads["model.bin"] = b"async-bytes"
    _install_async_backend(monkeypatch, backend)
    messages: list[str] = []

    handle = entrypoint_module.start_runtime_async_download_queue(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        runtime=runtime,
        runtime_state_path=state_path,
        log=messages.append,
    )
    handle.join(timeout=1)

    assert not handle.thread.is_alive()
    assert (
        runtime.comfyui_path / "models" / "model.bin"
    ).read_bytes() == b"async-bytes"
    state = load_runtime_state(state_path)
    entry = next(iter(state.downloads.entries.values()))
    assert entry.status == "completed"
    assert entry.attempts == 1
    assert entry.last_error is None
    assert any(
        "Async runtime download queue accepted: items=1" in message
        for message in messages
    )
    assert any(
        "Runtime download completed: mode=async target=models/model.bin "
        "backend=httpx attempts=1 status=completed" in message
        for message in messages
    )
    assert any(
        "Async runtime download queue finished: items=1" in message
        for message in messages
    )
    assert any(
        "Runtime download state persisted: mode=async target=models/model.bin "
        "status=completed attempts=1 identity=sha256:" in message
        for message in messages
    )


def test_async_retry_success_records_attempts_and_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    state_path = tmp_path / "state.json"
    item = _async_item(runtime, "retry.bin")
    plan = _activate_async_plan(runtime, state_path, item)
    backend = AsyncBackend()
    backend.failures["retry.bin"] = 1
    backend.payloads["retry.bin"] = b"eventual"
    _install_async_backend(monkeypatch, backend)

    handle = entrypoint_module.start_runtime_async_download_queue(
        plan,
        config=RuntimeConfig.model_validate(
            {"cdh": {"default_downloader": "httpx", "download_max_attempts": 2}}
        ),
        runtime=runtime,
        runtime_state_path=state_path,
        log=lambda message: None,
    )
    handle.join(timeout=1)

    state = load_runtime_state(state_path)
    entry = next(iter(state.downloads.entries.values()))
    assert [call[0].filename for call in backend.calls] == ["retry.bin", "retry.bin"]
    assert (runtime.comfyui_path / "models" / "retry.bin").read_bytes() == b"eventual"
    assert entry.status == "completed"
    assert entry.attempts == 2


def test_existing_target_async_skip_writes_skipped_and_does_not_start_queue(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    final_target = runtime.comfyui_path / "models" / "existing.bin"
    final_target.parent.mkdir(parents=True)
    final_target.write_bytes(b"existing")
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/existing.bin"
dir = "models"
filename = "existing.bin"
""",
    )
    state_path = tmp_path / "state.json"
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
        return FakeChild(0)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> None:
        del plan, config, runtime, runtime_state_path, log
        events.append("async-start")

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_state_path=state_path,
        )
        == 0
    )

    state = load_runtime_state(state_path)
    entry = next(iter(state.downloads.entries.values()))
    assert events == ["spawn"]
    assert final_target.read_bytes() == b"existing"
    assert entry.status == "skipped"
    assert entry.attempts == 0


def test_async_overwrite_preserves_old_final_until_atomic_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    final_target = runtime.comfyui_path / "models" / "overwrite.bin"
    final_target.parent.mkdir(parents=True)
    final_target.write_bytes(b"old")
    state_path = tmp_path / "state.json"
    item = _async_item(runtime, "overwrite.bin")
    item = replace(item, overwrite=True, action="overwrite_existing")
    plan = _activate_async_plan(runtime, state_path, item)
    backend = AsyncBackend()
    backend.block = True
    backend.final_target = final_target
    backend.payloads["overwrite.bin"] = b"new"
    _install_async_backend(monkeypatch, backend)

    handle = entrypoint_module.start_runtime_async_download_queue(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        runtime=runtime,
        runtime_state_path=state_path,
        log=lambda message: None,
    )
    assert backend.entered.wait(timeout=1)
    assert backend.observed_final_event.wait(timeout=1)
    assert final_target.read_bytes() == b"old"
    assert backend.observed_final == b"old"

    backend.release.set()
    handle.join(timeout=1)

    assert not handle.thread.is_alive()
    assert final_target.read_bytes() == b"new"


def test_async_file_level_failure_after_acceptance_does_not_prevent_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"
default_downloader = "httpx"
download_max_attempts = 1
download_failure_policy = "fail"

[[files]]
url = "https://example.com/fail.bin"
dir = "models"
filename = "fail.bin"
""",
    )
    backend = AsyncBackend()
    backend.failures["fail.bin"] = None
    _install_async_backend(monkeypatch, backend)
    events: list[str] = []
    handles: list[object] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")

        class Child(FakeChild):
            def wait(self) -> int:
                assert backend.entered.wait(timeout=1)
                return super().wait()

        return Child(0)

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> object:
        handle = entrypoint_module.start_runtime_async_download_queue(
            plan,
            config=config,
            runtime=runtime,
            runtime_state_path=runtime_state_path,
            log=log,
        )
        handles.append(handle)
        return handle

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_state_path=tmp_path / "state.json",
        )
        == 0
    )

    assert events == ["spawn"]
    assert len(handles) == 1
    handles[0].join(timeout=1)
    assert [call[0].filename for call in backend.calls] == ["fail.bin"]


def test_actual_async_queue_cancellation_leaves_downloading_and_pending_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingBackend(AsyncBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            self.entered.set()
            self.release.wait(timeout=1)
            item.target.write_bytes(b"cancelled-after-transfer")

    runtime = _runtime(tmp_path)
    state_path = tmp_path / "state.json"
    first = _async_item(runtime, "a.bin")
    second = _async_item(runtime, "b.bin")
    plan = _activate_async_plan(runtime, state_path, first, second)
    backend = BlockingBackend()
    _install_async_backend(monkeypatch, backend)

    handle = entrypoint_module.start_runtime_async_download_queue(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        runtime=runtime,
        runtime_state_path=state_path,
        log=lambda message: None,
    )
    assert backend.entered.wait(timeout=1)

    handle.request_stop()
    backend.release.set()
    handle.join(timeout=1)

    state = load_runtime_state(state_path)
    entries = {entry.target: entry for entry in state.downloads.entries.values()}
    assert not handle.is_alive()
    assert [call[0].filename for call in backend.calls] == ["a.bin"]
    assert entries["models/a.bin"].status == "downloading"
    assert entries["models/a.bin"].attempts == 1
    assert entries["models/b.bin"].status == "pending"
    assert not first.target.exists()
    assert not second.target.exists()
    assert not _runtime_file_staging_target(first).exists()


def test_pre_acceptance_aria2_prepare_failure_wraps_and_prevents_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPrepareAria2Backend:
        def __enter__(self) -> "FailingPrepareAria2Backend":
            events.append("enter")
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: object | None,
        ) -> None:
            del exc_type, exc_value, traceback
            events.append("exit")

        def prepare(self, settings: DownloaderSettings) -> None:
            del settings
            events.append("prepare")
            raise RuntimeError("aria2 startup failed in test")

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            del item, settings
            events.append("download")

    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
downloader = "aria2"
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
        return FakeChild(0)

    def failing_aria2_factory(*, log: Logger) -> FailingPrepareAria2Backend:
        del log
        events.append("factory")
        return FailingPrepareAria2Backend()

    assert runtime_files_module.download_runtime_files.__kwdefaults__ is not None
    monkeypatch.setitem(
        runtime_files_module.download_runtime_files.__kwdefaults__,
        "aria2_downloader_factory",
        failing_aria2_factory,
    )

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_state_path=tmp_path / "state.json",
        )

    assert events == ["factory", "enter", "prepare", "exit"]
    assert "async runtime download queue failed to start" in str(error.value)
    assert "aria2 startup failed in test" in str(error.value)


def test_pre_acceptance_backend_setup_failure_wraps_and_prevents_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
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
        return FakeChild(0)

    def failing_download_runtime_files(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
        state_observer: entrypoint_module.RuntimeDownloadStateObserver | None = None,
        startup_observer: object | None = None,
        cancel_requested: object | None = None,
        backend_observer: object | None = None,
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log, state_observer, startup_observer
        del cancel_requested, backend_observer
        raise RuntimeError("backend setup failed in test")

    monkeypatch.setattr(
        entrypoint_module, "download_runtime_files", failing_download_runtime_files
    )

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_state_path=tmp_path / "state.json",
        )

    assert events == []
    assert "async runtime download queue failed to start" in str(error.value)
    assert "backend setup failed in test" in str(error.value)


def test_pre_acceptance_state_setup_failure_wraps_and_prevents_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
filename = "async.bin"
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
        return FakeChild(0)

    def failing_load_runtime_state(path: Path) -> object:
        del path
        raise entrypoint_module.RuntimeStateError("state setup failed in test")

    monkeypatch.setattr(
        entrypoint_module, "load_runtime_state", failing_load_runtime_state
    )

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
            runtime_state_path=tmp_path / "state.json",
        )

    assert events == []
    assert "async runtime download queue failed to start" in str(error.value)
    assert "state setup failed in test" in str(error.value)
