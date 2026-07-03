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

[cdh.downloader.aria2]
split = 8
""",
    )

    result = load_runtime_config(baked_config_path=baked, mounted_config_path=mounted)

    assert result.config.comfyui.listen == "0.0.0.0"
    assert result.config.comfyui.port == 8288
    assert result.config.comfyui.extra_args == ["--preview-method", "auto"]
    assert result.config.cdh.default_downloader == "aria2"
    assert result.config.cdh.downloader.aria2.split == 8


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


def test_runtime_file_entries_are_rejected_until_m3(tmp_path: Path) -> None:
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
        )

    assert _identities(error.value) == [(("files",), "runtime.files_unsupported")]
    assert "not supported until v0.3-M3" in error.value.diagnostics[0].message


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
