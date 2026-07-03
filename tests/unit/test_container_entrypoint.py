"""Tests for the container runtime entrypoint service."""

import signal
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from comfyui_docker_helper.container.entrypoint import EntrypointError, run_entrypoint
from comfyui_docker_helper.container.runners import ContainerRuntime


def _runtime(tmp_path: Path) -> ContainerRuntime:
    return ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )


def _write(path: Path, document: str) -> Path:
    path.write_text(document, encoding="utf-8")
    return path


class FakeChild:
    """Minimal fake Popen-compatible child process."""

    def __init__(self, returncode: int) -> None:
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self.signals: list[signal.Signals] = []

    def wait(self) -> int:
        self.returncode = self._wait_returncode
        return self._wait_returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)


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


def test_runtime_files_are_rejected_before_spawn(tmp_path: Path) -> None:
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
    assert "runtime.files_unsupported" in str(error.value)
