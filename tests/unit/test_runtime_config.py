"""Tests for container runtime configuration loading and merge."""

from pathlib import Path

import pytest

from comfyui_docker_helper.config import (
    DiagnosticSeverity,
    RuntimeConfigurationError,
    load_runtime_config,
)

VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "test@example"
)
TRUNCATED_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 truncated"


def _write(path: Path, document: str) -> Path:
    path.write_text(document, encoding="utf-8")
    return path


def _identities(error: RuntimeConfigurationError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


# Runtime config precedence is code defaults, baked config, mounted config, then
# explicit environment overrides.
def test_missing_baked_and_mounted_runtime_configs_use_code_defaults(
    tmp_path: Path,
) -> None:
    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-opt.toml",
        mounted_config_path=tmp_path / "missing-etc.toml",
    )

    assert result.config.comfyui.listen == "0.0.0.0"
    assert result.config.comfyui.port == 8188
    assert result.config.comfyui.extra_args == []
    assert result.config.cdh.default_downloader == "aria2"
    assert result.config.cdh.default_download_mode == "sync"
    assert result.config.cdh.download_max_attempts == 3
    assert result.config.cdh.download_failure_policy == "continue"
    assert result.config.cdh.shutdown_timeout == 8
    assert result.config.system.ssh.enable is False
    assert result.config.system.ssh.port == 22
    assert result.config.system.ssh.password == ""
    assert result.config.system.ssh.pub_keys == []
    assert result.files == ()
    assert result.file_documents == ()
    assert result.warnings == ()
    assert result.explicit_paths == frozenset()


def test_baked_config_overrides_code_defaults(tmp_path: Path) -> None:
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
download_max_attempts = 5
download_failure_policy = "fail"

[cdh.downloader.httpx]
timeout = 15
""",
    )

    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=tmp_path / "missing-mounted.toml",
    )

    assert result.config.comfyui.listen == "127.0.0.1"
    assert result.config.comfyui.port == 8190
    assert result.config.comfyui.extra_args == ["--cpu"]
    assert result.config.cdh.default_downloader == "httpx"
    assert result.config.cdh.default_download_mode == "sync"
    assert result.config.cdh.download_max_attempts == 5
    assert result.config.cdh.download_failure_policy == "fail"
    assert result.config.cdh.downloader.httpx.timeout == 15


def test_mounted_config_overrides_baked_config(tmp_path: Path) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[comfyui]
listen = "127.0.0.1"
port = 8190
extra_args = ["--cpu"]

[cdh]
default_downloader = "httpx"

[cdh.downloader.aria2]
split = 4

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
[comfyui]
listen = "0.0.0.0"
port = 8288
extra_args = ["--preview-method", "auto"]

[cdh]
default_downloader = "aria2"
download_max_attempts = 6
download_failure_policy = "continue"

[cdh.downloader.aria2]
split = 8

[system.ssh]
enable = false
port = 2200
password = ""
pub_keys = []
""",
    )

    result = load_runtime_config(baked_config_path=baked, mounted_config_path=mounted)

    assert result.config.comfyui.listen == "0.0.0.0"
    assert result.config.comfyui.port == 8288
    assert result.config.comfyui.extra_args == ["--preview-method", "auto"]
    assert result.config.cdh.default_downloader == "aria2"
    assert result.config.cdh.download_max_attempts == 6
    assert result.config.cdh.download_failure_policy == "continue"
    assert result.config.cdh.downloader.aria2.split == 8
    assert result.config.system.ssh.enable is False
    assert result.config.system.ssh.port == 2200
    assert result.config.system.ssh.password == ""
    assert result.config.system.ssh.pub_keys == []


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


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port", ""])
def test_invalid_ssh_port_env_fails(tmp_path: Path, value: str) -> None:
    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"SSH_PORT": value},
        )

    assert _identities(raised.value) == [(("env", "SSH_PORT"), "env.invalid_ssh_port")]


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


def test_ssh_pub_key_env_empty_appends_none_and_exact_duplicate_is_deduped(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        f"""
[system.ssh]
pub_keys = ["", "{VALID_SSH_KEY}"]
""",
    )

    duplicate = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={"SSH_PUB_KEY": VALID_SSH_KEY},
    )
    empty = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={"SSH_PUB_KEY": "  "},
    )

    assert duplicate.config.system.ssh.pub_keys == [VALID_SSH_KEY]
    assert empty.config.system.ssh.pub_keys == [VALID_SSH_KEY]


def test_invalid_ssh_public_keys_fail_without_leaking_password(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
password = "super-secret"
pub_keys = ["not-a-key"]
""",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in raised.value.diagnostics
    )
    assert _identities(raised.value) == [
        (("system", "ssh", "pub_keys", 0), "ssh.invalid_public_key")
    ]
    assert "super-secret" not in payload


def test_truncated_base64_valid_ssh_public_key_fails(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[system.ssh]
pub_keys = ["{TRUNCATED_SSH_KEY}"]
""",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(raised.value) == [
        (("system", "ssh", "pub_keys", 0), "ssh.invalid_public_key")
    ]


def test_embedded_newline_ssh_public_key_fails_without_leaking_key(
    tmp_path: Path,
) -> None:
    injected = f"{VALID_SSH_KEY}\nssh-ed25519 injected"
    mounted = _write(
        tmp_path / "mounted.toml",
        f'''
[system.ssh]
password = "super-secret"
pub_keys = ["""{injected}"""]
''',
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in raised.value.diagnostics
    )
    assert _identities(raised.value) == [
        (("system", "ssh", "pub_keys", 0), "ssh.invalid_public_key")
    ]
    assert "super-secret" not in payload
    assert VALID_SSH_KEY not in payload
    assert "injected" not in payload


def test_nul_ssh_pub_key_env_fails_without_leaking_key(tmp_path: Path) -> None:
    injected = f"{VALID_SSH_KEY}\x00comment"

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={
                "SSH_PASSWORD": "env-super-secret",
                "SSH_PUB_KEY": injected,
            },
        )

    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in raised.value.diagnostics
    )
    assert _identities(raised.value) == [
        (("env", "SSH_PUB_KEY"), "env.invalid_ssh_pub_key")
    ]
    assert "env-super-secret" not in payload
    assert VALID_SSH_KEY not in payload


def test_invalid_ssh_pub_key_env_fails_without_leaking_password(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={
                "SSH_PASSWORD": "env-super-secret",
                "SSH_PUB_KEY": TRUNCATED_SSH_KEY,
            },
        )

    payload = "\n".join(
        f"{item.path} {item.code} {item.message}" for item in raised.value.diagnostics
    )
    assert _identities(raised.value) == [
        (("env", "SSH_PUB_KEY"), "env.invalid_ssh_pub_key")
    ]
    assert "env-super-secret" not in payload


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


@pytest.mark.parametrize("value", ["0", "-0.1", "-2", "nan", "inf", '"8"', "true"])
def test_invalid_shutdown_timeout_toml_fails_schema_validation(
    tmp_path: Path,
    value: str,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"[cdh]\nshutdown_timeout = {value}\n",
    )

    with pytest.raises(RuntimeConfigurationError) as raised:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
        )

    assert [item.path for item in raised.value.diagnostics] == [
        ("cdh", "shutdown_timeout")
    ]


# Host-only build-time settings may appear in mounted files but must not affect
# container runtime state.
def test_known_host_only_runtime_config_warns_and_is_ignored(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[system]
workspace = "/srv"

[python]
version = "3.12"

[pytorch]
version = "2.10"

[build]
tags = ["example:dev"]

[comfyui]
version = "latest"
install_cli = false
install_manager = true
listen = "127.0.0.1"

[[comfyui.custom_nodes]]
type = "registry"
id = "node"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
    )

    assert result.config.comfyui.listen == "127.0.0.1"
    assert [(item.path, item.code, item.severity) for item in result.warnings] == [
        (
            ("compute_platform",),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("system", "workspace"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (("python",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (("pytorch",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (("build",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (
            ("comfyui", "version"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("comfyui", "install_cli"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("comfyui", "install_manager"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("comfyui", "custom_nodes"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
    ]
    assert result.is_explicit(("comfyui", "listen"))
    assert not result.is_explicit(("comfyui", "version"))
    assert not result.is_explicit(("system", "workspace"))


def test_unknown_runtime_sections_and_fields_fail(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[readiness]
timeout = 60

[comfyui]
unknown = true

[cdh]
unexpected = "value"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("comfyui", "unknown"), "schema.extra_forbidden"),
        (("cdh", "unexpected"), "schema.extra_forbidden"),
        (("readiness",), "schema.extra_forbidden"),
    ]


# Runtime file config coverage preserves file-layer shape plus authored files.N
# diagnostics before runtime_files turns entries into executable plans.
def test_runtime_file_entries_are_accepted_and_recorded(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
    )

    assert result.files == (
        {
            "url": "https://example.com/model.bin",
            "dir": "models",
            "filename": "model.bin",
        },
    )
    assert result.file_documents == (
        {
            "files": [
                {
                    "url": "https://example.com/model.bin",
                    "dir": "models",
                    "filename": "model.bin",
                }
            ]
        },
    )


def test_runtime_file_url_accepts_valid_userinfo(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://user:password@example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
    )

    assert result.files[0]["url"] == "https://user:password@example.com/model.bin"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("url", "https://example.com/model\\u007f.bin", "runtime_file.invalid_url"),
        ("dir", "models\\u007fescape", "runtime_file.control_character"),
        ("filename", "model\\u007f.bin", "runtime_file.invalid_filename"),
    ],
)
def test_runtime_file_domains_reject_control_characters(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    values = {
        "url": "https://example.com/model.bin",
        "dir": "models",
        "filename": "model.bin",
    }
    values[field] = value
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[[files]]
url = "{values["url"]}"
dir = "{values["dir"]}"
filename = "{values["filename"]}"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [(("files", 0, field), code)]


def test_runtime_file_non_http_url_fails_runtime_validation(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "ftp://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "url"), "runtime_file.invalid_url")
    ]


def test_runtime_file_rejects_reserved_staging_final_leaf(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = ".cdh-staging"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "filename"), "runtime_file.invalid_filename")
    ]


def test_invalid_mounted_runtime_file_after_baked_reports_authored_index(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[[files]]
url = "https://example.com/baked.bin"
dir = "models"
filename = "baked.bin"
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "ftp://example.com/mounted.bin"
dir = "models"
filename = "mounted.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(baked_config_path=baked, mounted_config_path=mounted)

    assert _identities(error.value) == [
        (("files", 0, "url"), "runtime_file.invalid_url")
    ]


def test_multiple_invalid_runtime_file_items_keep_authored_indexes(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/a.bin"
dir = "/models"
filename = "a.bin"

[[files]]
url = "https://example.com/b.bin"
dir = "models"
filename = "nested/b.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "dir"), "runtime_file.absolute_directory"),
        (("files", 1, "filename"), "runtime_file.invalid_filename"),
    ]


def test_runtime_file_async_download_mode_is_accepted(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
download_mode = "async"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
    )

    assert result.files[0]["download_mode"] == "async"


def test_runtime_file_invalid_download_mode_fails_schema_validation(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
download_mode = "parallel"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "download_mode"), "schema.literal_error")
    ]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("download_max_attempts", "0", "schema.greater_than_equal"),
        ("download_max_attempts", "-1", "schema.greater_than_equal"),
        ("download_failure_policy", '"skip"', "schema.literal_error"),
    ],
)
def test_invalid_runtime_download_policy_values_fail_schema_validation(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[cdh]
{field} = {value}
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [(("cdh", field), code)]


def test_runtime_file_unknown_field_fails_schema_validation(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
unexpected = true
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("files", 0, "unexpected"), "schema.extra_forbidden")
    ]


def test_runtime_file_merge_preserves_current_baked_mounted_contract(
    tmp_path: Path,
) -> None:
    appended_baked = _write(
        tmp_path / "baked.toml",
        """
[[files]]
url = "https://example.com/baked.bin"
dir = "models"
filename = "baked.bin"
""",
    )
    appended_mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/mounted.bin"
dir = "models"
filename = "mounted.bin"
downloader = "httpx"
""",
    )

    appended = load_runtime_config(
        baked_config_path=appended_baked,
        mounted_config_path=appended_mounted,
    )

    assert appended.files == (
        {
            "url": "https://example.com/baked.bin",
            "dir": "models",
            "filename": "baked.bin",
        },
        {
            "url": "https://example.com/mounted.bin",
            "dir": "models",
            "filename": "mounted.bin",
            "downloader": "httpx",
        },
    )

    override_baked = _write(
        tmp_path / "override-baked.toml",
        """
[[files]]
url = "https://example.com/baked.bin"
dir = "models"
filename = "model.bin"
overwrite = false
downloader = "aria2"
""",
    )
    override_mounted = _write(
        tmp_path / "override-mounted.toml",
        """
[[files]]
url = "https://example.com/mounted.bin"
dir = "models"
filename = "model.bin"
overwrite = true
""",
    )

    overridden = load_runtime_config(
        baked_config_path=override_baked,
        mounted_config_path=override_mounted,
    )

    assert overridden.files == (
        {
            "url": "https://example.com/mounted.bin",
            "dir": "models",
            "filename": "model.bin",
            "overwrite": True,
            "downloader": "aria2",
        },
    )

    reset_baked = _write(
        tmp_path / "reset-baked.toml",
        """
[[files]]
url = "https://example.com/baked.bin"
dir = "models"
filename = "baked.bin"
""",
    )
    reset_mounted = _write(tmp_path / "reset-mounted.toml", "files = []\n")

    reset = load_runtime_config(
        baked_config_path=reset_baked,
        mounted_config_path=reset_mounted,
    )

    assert reset.files == ()


# Downloader validation keeps runtime-only backend tuning strict at load time.
def test_invalid_baked_aria2_backend_values_fail_runtime_validation(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[cdh.downloader.aria2]
rpc_port = 0
split = 0
max_connection_per_server = 0
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=baked,
            mounted_config_path=tmp_path / "missing-mounted.toml",
        )

    assert _identities(error.value) == [
        (
            ("cdh", "downloader", "aria2", "rpc_port"),
            "cdh.downloader.aria2_rpc_port_out_of_range",
        ),
        (
            ("cdh", "downloader", "aria2", "split"),
            "cdh.downloader.aria2_split_not_positive",
        ),
        (
            ("cdh", "downloader", "aria2", "max_connection_per_server"),
            "cdh.downloader.aria2_max_connection_per_server_not_positive",
        ),
    ]


def test_invalid_mounted_httpx_backend_values_fail_runtime_validation(
    tmp_path: Path,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh.downloader.httpx]
timeout = 0
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (
            ("cdh", "downloader", "httpx", "timeout"),
            "cdh.downloader.httpx_timeout_not_positive",
        ),
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


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        (
            "CDH_DEFAULT_DOWNLOADER",
            "curl",
            (("cdh", "default_downloader"), "schema.literal_error"),
        ),
        (
            "CDH_DEFAULT_DOWNLOAD_MODE",
            "parallel",
            (("cdh", "default_download_mode"), "schema.literal_error"),
        ),
        (
            "CDH_DOWNLOAD_FAILURE_POLICY",
            "skip",
            (("cdh", "download_failure_policy"), "schema.literal_error"),
        ),
    ],
)
def test_invalid_env_enum_values_fail_runtime_validation(
    tmp_path: Path,
    name: str,
    value: str,
    expected: tuple[tuple, str],
) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={name: value},
        )

    assert _identities(error.value) == [expected]


# ComfyUI process ownership stays with the entrypoint for listen, port, and
# auto-launch flags even when extra args come from runtime config or env.
@pytest.mark.parametrize(
    "argument",
    [
        "--listen",
        "--listen=127.0.0.1",
        "--port",
        "--port=8190",
        "--auto-launch",
        "--auto-launch=true",
        "--disable-auto-launch",
        "--disable-auto-launch=true",
    ],
)
def test_runtime_extra_args_reject_cdh_controlled_flags(
    tmp_path: Path,
    argument: str,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[comfyui]
extra_args = ["--cpu", "{argument}"]
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [
        (("comfyui", "extra_args", 1), "comfyui.controlled_extra_arg")
    ]


@pytest.mark.parametrize(
    ("document", "path", "code"),
    [
        (
            'listen = "host\\u007fpart"',
            ("comfyui", "listen"),
            "comfyui.invalid_listen",
        ),
        (
            'extra_args = ["--cpu\\u007fprobe"]',
            ("comfyui", "extra_args", 0),
            "comfyui.invalid_extra_arg",
        ),
    ],
)
def test_runtime_comfyui_argv_rejects_control_characters(
    tmp_path: Path,
    document: str,
    path: tuple[str | int, ...],
    code: str,
) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[comfyui]
{document}
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [(path, code)]


def test_env_extra_args_reject_cdh_controlled_flags(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            environ={"CDH_COMFYUI_EXTRA_ARGS": "--cpu --port=8190"},
        )

    assert _identities(error.value) == [
        (("comfyui", "extra_args", 1), "comfyui.controlled_extra_arg")
    ]


def test_baked_runtime_defaults_do_not_create_user_explicit_provenance(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[comfyui]
listen = "0.0.0.0"
port = 8188
extra_args = []

[cdh]
default_downloader = "aria2"
default_download_mode = "sync"

[cdh.downloader.aria2]
rpc_port = 6800
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
port = 8188
""",
    )

    result = load_runtime_config(baked_config_path=baked, mounted_config_path=mounted)

    assert result.config.comfyui.listen == "0.0.0.0"
    assert result.config.comfyui.port == 8188
    assert not result.is_explicit(("comfyui", "listen"))
    assert result.is_explicit(("comfyui", "port"))
    assert not result.is_explicit(("cdh", "default_downloader"))
