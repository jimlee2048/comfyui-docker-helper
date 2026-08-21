"""Direct Bash coverage for the static SSH-login workspace profile hook."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from comfyui_docker_helper.release_artifacts import WORKSPACE_PROFILE_RESOURCE

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="Bash is required for the workspace profile hook",
)

_SSH_CONNECTION = "203.0.113.5 4444 127.0.0.1 2222"
_FALLBACK_WARNING = "Warning: cdh could not enter WORKSPACE; continuing in /root\n"


def _run_profile(
    start: Path,
    *,
    workspace: str | None,
    ssh_connection: str | None,
    fallback_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in ("BASH_ENV", "CDPATH", "ENV", "SSH_CONNECTION", "WORKSPACE"):
        environment.pop(name, None)
    if workspace is not None:
        environment["WORKSPACE"] = workspace
    if ssh_connection is not None:
        environment["SSH_CONNECTION"] = ssh_connection
    # Keep the fixed /root fallback observable when the test user cannot enter /root.
    fallback = ""
    if fallback_root is not None:
        fallback = (
            "cd() { "
            'if [[ "$1" == "/root" ]]; then '
            f"builtin cd {shlex.quote(str(fallback_root))}; "
            'else builtin cd "$@"; fi; '
            "}; "
        )
    command = f"{fallback}source {shlex.quote(str(WORKSPACE_PROFILE_RESOURCE))}; pwd -P"
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=start,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def test_profile_hook_is_inert_without_ssh_metadata(tmp_path: Path) -> None:
    start = tmp_path / "starting directory"
    start.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _run_profile(
        start,
        workspace=str(workspace),
        ssh_connection=None,
    )

    assert result.returncode == 0
    assert result.stdout == f"{start}\n"
    assert result.stderr == ""


def test_profile_hook_enters_inherited_workspace_with_spaces(tmp_path: Path) -> None:
    start = tmp_path / "starting"
    start.mkdir()
    workspace = tmp_path / "project workspace with spaces"
    workspace.mkdir()

    result = _run_profile(
        start,
        workspace=str(workspace),
        ssh_connection=_SSH_CONNECTION,
    )

    assert result.returncode == 0
    assert result.stdout == f"{workspace}\n"
    assert result.stderr == ""


def test_profile_hook_falls_back_without_leaking_unavailable_workspace(
    tmp_path: Path,
) -> None:
    start = tmp_path / "starting"
    start.mkdir()
    fallback_root = tmp_path / "root"
    fallback_root.mkdir()
    unavailable = tmp_path / "missing workspace with spaces"

    result = _run_profile(
        start,
        workspace=str(unavailable),
        ssh_connection=_SSH_CONNECTION,
        fallback_root=fallback_root,
    )

    assert result.returncode == 0
    assert result.stdout == f"{fallback_root}\n"
    assert result.stderr == _FALLBACK_WARNING
    assert str(unavailable) not in result.stderr
