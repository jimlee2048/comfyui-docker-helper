"""Container-side subprocess and hook runner tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
    resolve_hook_path,
    run_argv,
    run_hook,
    run_hooks,
    start_argv,
)


def test_runtime_env_sets_required_container_variables() -> None:
    """Build the helper env expected by scripts and subprocesses."""
    runtime = ContainerRuntime(
        workspace=Path("/w"),
        comfyui_path=Path("/w/c"),
        virtual_env=Path("/venv"),
    )

    env = runtime.env({"PATH": "/usr/bin", "KEEP": "1"})

    assert env["WORKSPACE"] == "/w"
    assert env["COMFYUI_PATH"] == "/w/c"
    assert env["VIRTUAL_ENV"] == "/venv"
    assert env["PATH"] == "/venv/bin:/usr/bin"
    assert env["KEEP"] == "1"


def test_runtime_from_env_uses_configured_container_paths() -> None:
    """Build the CLI runtime from Docker-managed environment variables."""
    runtime = ContainerRuntime.from_env(
        {
            "WORKSPACE": "/srv/work",
            "COMFYUI_PATH": "/opt/comfy",
            "VIRTUAL_ENV": "/venv",
        }
    )

    assert runtime.workspace == Path("/srv/work")
    assert runtime.comfyui_path == Path("/opt/comfy")
    assert runtime.virtual_env == Path("/venv")


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"WORKSPACE": "/srv/work"},
        {"COMFYUI_PATH": "/opt/comfy"},
    ],
)
def test_runtime_from_env_fails_closed_without_required_paths(
    env: dict[str, str],
) -> None:
    """Refuse container CLI defaults when Docker-managed paths are missing."""
    with pytest.raises(ContainerCommandError, match="missing required"):
        ContainerRuntime.from_env(env)


def test_run_argv_uses_cwd_env_and_inherited_output(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Run argv without shell while preserving stdout/stderr."""
    output = tmp_path / "probe.txt"
    env = {
        "OUT": str(output),
        "COMFYUI_PATH": "/workspace/ComfyUI",
    }

    run_argv(
        [
            sys.executable,
            "-c",
            (
                "import os, pathlib, sys; "
                "print('stdout-ok'); "
                "print('stderr-ok', file=sys.stderr); "
                "pathlib.Path(os.environ['OUT']).write_text("
                "os.getcwd() + '|' + os.environ['COMFYUI_PATH'])"
            ),
        ],
        cwd=tmp_path,
        env=env,
        description="probe",
    )

    captured = capfd.readouterr()
    assert "stdout-ok" in captured.out
    assert "stderr-ok" in captured.err
    assert output.read_text(encoding="utf-8") == f"{tmp_path}|/workspace/ComfyUI"


def test_run_argv_can_request_new_process_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass start_new_session only when process-group isolation is requested."""
    calls: list[dict[str, object]] = []

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_argv(["echo", "default"], cwd=tmp_path, env={}, description="default")
    run_argv(
        ["echo", "isolated"],
        cwd=tmp_path,
        env={},
        description="isolated",
        start_new_session=True,
    )

    assert "start_new_session" not in calls[0]
    assert calls[1]["start_new_session"] is True


def test_run_argv_can_close_child_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_argv(
        ["cm-cli", "install"],
        cwd=tmp_path,
        env={},
        close_stdin=True,
    )

    assert calls[0]["stdin"] is subprocess.DEVNULL


def test_start_argv_can_request_new_process_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass start_new_session only when starting an isolated subprocess."""
    calls: list[dict[str, object]] = []

    def fake_popen(
        command: list[str],
        **kwargs: object,
    ) -> object:
        calls.append({"command": command, **kwargs})
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    start_argv(["echo", "default"], cwd=tmp_path, env={}, description="default")
    start_argv(
        ["echo", "isolated"],
        cwd=tmp_path,
        env={},
        description="isolated",
        start_new_session=True,
    )

    assert "start_new_session" not in calls[0]
    assert calls[1]["start_new_session"] is True


def test_run_argv_rejects_empty_argv(tmp_path: Path) -> None:
    """Do not allow ambiguous empty subprocess invocations."""
    with pytest.raises(ContainerCommandError, match="must not be empty"):
        run_argv([], cwd=tmp_path, env={}, description="empty")


def test_run_argv_reports_nonzero_exit(tmp_path: Path) -> None:
    """Nonzero subprocess return codes are fatal."""
    with pytest.raises(ContainerCommandError, match="failed with exit code 7") as error:
        run_argv(
            [sys.executable, "-c", "raise SystemExit(7)"],
            cwd=tmp_path,
            env={},
            description="failing command",
        )

    assert error.value.exit_code == 7


def test_run_argv_reports_missing_executable(tmp_path: Path) -> None:
    """Missing executables become user-facing helper errors."""
    with pytest.raises(ContainerCommandError, match="executable not found"):
        run_argv(
            [str(tmp_path / "missing-executable")],
            cwd=tmp_path,
            env={},
            description="missing",
        )


def test_run_hook_maps_shell_and_python_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map .sh to bash and .py to the venv Python with ComfyUI as cwd."""
    scripts = tmp_path / "scripts"
    nested = scripts / "nested"
    nested.mkdir(parents=True)
    shell_hook = scripts / "before.sh"
    python_hook = nested / "after.py"
    shell_hook.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    python_hook.write_text("pass\n", encoding="utf-8")
    runtime = ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )
    calls: list[tuple[list[str], str, dict[str, str], bool]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        shell: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, cwd, env, shell))
        assert check is False
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_hook(
        "before.sh",
        scripts_dir=scripts,
        runtime=runtime,
        env={"PATH": "/usr/bin"},
    )
    run_hook(
        "nested/after.py",
        scripts_dir=scripts,
        runtime=runtime,
        env={"PATH": "/usr/bin"},
    )

    assert calls[0][0] == ["bash", str(shell_hook)]
    assert calls[1][0] == [str(runtime.python), str(python_hook)]
    assert [call[1] for call in calls] == [str(runtime.comfyui_path)] * 2
    assert [call[3] for call in calls] == [False, False]
    assert calls[0][2]["WORKSPACE"] == str(runtime.workspace)
    assert calls[0][2]["COMFYUI_PATH"] == str(runtime.comfyui_path)
    assert calls[0][2]["VIRTUAL_ENV"] == str(runtime.virtual_env)


def test_run_hooks_stops_on_first_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run hooks serially and stop immediately on failure."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("first.sh", "second.sh"):
        (scripts / name).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        shell: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, env, shell, check
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 5)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ContainerCommandError, match=r"first\.sh"):
        run_hooks(("first.sh", "second.sh"), scripts_dir=scripts)

    assert len(calls) == 1
    assert calls[0][1] == str(scripts / "first.sh")


@pytest.mark.parametrize(
    ("hook", "message"),
    [
        ("/absolute.sh", "relative"),
        ("../escape.py", "must not contain"),
        ("notes.txt", "must end"),
        ("missing.sh", "does not exist"),
    ],
)
def test_resolve_hook_path_rejects_invalid_hooks(
    tmp_path: Path,
    hook: str,
    message: str,
) -> None:
    """Repeat runtime hook checks even after host/render validation."""
    with pytest.raises(ContainerCommandError, match=message):
        resolve_hook_path(hook, scripts_dir=tmp_path)


def test_resolve_hook_path_accepts_nested_regular_file(tmp_path: Path) -> None:
    """Resolve supported hook files below scripts-dir."""
    scripts = tmp_path / "scripts"
    nested = scripts / "nested"
    nested.mkdir(parents=True)
    hook = nested / "hook.py"
    hook.write_text("pass\n", encoding="utf-8")

    assert resolve_hook_path("nested/hook.py", scripts_dir=scripts) == hook
