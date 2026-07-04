"""Tests for baked runtime config projection."""

import tomllib

from comfyui_docker_helper.config import load_validate_plan_result

MINIMAL_CONFIG = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"

[comfyui]
version = "latest"
"""


def test_runtime_projection_writes_deterministic_startup_and_downloader_defaults(
    tmp_path,
) -> None:
    """Bake effective runtime defaults while excluding host-only config."""
    path = tmp_path / "config.toml"
    path.write_text(
        MINIMAL_CONFIG
        + """
listen = "127.0.0.1"
port = 8190
extra_args = ["--preview-method", "auto", "--cpu"]
cli_version = "1.5.0"
install_manager = true

[system]
workspace = "/srv"
extra_packages = ["ffmpeg"]

[system.env]
HF_HOME = "/srv/cache"

[python]
extra_packages = ["httpx"]

[build]
tags = ["example:dev"]

[cdh]
default_downloader = "httpx"
default_download_mode = "sync"
download_max_attempts = 4
download_failure_policy = "continue"

[cdh.downloader.aria2]
split = 8

[cdh.downloader.httpx]
timeout = 30
retries = 5

[[comfyui.custom_nodes]]
type = "registry"
id = "node"

[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
overwrite = true
downloader = "httpx"
download_mode = "sync"
""",
        encoding="utf-8",
    )

    projection = load_validate_plan_result(path).runtime_config
    first = projection.to_toml_bytes()
    second = projection.to_toml_bytes()
    document = tomllib.loads(first.decode("utf-8"))

    assert first == second
    assert document == {
        "comfyui": {
            "listen": "127.0.0.1",
            "port": 8190,
            "extra_args": ["--preview-method", "auto", "--cpu"],
        },
        "cdh": {
            "default_downloader": "httpx",
            "default_download_mode": "sync",
            "download_max_attempts": 4,
            "download_failure_policy": "continue",
            "downloader": {
                "aria2": {
                    "rpc_port": 6800,
                    "split": 8,
                    "max_connection_per_server": 16,
                    "min_split_size": "1M",
                    "resume_download": True,
                },
                "httpx": {"timeout": 30, "retries": 5},
            },
        },
        "files": [
            {
                "url": "https://example.com/model.bin",
                "dir": "models",
                "filename": "model.bin",
                "overwrite": True,
                "downloader": "httpx",
                "download_mode": "sync",
            }
        ],
    }
    assert "system" not in document
    assert "python" not in document
    assert "pytorch" not in document
    assert "build" not in document
    assert "version" not in document["comfyui"]
    assert "cli_version" not in document["comfyui"]
    assert "install_manager" not in document["comfyui"]
    assert "custom_nodes" not in document["comfyui"]
    assert projection.is_explicit(("files", 0, "url"))
    assert projection.is_explicit(("files", 0, "download_mode"))


def test_runtime_projection_tracks_only_user_explicit_runtime_fields(tmp_path) -> None:
    """Distinguish omitted defaults from explicitly-authored default values."""
    omitted = tmp_path / "omitted.toml"
    explicit = tmp_path / "explicit.toml"
    omitted.write_text(MINIMAL_CONFIG, encoding="utf-8")
    explicit.write_text(
        MINIMAL_CONFIG
        + """
listen = "0.0.0.0"
port = 8188
extra_args = []

[cdh]
default_downloader = "aria2"
default_download_mode = "sync"
download_max_attempts = 3

[cdh.downloader.aria2]
rpc_port = 6800
""",
        encoding="utf-8",
    )

    omitted_projection = load_validate_plan_result(omitted).runtime_config
    explicit_projection = load_validate_plan_result(explicit).runtime_config

    assert omitted_projection.to_toml_bytes() == explicit_projection.to_toml_bytes()
    assert not omitted_projection.is_explicit(("comfyui", "listen"))
    assert not omitted_projection.is_explicit(("comfyui", "port"))
    assert not omitted_projection.is_explicit(("comfyui", "extra_args"))
    assert not omitted_projection.is_explicit(("cdh", "default_downloader"))
    assert not omitted_projection.is_explicit(("cdh", "default_download_mode"))
    assert not omitted_projection.is_explicit(("cdh", "download_max_attempts"))
    assert not omitted_projection.is_explicit(
        ("cdh", "downloader", "aria2", "rpc_port")
    )
    assert explicit_projection.is_explicit(("comfyui", "listen"))
    assert explicit_projection.is_explicit(("comfyui", "port"))
    assert explicit_projection.is_explicit(("comfyui", "extra_args"))
    assert explicit_projection.is_explicit(("cdh", "default_downloader"))
    assert explicit_projection.is_explicit(("cdh", "default_download_mode"))
    assert explicit_projection.is_explicit(("cdh", "download_max_attempts"))
    assert explicit_projection.is_explicit(("cdh", "downloader", "aria2", "rpc_port"))


def test_runtime_projection_omits_implicit_host_failure_policy(tmp_path) -> None:
    """Do not bake the host-only failure-policy default into runtime config."""
    omitted = tmp_path / "omitted.toml"
    explicit_fail = tmp_path / "explicit-fail.toml"
    explicit_continue = tmp_path / "explicit-continue.toml"
    omitted.write_text(MINIMAL_CONFIG, encoding="utf-8")
    explicit_fail.write_text(
        MINIMAL_CONFIG
        + """
[cdh]
download_failure_policy = "fail"
""",
        encoding="utf-8",
    )
    explicit_continue.write_text(
        MINIMAL_CONFIG
        + """
[cdh]
download_failure_policy = "continue"
""",
        encoding="utf-8",
    )

    omitted_projection = load_validate_plan_result(omitted).runtime_config
    explicit_fail_projection = load_validate_plan_result(explicit_fail).runtime_config
    explicit_continue_projection = load_validate_plan_result(
        explicit_continue
    ).runtime_config
    omitted_document = tomllib.loads(omitted_projection.to_toml_bytes().decode("utf-8"))
    explicit_fail_document = tomllib.loads(
        explicit_fail_projection.to_toml_bytes().decode("utf-8")
    )
    explicit_continue_document = tomllib.loads(
        explicit_continue_projection.to_toml_bytes().decode("utf-8")
    )

    assert "download_failure_policy" not in omitted_document["cdh"]
    assert explicit_fail_document["cdh"]["download_failure_policy"] == "fail"
    assert explicit_continue_document["cdh"]["download_failure_policy"] == "continue"
    assert not omitted_projection.is_explicit(("cdh", "download_failure_policy"))
    assert explicit_fail_projection.is_explicit(("cdh", "download_failure_policy"))
    assert explicit_continue_projection.is_explicit(("cdh", "download_failure_policy"))
