"""Offline integration with the installed Git credential plumbing."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from comfyui_docker_helper.config.service import load_validate_config_result
from comfyui_docker_helper.host.secret_session import HostSecretSession


def test_cdh_helper_resets_ambient_helpers_and_selects_by_http_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("installed Git is required for credential plumbing integration")

    config = tmp_path / "config.toml"
    config.write_text(
        """
[compute_platform]
type = "cuda"
[compute_platform.cuda]
version = "13.0.3"
[pytorch]
version = "2.12.1"
[comfyui]
version = "0.11.0"
install_manager = false

[secrets.root_token]
env = "CDH_TEST_ROOT_TOKEN"
[secrets.team_token]
env = "CDH_TEST_TEAM_TOKEN"

[[cdh.git.credentials]]
match = "https://example.test/"
username = "root-user"
password = { secret = "root_token" }

[[cdh.git.credentials]]
match = "https://example.test/team/"
username = "team-user"
password = { secret = "team_token" }
"""
    )
    ambient_marker = tmp_path / "ambient-helper-ran"
    ambient_helper = tmp_path / "ambient-helper"
    ambient_helper.write_text(
        "#!/bin/sh\n"
        f"printf invoked > {shlex.quote(os.fspath(ambient_marker))}\n"
        "printf 'username=ambient\\npassword=ambient\\n'\n"
    )
    ambient_helper.chmod(0o700)
    monkeypatch.delenv("CDH_TEST_ROOT_TOKEN", raising=False)
    monkeypatch.setenv("CDH_TEST_TEAM_TOKEN", "team-secret")
    result = load_validate_config_result(config)

    with HostSecretSession.from_configuration(result) as session:
        binding = session.git_binding()
        assert binding is not None
        environment = {
            **os.environ,
            **binding.environment,
            "HOME": os.fspath(tmp_path / "isolated-home"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
            "SSH_ASKPASS": "",
        }
        command = (
            git,
            "-c",
            f"credential.helper=!{ambient_helper}",
            *binding.config_args,
            "credential",
            "fill",
        )

        selected = subprocess.run(
            command,
            input=(b"protocol=https\nhost=example.test\npath=team/repository.git\n\n"),
            capture_output=True,
            check=False,
            env=environment,
        )
        unmatched = subprocess.run(
            command,
            input=b"protocol=https\nhost=other.test\npath=team/repository.git\n\n",
            capture_output=True,
            check=False,
            env=environment,
        )
        mismatched = subprocess.run(
            command,
            input=(
                b"protocol=https\nhost=example.test\n"
                b"path=team/repository.git\nusername=root-user\n\n"
            ),
            capture_output=True,
            check=False,
            env=environment,
        )

    assert selected.returncode == 0
    assert b"username=team-user\n" in selected.stdout
    assert b"password=team-secret\n" in selected.stdout
    assert selected.stderr == b""
    assert unmatched.returncode != 0
    assert mismatched.returncode != 0
    assert b"team-secret" not in unmatched.stdout + unmatched.stderr
    assert b"team-secret" not in mismatched.stdout + mismatched.stderr
    assert not ambient_marker.exists()
