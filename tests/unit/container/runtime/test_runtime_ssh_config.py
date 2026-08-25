"""Runtime sshd configuration serialization coverage."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from tests.unit.container.runtime.runtime_ssh_support import VALID_SSH_KEY

import comfyui_docker_helper.container.runtime.ssh.config as ssh_module
from comfyui_docker_helper.config import RuntimeSystemSshConfig
from comfyui_docker_helper.container.runtime.ssh.config import (
    RootSshCredentialPreparationStatus,
    SshEnvironmentProjectionError,
    build_sshd_argv,
    serialize_sshd_config,
    serialize_sshd_set_env,
)


# Environment projection preserves representable bytes and fails content-safe.
def test_serialize_sshd_set_env_preserves_exact_representable_bytes() -> None:
    environment = {
        b"SSH_CUSTOM": b"preserved",
        b"Z_NON_UTF8": b"\xff",
        b"TERM": b"stale-container-terminal",
        b"D_QUOTE": b'left"right',
        b"A_EMPTY": b"",
        b"F_HASH": b"# literal",
        b"G_EQUALS": b"left=middle=right",
        b"E_BACKSLASH": b"left\\right",
        b"I_\xff_NON_UTF8_NAME": b"preserved",
        b"SSH_AUTH_SOCK": b"/stale/agent.sock",
        b"C_TAB": b"left\tright",
        b"B_SPACE": b" left right ",
        b"H-NON-SHELL-NAME": b"accepted",
    }

    serialized = serialize_sshd_set_env(environment)

    assert serialized == (
        b'SetEnv "A_EMPTY=" "B_SPACE= left right " "C_TAB=left\tright" '
        b'"D_QUOTE=left\\"right" "E_BACKSLASH=left\\\\right" '
        b'"F_HASH=# literal" "G_EQUALS=left=middle=right" '
        b'"H-NON-SHELL-NAME=accepted" "I_\xff_NON_UTF8_NAME=preserved" '
        b'"SSH_CUSTOM=preserved" '
        b'"Z_NON_UTF8=\xff"\n'
    )
    assert serialized.count(b"SetEnv ") == 1
    assert b"stale-container-terminal" not in serialized
    assert b"/stale/agent.sock" not in serialized


def test_serialize_sshd_set_env_omits_only_negotiated_session_names() -> None:
    projected = {
        b"SSH_PASSWORD": b"password-value",
        b"SSH_CLIENT": b"client-value",
        b"SSH_CONNECTION": b"connection-value",
        b"SSH_ORIGINAL_COMMAND": b"command-value",
        b"SSH_TTY": b"tty-value",
        b"USER": b"user-value",
        b"HOME": b"home-value",
        b"SHELL": b"shell-value",
        b"PATH": b"path-value",
    }
    environment = {
        **projected,
        b"TERM": b"stale-container-terminal",
        b"SSH_AUTH_SOCK": b"/stale/agent.sock",
    }

    serialized = serialize_sshd_set_env(environment)

    for name, value in projected.items():
        assert b'"' + name + b"=" + value + b'"' in serialized
    assert b"TERM=" not in serialized
    assert b"SSH_AUTH_SOCK=" not in serialized


def test_serialize_sshd_set_env_empty_projection_has_no_directive() -> None:
    assert serialize_sshd_set_env({}) == b""
    assert (
        serialize_sshd_set_env(
            {
                b"TERM": b"stale-container-terminal",
                b"SSH_AUTH_SOCK": b"/stale/agent.sock",
            }
        )
        == b""
    )


@pytest.mark.parametrize(
    ("environment", "leaked_fragment"),
    [
        pytest.param(
            {b"BAD\x00NAME": b"value"},
            "BAD",
            id="nul-name",
        ),
        pytest.param(
            {b"NAME": b"private-nul\x00sentinel"},
            "private-nul",
            id="nul-value",
        ),
        pytest.param(
            {b"BAD\nNAME": b"value"},
            "BAD",
            id="lf-name",
        ),
        pytest.param(
            {b"NAME": b"private-lf\nsentinel"},
            "private-lf",
            id="lf-value",
        ),
        pytest.param(
            {b"BAD=NAME": b"value"},
            "BAD",
            id="equals-name",
        ),
    ],
)
def test_serialize_sshd_set_env_rejects_only_unrepresentable_bytes_without_leak(
    environment: dict[bytes, bytes],
    leaked_fragment: str,
) -> None:
    with pytest.raises(SshEnvironmentProjectionError) as raised:
        serialize_sshd_set_env(environment)

    assert str(raised.value) == "SSH environment cannot be projected"
    assert leaked_fragment not in str(raised.value)


def test_serialize_sshd_set_env_accepts_exact_openssh_name_capacity() -> None:
    assert len(ssh_module._SSH_SESSION_ENVIRONMENT_NAMES) == 11
    environment = {
        f"CDH_CAPACITY_{index:04d}".encode(): b"value" for index in range(988)
    }

    serialized = serialize_sshd_set_env(environment)

    assert serialized.startswith(b'SetEnv "CDH_CAPACITY_0000=value"')
    assert serialized.endswith(b'"CDH_CAPACITY_0987=value"\n')


def test_serialize_sshd_set_env_rejects_above_openssh_name_capacity() -> None:
    environment = {
        f"CDH_CAPACITY_{index:04d}".encode(): b"value" for index in range(989)
    }

    with pytest.raises(
        SshEnvironmentProjectionError,
        match=r"^SSH environment cannot be projected$",
    ):
        serialize_sshd_set_env(environment)


def test_serialize_sshd_set_env_is_not_limited_by_single_argv_size() -> None:
    value = b"x" * (128 * 1024 + 1)
    expected = b'SetEnv "LARGE_VALUE=' + value + b'"\n'

    serialized = serialize_sshd_set_env({b"LARGE_VALUE": value})

    assert len(serialized) == len(expected)
    assert hashlib.sha256(serialized).digest() == hashlib.sha256(expected).digest()


# sshd config and argv tests lock down one complete configuration authority.
def test_serialize_sshd_config_contains_service_policy_and_environment() -> None:
    status = RootSshCredentialPreparationStatus(
        ssh_enabled=True,
        public_key_count=1,
        password_configured=False,
    )

    serialized = serialize_sshd_config(
        RuntimeSystemSshConfig(enable=True, port=2022, pub_keys=[VALID_SSH_KEY]),
        status,
        {b"TEST_SENTINEL": b"environment-value"},
    )

    assert serialized == (
        b"Port 2022\n"
        b"PermitRootLogin yes\n"
        b"PasswordAuthentication no\n"
        b"KbdInteractiveAuthentication no\n"
        b"PubkeyAuthentication yes\n"
        b"AuthorizedKeysFile /root/.ssh/authorized_keys\n"
        b'SetEnv "TEST_SENTINEL=environment-value"\n'
    )
    assert VALID_SSH_KEY.encode() not in serialized


def test_build_sshd_argv_uses_only_owned_config_and_process_flags() -> None:
    config_path = Path("/run/cdh/sshd_config.unique")

    assert build_sshd_argv(config_path) == [
        "/usr/sbin/sshd",
        "-f",
        os.fspath(config_path),
        "-D",
        "-e",
    ]
