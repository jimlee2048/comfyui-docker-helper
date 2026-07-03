"""Tests for the container runtime entrypoint service."""

import subprocess
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


def test_default_argv_uses_runtime_defaults_and_venv_python(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    calls: list[tuple[list[str], str, dict[str, str]]] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((list(argv), cwd, dict(env)))
        return subprocess.CompletedProcess(list(argv), 0)

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

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0)

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


@pytest.mark.parametrize("returncode", [0, 17])
def test_child_exit_code_is_returned(tmp_path: Path, returncode: int) -> None:
    runtime = _runtime(tmp_path)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(list(argv), returncode)

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={},
            runner=runner,
        )
        == returncode
    )


def test_runtime_validation_failure_happens_before_spawn(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    calls: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0)

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
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0)

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
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0)

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
