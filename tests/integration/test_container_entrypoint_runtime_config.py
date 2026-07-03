"""End-to-end runtime config coverage from host render to entrypoint startup."""

from __future__ import annotations

import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container.entrypoint import EntrypointError, run_entrypoint
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
)

HOST_CONFIG_BASE = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"
"""


@dataclass(frozen=True, slots=True)
class SpawnCall:
    argv: list[str]
    cwd: str
    env: dict[str, str]
    shell: bool


class FakeChild:
    """Minimal child process for entrypoint integration tests."""

    def __init__(self, returncode: int = 0) -> None:
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


def _runtime(tmp_path: Path) -> ContainerRuntime:
    return ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )


def _write(path: Path, document: str) -> Path:
    path.write_text(document, encoding="utf-8")
    return path


def _recording_runner(calls: list[SpawnCall]):
    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        calls.append(SpawnCall(argv=list(argv), cwd=cwd, env=dict(env), shell=shell))
        return FakeChild()

    return runner


def _render_host_context(
    cli_runner: CliRunner,
    tmp_path: Path,
    document: str,
) -> Path:
    config = _write(tmp_path / "host.toml", document)
    context = tmp_path / "context"

    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(context)],
    )

    assert result.exit_code == 0, result.output
    assert (context / "runtime" / "config.toml").is_file()
    return context


def _expected_argv(
    runtime: ContainerRuntime,
    *,
    listen: str,
    port: int,
    extra_args: Sequence[str] = (),
) -> list[str]:
    return [
        str(runtime.python),
        str(runtime.comfyui_path / "main.py"),
        "--listen",
        listen,
        "--port",
        str(port),
        "--disable-auto-launch",
        *extra_args,
    ]


def _missing_baked_config(tmp_path: Path) -> Path:
    return tmp_path / "missing-baked-runtime.toml"


def _missing_mounted_config(tmp_path: Path) -> Path:
    return tmp_path / "missing-mounted-runtime.toml"


def _missing_baked_hooks(tmp_path: Path) -> Path:
    return tmp_path / "missing-baked-hooks"


def _missing_mounted_hooks(tmp_path: Path) -> Path:
    return tmp_path / "missing-mounted-hooks"


def test_entrypoint_starts_with_defaults_without_runtime_config(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []

    exit_code = run_entrypoint(
        runtime=runtime,
        baked_config_path=_missing_baked_config(tmp_path),
        mounted_config_path=_missing_mounted_config(tmp_path),
        baked_hooks_path=_missing_baked_hooks(tmp_path),
        mounted_hooks_path=_missing_mounted_hooks(tmp_path),
        environ={"PATH": "/usr/bin"},
        runner=_recording_runner(calls),
    )

    assert exit_code == 0
    assert calls == [
        SpawnCall(
            argv=_expected_argv(runtime, listen="0.0.0.0", port=8188),
            cwd=str(runtime.comfyui_path),
            env={
                "PATH": f"{runtime.virtual_env / 'bin'}:/usr/bin",
                "WORKSPACE": str(runtime.workspace),
                "COMFYUI_PATH": str(runtime.comfyui_path),
                "VIRTUAL_ENV": str(runtime.virtual_env),
            },
            shell=False,
        )
    ]


def test_host_rendered_baked_runtime_config_feeds_entrypoint_argv(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    context = _render_host_context(
        cli_runner,
        tmp_path,
        HOST_CONFIG_BASE
        + """
[comfyui]
version = "latest"
listen = "127.0.0.10"
port = 8191
extra_args = ["--cpu", "--lowvram"]
""",
    )
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []

    exit_code = run_entrypoint(
        runtime=runtime,
        baked_config_path=context / "runtime" / "config.toml",
        mounted_config_path=_missing_mounted_config(tmp_path),
        baked_hooks_path=_missing_baked_hooks(tmp_path),
        mounted_hooks_path=_missing_mounted_hooks(tmp_path),
        environ={"PATH": "/usr/bin"},
        runner=_recording_runner(calls),
    )

    assert exit_code == 0
    assert [call.argv for call in calls] == [
        _expected_argv(
            runtime,
            listen="127.0.0.10",
            port=8191,
            extra_args=("--cpu", "--lowvram"),
        )
    ]


def test_mounted_runtime_config_overrides_host_rendered_baked_config(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    context = _render_host_context(
        cli_runner,
        tmp_path,
        HOST_CONFIG_BASE
        + """
[comfyui]
version = "latest"
listen = "127.0.0.10"
port = 8191
extra_args = ["--cpu"]
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
listen = "127.0.0.20"
port = 8291
extra_args = ["--preview-method", "auto"]
""",
    )
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []

    exit_code = run_entrypoint(
        runtime=runtime,
        baked_config_path=context / "runtime" / "config.toml",
        mounted_config_path=mounted,
        baked_hooks_path=_missing_baked_hooks(tmp_path),
        mounted_hooks_path=_missing_mounted_hooks(tmp_path),
        environ={"PATH": "/usr/bin"},
        runner=_recording_runner(calls),
    )

    assert exit_code == 0
    assert [call.argv for call in calls] == [
        _expected_argv(
            runtime,
            listen="127.0.0.20",
            port=8291,
            extra_args=("--preview-method", "auto"),
        )
    ]


def test_environment_overrides_mounted_and_baked_runtime_config(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    context = _render_host_context(
        cli_runner,
        tmp_path,
        HOST_CONFIG_BASE
        + """
[cdh]
default_downloader = "aria2"

[comfyui]
version = "latest"
listen = "127.0.0.10"
port = 8191
extra_args = ["--cpu"]

[[files]]
url = "https://example.com/model.bin"
dir = "models/checkpoints"
filename = "model.bin"
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_downloader = "aria2"

[comfyui]
listen = "127.0.0.20"
port = 8291
extra_args = ["--preview-method", "auto"]
""",
    )
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []
    downloader_configs: list[RuntimeConfig] = []
    downloader_plans: list[RuntimeFilePlan] = []
    events: list[str] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        events.append("spawn")
        calls.append(SpawnCall(argv=list(argv), cwd=cwd, env=dict(env), shell=shell))
        return FakeChild()

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        log: Logger,
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del log
        events.append("download")
        downloader_plans.append(plan)
        downloader_configs.append(config)
        return ()

    exit_code = run_entrypoint(
        runtime=runtime,
        baked_config_path=context / "runtime" / "config.toml",
        mounted_config_path=mounted,
        baked_hooks_path=_missing_baked_hooks(tmp_path),
        mounted_hooks_path=_missing_mounted_hooks(tmp_path),
        environ={
            "PATH": "/usr/bin",
            "CDH_COMFYUI_LISTEN": "127.0.0.30",
            "CDH_COMFYUI_PORT": "8391",
            "CDH_COMFYUI_EXTRA_ARGS": '--preview-method "latent2rgb" --fast',
            "CDH_DEFAULT_DOWNLOADER": "httpx",
            "CDH_DEFAULT_DOWNLOAD_MODE": "sync",
        },
        runner=runner,
        runtime_downloader=runtime_downloader,
    )

    assert exit_code == 0
    assert events == ["download", "spawn"]
    assert [call.argv for call in calls] == [
        _expected_argv(
            runtime,
            listen="127.0.0.30",
            port=8391,
            extra_args=("--preview-method", "latent2rgb", "--fast"),
        )
    ]
    assert downloader_configs[0].cdh.default_downloader == "httpx"
    assert downloader_configs[0].cdh.default_download_mode == "sync"
    assert downloader_plans[0].items[0].target == (
        runtime.comfyui_path / "models" / "checkpoints" / "model.bin"
    )


def test_runtime_cross_context_fields_warn_and_do_not_block_startup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system]
workspace = "/ignored"

[compute_platform]
type = "cuda"

[comfyui]
version = "latest"
listen = "127.0.0.40"
""",
    )
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []

    exit_code = run_entrypoint(
        runtime=runtime,
        baked_config_path=_missing_baked_config(tmp_path),
        mounted_config_path=mounted,
        baked_hooks_path=_missing_baked_hooks(tmp_path),
        mounted_hooks_path=_missing_mounted_hooks(tmp_path),
        environ={"PATH": "/usr/bin"},
        runner=_recording_runner(calls),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert [call.argv for call in calls] == [
        _expected_argv(runtime, listen="127.0.0.40", port=8188)
    ]
    assert "Runtime configuration warnings:" in captured.err
    assert "[system]" in captured.err
    assert "[compute_platform]" in captured.err
    assert "[comfyui.version]" in captured.err
    assert "runtime.host_only_ignored" in captured.err


def test_invalid_runtime_config_fails_before_spawn(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
port = 0
""",
    )
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_baked_config(tmp_path),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_baked_hooks(tmp_path),
            mounted_hooks_path=_missing_mounted_hooks(tmp_path),
            environ={"PATH": "/usr/bin"},
            runner=_recording_runner(calls),
        )

    assert calls == []
    assert "runtime configuration is invalid" in str(error.value)
    assert "[comfyui.port]" in str(error.value)


def test_unknown_runtime_config_fields_fail_before_spawn(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[readiness]
enabled = true
""",
    )
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []

    with pytest.raises(EntrypointError) as error:
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_baked_config(tmp_path),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_baked_hooks(tmp_path),
            mounted_hooks_path=_missing_mounted_hooks(tmp_path),
            environ={"PATH": "/usr/bin"},
            runner=_recording_runner(calls),
        )

    assert calls == []
    assert "runtime configuration is invalid" in str(error.value)
    assert "[readiness]" in str(error.value)
