"""Runtime config precedence coverage for container runtime startup."""

from __future__ import annotations

import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from comfyui_docker_helper.config import (
    RuntimeConfig,
    RuntimeConfigurationError,
    load_runtime_config,
)
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeDownloadStateObserver,
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
)
from comfyui_docker_helper.container.runtime_serve import (
    RuntimeExecutionError,
    run_runtime_generation_once,
)


@dataclass(frozen=True, slots=True)
class SpawnCall:
    argv: list[str]
    cwd: str
    env: dict[str, str]
    shell: bool


class FakeChild:
    """Minimal child process for runtime integration tests."""

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


def test_runtime_downloader_credentials_are_independent_and_value_lazy(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_downloader = "httpx"

[[cdh.downloader.credentials]]
match = "https://example.test/private/"
type = "bearer"
token = { secret = "runtime_read" }

[secrets.runtime_read]
file = "/run/secrets/runtime-token"

[[files]]
type = "http"
url = "https://example.test/private/model.bin?download=1"
target_dir = "models"
filename = "model.bin"
""",
    )

    result = load_runtime_config(
        baked_config_path=_missing_baked_config(tmp_path),
        mounted_config_path=mounted,
        environ={},
    )

    assert result.config.secrets["runtime_read"].file == "/run/secrets/runtime-token"


def test_runtime_authenticated_aria2_fails_with_security_remediation(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "runtime.toml",
        """
[[cdh.downloader.credentials]]
match = "https://example.test/private/"
type = "bearer"
token = { secret = "runtime_read" }

[secrets.runtime_read]
env = "RUNTIME_TOKEN"

[[files]]
type = "http"
url = "https://example.test/private/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=_missing_baked_config(tmp_path),
            mounted_config_path=mounted,
            environ={},
        )

    diagnostic = next(
        item
        for item in raised.value.diagnostics
        if item.code == "downloader_credential.httpx_required"
    )
    assert diagnostic.path == ("files", 0, "downloader")
    assert "security" in diagnostic.message.lower()
    assert diagnostic.hint is not None and "httpx" in diagnostic.hint


def test_runtime_secret_file_requires_absolute_container_path(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "runtime.toml",
        """
[[cdh.downloader.credentials]]
match = "https://example.test/private/"
type = "bearer"
token = { secret = "runtime_read" }

[secrets.runtime_read]
file = "relative/token"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=_missing_baked_config(tmp_path),
            mounted_config_path=mounted,
            environ={},
        )

    assert any(item.code == "secret.invalid_file" for item in raised.value.diagnostics)


# Runtime config startup coverage pins the default argv/env contract and the
# baked-to-mounted config precedence used by container runtime startup.
def test_runtime_starts_with_defaults_without_runtime_config(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []
    state_path = tmp_path / "state" / "state.json"

    exit_code = run_runtime_generation_once(
        runtime=runtime,
        runtime_state_path=state_path,
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
    assert not state_path.parent.exists()


# Existing state remains an admission boundary even when the effective desired
# file list is empty, and invalid bytes cannot reach child startup or mutation.
def test_empty_file_plan_rejects_invalid_existing_runtime_state(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    state_path = _write(tmp_path / "state.json", "{not-json")
    calls: list[SpawnCall] = []

    with pytest.raises(
        RuntimeExecutionError,
        match=r"runtime state failed: runtime state is invalid; remove .* and restart",
    ):
        run_runtime_generation_once(
            runtime=runtime,
            runtime_state_path=state_path,
            baked_config_path=_missing_baked_config(tmp_path),
            mounted_config_path=_missing_mounted_config(tmp_path),
            baked_hooks_path=_missing_baked_hooks(tmp_path),
            mounted_hooks_path=_missing_mounted_hooks(tmp_path),
            environ={"PATH": "/usr/bin"},
            runner=_recording_runner(calls),
        )

    assert calls == []
    assert state_path.read_text(encoding="utf-8") == "{not-json"


def test_baked_runtime_config_feeds_runtime_argv(tmp_path: Path) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[comfyui]
listen = "127.0.0.10"
port = 8191
extra_args = ["--cpu", "--lowvram"]
""",
    )
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []

    exit_code = run_runtime_generation_once(
        runtime=runtime,
        baked_config_path=baked,
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


def test_mounted_runtime_config_overrides_baked_config(tmp_path: Path) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[comfyui]
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

    exit_code = run_runtime_generation_once(
        runtime=runtime,
        baked_config_path=baked,
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


# Environment override coverage keeps CLI-facing knobs wired through both
# ComfyUI argv generation and runtime downloader configuration.
def test_environment_overrides_mounted_and_baked_runtime_config(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[cdh]
default_downloader = "aria2"
download_max_attempts = 4
download_failure_policy = "continue"
shutdown_timeout = 20

[comfyui]
listen = "127.0.0.10"
port = 8191
extra_args = ["--cpu"]

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models/checkpoints"
filename = "model.bin"
download_mode = "sync"
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_downloader = "aria2"
download_max_attempts = 5
download_failure_policy = "fail"
shutdown_timeout = -1

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
        state_observer: RuntimeDownloadStateObserver | None = None,
        **_kwargs: object,
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del log, state_observer
        events.append("download")
        downloader_plans.append(plan)
        downloader_configs.append(config)
        return ()

    exit_code = run_runtime_generation_once(
        runtime=runtime,
        runtime_state_path=tmp_path / "state.json",
        baked_config_path=baked,
        mounted_config_path=mounted,
        baked_hooks_path=_missing_baked_hooks(tmp_path),
        mounted_hooks_path=_missing_mounted_hooks(tmp_path),
        environ={
            "PATH": "/usr/bin",
            "TZ": "Asia/Shanghai",
            "CDH_COMFYUI_LISTEN": "127.0.0.30",
            "CDH_COMFYUI_PORT": "8391",
            "CDH_COMFYUI_EXTRA_ARGS": '--preview-method "latent2rgb" --fast',
            "CDH_DEFAULT_DOWNLOADER": "httpx",
            "CDH_DEFAULT_DOWNLOAD_MODE": "async",
            "CDH_DOWNLOAD_MAX_ATTEMPTS": "6",
            "CDH_DOWNLOAD_FAILURE_POLICY": "continue",
            "CDH_SHUTDOWN_TIMEOUT": "55.5",
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
    assert downloader_configs[0].cdh.default_download_mode == "async"
    assert downloader_configs[0].cdh.download_max_attempts == 6
    assert downloader_configs[0].cdh.download_failure_policy == "continue"
    assert downloader_configs[0].cdh.shutdown_timeout == 55.5
    assert calls[0].env["TZ"] == "Asia/Shanghai"
    assert downloader_plans[0].items[0].target == (
        runtime.comfyui_path / "models" / "checkpoints" / "model.bin"
    )


# Runtime-only validation coverage documents tolerated host-only fields and
# blocks invalid mounted/env/unknown fields before spawning ComfyUI.
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

    exit_code = run_runtime_generation_once(
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
    assert "[system.workspace]" in captured.err
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

    with pytest.raises(RuntimeExecutionError) as error:
        run_runtime_generation_once(
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


@pytest.mark.parametrize(
    ("environ", "expected_path"),
    [
        (
            {"CDH_DOWNLOAD_MAX_ATTEMPTS": "0"},
            "[env.CDH_DOWNLOAD_MAX_ATTEMPTS]",
        ),
        (
            {"CDH_DOWNLOAD_FAILURE_POLICY": "skip"},
            "[cdh.download_failure_policy]",
        ),
    ],
)
def test_invalid_env_runtime_config_fails_before_spawn(
    tmp_path: Path,
    environ: dict[str, str],
    expected_path: str,
) -> None:
    runtime = _runtime(tmp_path)
    calls: list[SpawnCall] = []

    with pytest.raises(RuntimeExecutionError) as error:
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=_missing_baked_config(tmp_path),
            mounted_config_path=_missing_mounted_config(tmp_path),
            baked_hooks_path=_missing_baked_hooks(tmp_path),
            mounted_hooks_path=_missing_mounted_hooks(tmp_path),
            environ={"PATH": "/usr/bin", **environ},
            runner=_recording_runner(calls),
        )

    assert calls == []
    assert "runtime configuration is invalid" in str(error.value)
    assert expected_path in str(error.value)


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

    with pytest.raises(RuntimeExecutionError) as error:
        run_runtime_generation_once(
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
