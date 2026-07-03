"""Tests for the container runtime entrypoint service."""

import signal
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from comfyui_docker_helper.config import Diagnostic, RuntimeConfig
from comfyui_docker_helper.container.entrypoint import EntrypointError, run_entrypoint
from comfyui_docker_helper.container.readiness import ReadinessError
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeFileDownloadError,
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
)
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    RuntimeHookPlan,
    RuntimeHookResult,
)


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

    def wait(self) -> int:
        self.returncode = self._wait_returncode
        return self._wait_returncode

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.terminated = True


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
    assert restored == [signal.SIGTERM, signal.SIGINT]


@pytest.mark.parametrize("forwarded", [signal.SIGTERM, signal.SIGINT])
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

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        start_new_session: bool = False,
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log
        assert phase == "stop"
        assert start_new_session is True
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
        runtime_hook_runner=runtime_hook_runner,
    ) == 128 + int(forwarded)

    assert events == [
        "stop:10-baked.sh,10-mounted.py",
        f"signal:{forwarded.name}",
        "wait",
    ]
    assert child.signals == [forwarded]
    assert restored == [signal.SIGTERM, signal.SIGINT]


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

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        start_new_session: bool = False,
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log, start_new_session
        calls.append(phase)
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

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        start_new_session: bool = False,
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log
        assert phase == "stop"
        assert start_new_session is True
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
            runtime_hook_runner=runtime_hook_runner,
        )
        == 143
    )

    captured = capsys.readouterr()
    assert events == ["stop", "signal:SIGTERM", "wait"]
    assert child.signals == [signal.SIGTERM]
    assert "Runtime stop hook failed:" in captured.err
    assert "[hooks.mounted.stop.10-fail.sh]" in captured.err
    assert "runtime_hook.execution_failed" in captured.err


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


def test_raw_launch_args_are_rejected_before_spawn(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
launch_args = ["--cpu"]
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
    assert "[comfyui.launch_args]" in str(error.value)


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
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log
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
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, phase, runtime, env, log
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
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, phase, runtime, env, log
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
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, phase, runtime, env, log
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
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log
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


def test_post_start_hooks_run_once_after_readiness_internal_retries(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-post.sh")
    readiness_calls: list[list[str]] = []
    post_start_calls: list[str] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        return FakeChild(0)

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        assert port == 8188
        assert child.poll() is None
        readiness_calls.append(["failed-poll", "failed-poll", "ready"])

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log
        post_start_calls.append(phase)
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
        == 0
    )

    assert readiness_calls == [["failed-poll", "failed-poll", "ready"]]
    assert post_start_calls == ["post-start"]


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
) -> None:
    runtime = _runtime(tmp_path)
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
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, phase, runtime, env, log
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


def test_post_start_hook_failure_terminates_child_and_prevents_normal_wait(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start.d", "10-fail.sh")
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
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log
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
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log
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
        )

    assert spawn_calls == []
    assert "runtime download failed" in str(error.value)
    assert "[files.0.target]" in str(error.value)
    assert "runtime_file.test_failure" in str(error.value)


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
        )
        == 0
    )

    assert default_downloader == ["httpx"]
    assert seen == [(None, "sync"), ("aria2", "sync")]
