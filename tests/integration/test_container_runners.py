"""Container-side subprocess and hook runner tests."""

from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
    run_argv,
    run_hook,
    start_argv,
)


# Process runners preserve exact argv/environment ownership and actionable failures.
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


# Subprocess execution preserves process-session choices, streams, and diagnostics.
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


# Hook execution binds the locked bytes to an immutable inherited descriptor.
@pytest.mark.parametrize("suffix", [".sh", ".py"])
def test_run_hook_executes_exact_shell_and_python_bytes(
    tmp_path: Path, suffix: str
) -> None:
    """Execute exact locked bytes with the suffix-selected interpreter."""
    _require_linux_memfd()
    scripts = tmp_path / "scripts"
    nested = scripts / "nested"
    nested.mkdir(parents=True)
    marker = tmp_path / f"ran{suffix}"
    content = (
        b'printf "shell" > "$HOOK_MARKER"\n'
        if suffix == ".sh"
        else (
            b"import os\nfrom pathlib import Path\n"
            b'Path(os.environ["HOOK_MARKER"]).write_text("python")\n'
        )
    )
    hook = nested / f"exact{suffix}"
    hook.write_bytes(content)
    runtime = _runtime(tmp_path)

    run_hook(
        f"nested/exact{suffix}",
        expected_digest=_digest(content),
        scripts_dir=scripts,
        runtime=runtime,
        env={**os.environ, "HOOK_MARKER": str(marker)},
    )

    assert marker.read_text() == ("shell" if suffix == ".sh" else "python")


def test_run_hook_rejects_digest_mismatch_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not start mismatched hook bytes."""
    _require_linux_memfd()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    marker = tmp_path / "must-not-run"
    hook = scripts / "mismatch.sh"
    hook.write_text(f"touch {marker}\n")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("mismatched hook must not start"),
    )

    with pytest.raises(ContainerCommandError, match="digest does not match"):
        run_hook(
            "mismatch.sh",
            expected_digest=_digest(b"different\n"),
            scripts_dir=scripts,
            runtime=_runtime(tmp_path),
            env=os.environ,
        )

    assert not marker.exists()


@pytest.mark.parametrize(
    ("hook", "message"),
    [
        ("/absolute.sh", "canonical relative"),
        ("../escape.py", "canonical relative"),
        ("nested//hook.py", "canonical relative"),
        ("notes.txt", "must end"),
        ("missing.sh", "regular non-symlink"),
    ],
)
def test_run_hook_rejects_invalid_paths(
    tmp_path: Path,
    hook: str,
    message: str,
) -> None:
    """Repeat canonical path and source admission at execution time."""
    with pytest.raises(ContainerCommandError, match=message):
        run_hook(
            hook,
            expected_digest=_digest(b"unused"),
            scripts_dir=tmp_path,
            runtime=_runtime(tmp_path),
        )


@pytest.mark.parametrize("symlink_location", ["parent", "leaf"])
def test_run_hook_rejects_symlinked_component(
    tmp_path: Path, symlink_location: str
) -> None:
    """Reject symlinks in either the directory walk or leaf admission."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    target.joinpath("hook.sh").write_bytes(b"true\n")
    if symlink_location == "parent":
        scripts.joinpath("linked").symlink_to(target, target_is_directory=True)
        hook = "linked/hook.sh"
    else:
        scripts.joinpath("linked.sh").symlink_to(target / "hook.sh")
        hook = "linked.sh"

    with pytest.raises(ContainerCommandError, match="regular non-symlink"):
        run_hook(
            hook,
            expected_digest=_digest(target.joinpath("hook.sh").read_bytes()),
            scripts_dir=scripts,
            runtime=_runtime(tmp_path),
        )


def test_run_hook_rejects_symlinked_scripts_root_ancestor(tmp_path: Path) -> None:
    """Reject an exact hook when an ancestor of its scripts root is a symlink."""
    real_parent = tmp_path / "real-parent"
    scripts = real_parent / "scripts"
    scripts.mkdir(parents=True)
    marker = tmp_path / "must-not-run"
    content = f"touch {marker}\n".encode()
    scripts.joinpath("exact.sh").write_bytes(content)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ContainerCommandError, match="regular non-symlink"):
        run_hook(
            "exact.sh",
            expected_digest=_digest(content),
            scripts_dir=alias / "scripts",
            runtime=_runtime(tmp_path),
            env=os.environ,
        )

    assert not marker.exists()


def test_run_hook_executes_sealed_bytes_after_original_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the inherited script is sealed and independent of its old path."""
    _require_linux_memfd()
    import fcntl

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    trusted_marker = tmp_path / "trusted"
    malicious_marker = tmp_path / "malicious"
    trusted = f'touch "{trusted_marker}"\n'.encode()
    malicious = f'touch "{malicious_marker}"\n'.encode()
    source = scripts / "swap.sh"
    source.write_bytes(trusted)
    real_run = subprocess.run
    observed_seals = 0

    def swap_then_run(command: list[str], **kwargs: object):
        nonlocal observed_seals
        pass_fds = kwargs["pass_fds"]
        assert isinstance(pass_fds, tuple) and len(pass_fds) == 1
        sealed_fd = pass_fds[0]
        observed_seals = fcntl.fcntl(sealed_fd, getattr(fcntl, "F_GET_SEALS", 1034))
        with pytest.raises(OSError) as error:
            os.write(sealed_fd, b"mutate")
        assert error.value.errno == errno.EPERM
        source.write_bytes(malicious)
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", swap_then_run)

    run_hook(
        "swap.sh",
        expected_digest=_digest(trusted),
        scripts_dir=scripts,
        runtime=_runtime(tmp_path),
        env=os.environ,
    )

    required = (
        getattr(fcntl, "F_SEAL_WRITE", 0x0008)
        | getattr(fcntl, "F_SEAL_GROW", 0x0004)
        | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
        | getattr(fcntl, "F_SEAL_SEAL", 0x0001)
    )
    assert observed_seals & required == required
    assert trusted_marker.exists()
    assert not malicious_marker.exists()


def _runtime(tmp_path: Path) -> ContainerRuntime:
    comfyui = tmp_path / "workspace/ComfyUI"
    comfyui.mkdir(parents=True, exist_ok=True)
    return ContainerRuntime(
        workspace=comfyui.parent,
        comfyui_path=comfyui,
        virtual_env=Path(sys.executable).parent.parent,
    )


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _require_linux_memfd() -> None:
    if sys.platform != "linux":
        pytest.skip("sealed hook execution requires Linux")
