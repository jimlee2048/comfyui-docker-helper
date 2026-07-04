"""Tests for the container runtime entrypoint service."""

import os
import signal
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from comfyui_docker_helper.config import Diagnostic, RuntimeConfig
from comfyui_docker_helper.container import entrypoint as entrypoint_module
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
        self.wait_calls = 0

    def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = self._wait_returncode
        return self._wait_returncode

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.terminated = True


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


def test_default_argv_uses_runtime_defaults_and_venv_python(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    calls: list[tuple[list[str], str, dict[str, str]]] = []
    child = FakeChild(0)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        calls.append((list(argv), cwd, dict(env)))
        return child

    exit_code = run_entrypoint(
        runtime=runtime,
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={"PATH": "/usr/bin"},
        runner=runner,
    )

    assert exit_code == 0
    assert calls == [
        (
            [
                str(runtime.python),
                str(runtime.comfyui_path / "main.py"),
                "--listen",
                "0.0.0.0",
                "--port",
                "8188",
                "--disable-auto-launch",
            ],
            str(runtime.comfyui_path),
            {
                "PATH": f"{runtime.virtual_env / 'bin'}:/usr/bin",
                "WORKSPACE": str(runtime.workspace),
                "COMFYUI_PATH": str(runtime.comfyui_path),
                "VIRTUAL_ENV": str(runtime.virtual_env),
            },
        )
    ]


def test_mounted_config_and_env_overrides_affect_argv(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
listen = "127.0.0.1"
port = 8190
extra_args = ["--cpu"]
""",
    )
    calls: list[list[str]] = []
    child = FakeChild(0)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        calls.append(list(argv))
        return child

    exit_code = run_entrypoint(
        runtime=runtime,
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
        environ={
            "CDH_COMFYUI_PORT": "8288",
            "CDH_COMFYUI_EXTRA_ARGS": '--preview-method "latent2rgb"',
        },
        runner=runner,
    )

    assert exit_code == 0
    assert calls == [
        [
            str(runtime.python),
            str(runtime.comfyui_path / "main.py"),
            "--listen",
            "127.0.0.1",
            "--port",
            "8288",
            "--disable-auto-launch",
            "--preview-method",
            "latent2rgb",
        ]
    ]


def test_runtime_config_warnings_are_printed_before_spawn(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system]
workspace = "/srv"

[comfyui]
listen = "127.0.0.1"
""",
    )
    calls: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        calls.append(list(argv))
        return FakeChild(0)

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
        )
        == 0
    )

    captured = capsys.readouterr()
    assert calls
    assert captured.out == ""
    assert "Runtime configuration warnings:" in captured.err
    assert "[system]" in captured.err
    assert "runtime.host_only_ignored" in captured.err
    assert "severity=warning" in captured.err


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


@pytest.mark.parametrize("forwarded", [signal.SIGTERM, signal.SIGINT])
# Lifecycle signal tests pin the ordered windows for hook execution, child
# forwarding, and cancellation during startup and shutdown.
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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


def test_runtime_validation_failure_happens_before_spawn(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    calls: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        calls.append(list(argv))
        return FakeChild(0)

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"CDH_COMFYUI_PORT": "0"},
            runner=runner,
        )

    assert calls == []
    assert "runtime configuration is invalid" in str(error.value)
    assert "[env.CDH_COMFYUI_PORT]" in str(error.value)


def test_unknown_runtime_config_field_is_rejected_before_spawn(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
unknown = true
""",
    )
    calls: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        calls.append(list(argv))
        return FakeChild(0)

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
            runner=runner,
        )

    assert calls == []
    assert "[comfyui.unknown]" in str(error.value)


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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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
    assert child.returncode is None
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
    assert child.returncode is None
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
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


def test_internal_async_plan_exercises_active_state_gate_without_download(
    tmp_path: Path,
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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


def test_public_async_runtime_file_records_pending_without_sync_download(
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del plan, config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log
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
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del log
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
