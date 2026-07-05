"""Tests for container runtime configuration loading and merge."""

from pathlib import Path

import pytest

from comfyui_docker_helper.config import (
    DiagnosticSeverity,
    RuntimeConfigurationError,
    load_runtime_config,
)


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
    assert result.files == ()
    assert result.file_documents == ()
    assert result.warnings == ()
    assert result.explicit_paths == frozenset()


def test_empty_baked_and_mounted_runtime_configs_use_code_defaults(
    tmp_path: Path,
) -> None:
    baked = _write(tmp_path / "baked.toml", "")
    mounted = _write(tmp_path / "mounted.toml", "")

    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
    )

    assert result.config.comfyui.listen == "0.0.0.0"
    assert result.config.comfyui.port == 8188
    assert result.config.cdh.downloader.aria2.split == 16
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
retries = 4
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
    assert result.config.cdh.downloader.httpx.retries == 4


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
cli_version = "latest"
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
        (("system",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (("python",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (("pytorch",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (("build",), "runtime.host_only_ignored", DiagnosticSeverity.WARNING),
        (
            ("comfyui", "version"),
            "runtime.host_only_ignored",
            DiagnosticSeverity.WARNING,
        ),
        (
            ("comfyui", "cli_version"),
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


# Runtime file merge tests preserve baked/mounted ordering and same-target
# override behavior before the downloader plan consumes entries.
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


def test_runtime_file_merge_appends_mounted_files_to_baked_files(
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
url = "https://example.com/mounted.bin"
dir = "models"
filename = "mounted.bin"
downloader = "httpx"
""",
    )

    result = load_runtime_config(baked_config_path=baked, mounted_config_path=mounted)

    assert result.files == (
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


def test_runtime_file_merge_same_key_mounted_values_override_baked(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        """
[[files]]
url = "https://example.com/baked.bin"
dir = "models"
filename = "model.bin"
overwrite = false
downloader = "aria2"
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/mounted.bin"
dir = "models"
filename = "model.bin"
overwrite = true
""",
    )

    result = load_runtime_config(baked_config_path=baked, mounted_config_path=mounted)

    assert result.files == (
        {
            "url": "https://example.com/mounted.bin",
            "dir": "models",
            "filename": "model.bin",
            "overwrite": True,
            "downloader": "aria2",
        },
    )


def test_runtime_file_merge_mounted_empty_files_resets_baked_files(
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
    mounted = _write(tmp_path / "mounted.toml", "files = []\n")

    result = load_runtime_config(baked_config_path=baked, mounted_config_path=mounted)

    assert result.files == ()


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
retries = -1
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
        (
            ("cdh", "downloader", "httpx", "retries"),
            "cdh.downloader.httpx_retries_negative",
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


def test_unsupported_downloader_alias_env_var_has_no_effect(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[cdh]
default_downloader = "aria2"
""",
    )

    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=mounted,
        environ={"CDH_DOWNLOADER_DEFAULT": "httpx"},
    )

    assert result.config.cdh.default_downloader == "aria2"


def test_backend_tuning_env_vars_are_not_applied(tmp_path: Path) -> None:
    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-baked.toml",
        mounted_config_path=tmp_path / "missing-mounted.toml",
        environ={
            "CDH_ARIA2_RPC_PORT": "6811",
            "CDH_DOWNLOADER_ARIA2_SPLIT": "1",
            "CDH_HTTPX_TIMEOUT": "5",
        },
    )

    assert result.config.cdh.downloader.aria2.rpc_port == 6800
    assert result.config.cdh.downloader.aria2.split == 16
    assert result.config.cdh.downloader.httpx.timeout == 60


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
