"""Runtime environment projection and parsing contracts."""

from pathlib import Path

import pytest

from comfyui_docker_helper.config import (
    RuntimeConfigurationError,
    load_runtime_config,
)

VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "test@example"
)


def _write(path: Path, document: str) -> Path:
    path.write_text(document, encoding="utf-8")
    return path


def _identities(error: RuntimeConfigurationError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


def test_env_overrides_mounted_and_baked_runtime_config(tmp_path: Path) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[comfyui]
listen = "127.0.0.1"
port = 8190
extra_args = ["--cpu"]

[cdh]
default_downloader = "httpx"
default_download_mode = "sync"
download_max_attempts = 4
download_failure_policy = "continue"
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
listen = "0.0.0.0"
port = 8288
extra_args = ["--preview-method", "auto"]

[cdh]
default_downloader = "aria2"
default_download_mode = "sync"
download_max_attempts = 5
download_failure_policy = "fail"
""",
    )

    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
        environ={
            "CDH_COMFYUI_LISTEN": "192.0.2.10",
            "CDH_COMFYUI_PORT": "8388",
            "CDH_COMFYUI_EXTRA_ARGS": '--preview-method "latent2rgb" --cpu',
            "CDH_DEFAULT_DOWNLOADER": "httpx",
            "CDH_DEFAULT_DOWNLOAD_MODE": "async",
            "CDH_DOWNLOAD_MAX_ATTEMPTS": "6",
            "CDH_DOWNLOAD_FAILURE_POLICY": "continue",
        },
    )

    assert result.config.comfyui.listen == "192.0.2.10"
    assert result.config.comfyui.port == 8388
    assert result.config.comfyui.extra_args == [
        "--preview-method",
        "latent2rgb",
        "--cpu",
    ]
    assert result.config.cdh.default_downloader == "httpx"
    assert result.config.cdh.default_download_mode == "async"
    assert result.config.cdh.download_max_attempts == 6
    assert result.config.cdh.download_failure_policy == "continue"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" true ", True),
        ("1", True),
        ("YES", True),
        ("on", True),
        (" false ", False),
        ("0", False),
        ("No", False),
        ("OFF", False),
    ],
)
def test_ssh_enable_env_parses_supported_booleans(
    tmp_path: Path,
    value: str,
    expected: bool,
) -> None:
    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={"SSH_ENABLE": value},
    )

    assert result.config.system.ssh.enable is expected


@pytest.mark.parametrize(
    ("value", "expected"), [(" 2222 ", 2222), ("1", 1), ("65535", 65535)]
)
def test_ssh_port_env_trims_and_validates_range(
    tmp_path: Path,
    value: str,
    expected: int,
) -> None:
    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={"SSH_PORT": value},
    )

    assert result.config.system.ssh.port == expected


def test_ssh_env_overrides_and_pub_key_append_after_config_merge(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        f"""
[system.ssh]
enable = true
port = 2222
password = "baked-secret"
pub_keys = ["{VALID_SSH_KEY}"]
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
port = 2200
password = "mounted-secret"
pub_keys = []
""",
    )

    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
        environ={
            "SSH_ENABLE": " false ",
            "SSH_PORT": " 2022 ",
            "SSH_PASSWORD": " env secret with spaces ",
            "SSH_PUB_KEY": f"  {VALID_SSH_KEY}  ",
        },
    )

    assert result.config.system.ssh.enable is False
    assert result.config.system.ssh.port == 2022
    assert result.config.system.ssh.password == " env secret with spaces "
    assert result.config.system.ssh.pub_keys == [VALID_SSH_KEY]


def test_ssh_pub_key_env_empty_or_same_identity_is_a_quiet_noop(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        f"""
[system.ssh]
pub_keys = [
  "",
  "{VALID_SSH_KEY}",
  "{VALID_SSH_KEY.rsplit(" ", 1)[0]} baked-other@example",
]
""",
    )

    duplicate = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={"SSH_PUB_KEY": VALID_SSH_KEY.rsplit(" ", 1)[0] + " other@example"},
    )
    empty = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={"SSH_PUB_KEY": "  "},
    )

    assert duplicate.config.system.ssh.pub_keys == [VALID_SSH_KEY]
    assert empty.config.system.ssh.pub_keys == [VALID_SSH_KEY]


@pytest.mark.parametrize(
    ("value", "mounted_value"),
    [
        ("continue", "fail"),
        ("fail", "continue"),
    ],
)
def test_env_download_failure_policy_valid_values_override_runtime_config(
    tmp_path: Path,
    value: str,
    mounted_value: str,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[cdh]
download_failure_policy = "{mounted_value}"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
        environ={"CDH_DOWNLOAD_FAILURE_POLICY": value},
    )

    assert result.config.cdh.download_failure_policy == value


# Shutdown timeout uses the normal runtime precedence chain, with the
# environment as the final validated override.
def test_shutdown_timeout_env_overrides_mounted_and_baked_values(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[cdh]
shutdown_timeout = 20
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
shutdown_timeout = -1
""",
    )

    mounted_result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
        environ={},
    )
    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
        environ={"CDH_SHUTDOWN_TIMEOUT": "55.5"},
    )

    assert mounted_result.config.cdh.shutdown_timeout == -1
    assert result.config.cdh.shutdown_timeout == 55.5


@pytest.mark.parametrize("value", ["8", "0.25", "-1"])
def test_shutdown_timeout_env_accepts_finite_positive_or_disabled(
    tmp_path: Path,
    value: str,
) -> None:
    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={"CDH_SHUTDOWN_TIMEOUT": value},
    )

    assert result.config.cdh.shutdown_timeout == float(value)


@pytest.mark.parametrize("value", ["", " ", "maybe", "2"])
def test_invalid_ssh_enable_env_fails(tmp_path: Path, value: str) -> None:
    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"SSH_ENABLE": value},
        )

    assert _identities(raised.value) == [
        (("env", "SSH_ENABLE"), "env.invalid_ssh_enable")
    ]


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port", ""])
def test_invalid_ssh_port_env_fails(tmp_path: Path, value: str) -> None:
    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"SSH_PORT": value},
        )

    assert _identities(raised.value) == [(("env", "SSH_PORT"), "env.invalid_ssh_port")]


@pytest.mark.parametrize("value", ["", " ", "0", "-0.1", "-2", "nan", "inf", "false"])
def test_invalid_shutdown_timeout_env_fails_with_stable_identity(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"CDH_SHUTDOWN_TIMEOUT": value},
        )

    assert _identities(raised.value) == [
        (("env", "CDH_SHUTDOWN_TIMEOUT"), "env.invalid_shutdown_timeout")
    ]


def test_malformed_env_extra_args_fail_runtime_validation(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"CDH_COMFYUI_EXTRA_ARGS": '"unterminated'},
        )

    assert _identities(error.value) == [
        (("env", "CDH_COMFYUI_EXTRA_ARGS"), "env.invalid_extra_args")
    ]


@pytest.mark.parametrize("value", ["", "0", "65536", "8188.0", "abc"])
def test_invalid_env_port_values_fail_runtime_validation(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"CDH_COMFYUI_PORT": value},
        )

    assert _identities(error.value) == [
        (("env", "CDH_COMFYUI_PORT"), "env.invalid_port")
    ]


@pytest.mark.parametrize("value", ["", "0", "-1", "3.5", "abc"])
def test_invalid_env_download_max_attempts_values_fail_runtime_validation(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"CDH_DOWNLOAD_MAX_ATTEMPTS": value},
        )

    assert _identities(error.value) == [
        (("env", "CDH_DOWNLOAD_MAX_ATTEMPTS"), "env.invalid_download_max_attempts")
    ]
