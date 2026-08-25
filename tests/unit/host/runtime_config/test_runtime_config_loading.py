"""Runtime config loading and source precedence contracts."""

from pathlib import Path

from comfyui_docker_helper.config import (
    RuntimeConfig,
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


def test_missing_baked_and_mounted_runtime_configs_use_code_defaults(
    tmp_path: Path,
) -> None:
    result = load_runtime_config(
        baked_config_path=tmp_path / "missing-opt.toml",
        mounted_config_path=tmp_path / "missing-etc.toml",
    )

    assert result.config == RuntimeConfig()
    assert result.files == ()
    assert result.warnings == ()


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


def test_runtime_generic_merge_preserves_nested_siblings_and_sequence_resets(
    tmp_path: Path,
) -> None:
    baked = _write(
        tmp_path / "baked.toml",
        f"""
[comfyui]
extra_args = ["--cpu"]

[cdh.downloader.aria2]
split = 4
min_split_size = "2M"

[system.ssh]
pub_keys = ["{VALID_SSH_KEY}"]
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[comfyui]
extra_args = []

[cdh.downloader.aria2]
rpc_port = 6801

[system.ssh]
pub_keys = []
""",
    )

    result = load_runtime_config(
        baked_config_path=baked,
        mounted_config_path=mounted,
        environ={},
    )

    assert result.config.cdh.downloader.aria2.split == 4
    assert result.config.cdh.downloader.aria2.min_split_size == "2M"
    assert result.config.cdh.downloader.aria2.rpc_port == 6801
    assert result.config.comfyui.extra_args == []
    assert result.config.system.ssh.pub_keys == []
