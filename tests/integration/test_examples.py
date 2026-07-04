"""Validation tests for user-facing example configuration files."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from comfyui_docker_helper.config import load_validate_plan, load_validate_plan_result

pytestmark = pytest.mark.integration

EXAMPLES = Path("examples")
EXPECTED_FULL_KEYS = {
    "compute_platform": {"type", "cuda"},
    "compute_platform.cuda": {"version", "image_flavor", "image_distro"},
    "system": {"workspace", "comfyui_path", "extra_packages", "env"},
    "system.env": {"COMFYUI_PORT", "EXAMPLE_PROFILE"},
    "python": {"version", "uv_version", "index_url", "extra_packages"},
    "pytorch": {"version", "index_base_url", "extra_packages"},
    "cdh": {"default_downloader", "default_download_mode", "downloader"},
    "cdh.downloader.httpx": {"timeout", "retries"},
    "cdh.downloader.aria2": {
        "rpc_port",
        "split",
        "max_connection_per_server",
        "min_split_size",
        "resume_download",
    },
    "build": {"tags", "output"},
    "comfyui": {
        "version",
        "cli_version",
        "install_manager",
        "listen",
        "port",
        "extra_args",
        "custom_nodes",
    },
    "files": {"url", "dir", "filename", "overwrite", "downloader", "download_mode"},
}


@pytest.mark.parametrize("name", ["minimal.toml", "full.toml"])
def test_example_configs_validate(name: str) -> None:
    """Keep user-facing examples aligned with the public schema."""
    result = load_validate_plan_result(
        EXAMPLES / name,
        scripts_dir=EXAMPLES / "scripts",
    )
    plan = result.plan

    assert plan.output_manifest.always
    assert result.warnings == ()


def test_full_example_references_existing_hook_scripts() -> None:
    """Keep hook paths in full.toml synchronized with examples/scripts."""
    plan = load_validate_plan(EXAMPLES / "full.toml", scripts_dir=EXAMPLES / "scripts")

    hooks = [
        hook
        for node in plan.custom_nodes.items
        for hook in (*node.pre_install_scripts, *node.post_install_scripts)
    ]
    assert hooks == ["pre.sh", "post.sh"]
    for hook in hooks:
        assert (EXAMPLES / "scripts" / hook).is_file()


def test_full_example_covers_all_public_config_fields() -> None:
    """Document every currently supported top-level config field in full.toml."""
    document = _read_toml(EXAMPLES / "full.toml")

    assert set(document) == {
        "compute_platform",
        "system",
        "python",
        "pytorch",
        "cdh",
        "build",
        "comfyui",
        "files",
    }
    assert set(document["compute_platform"]) == EXPECTED_FULL_KEYS["compute_platform"]
    assert (
        set(document["compute_platform"]["cuda"])
        == EXPECTED_FULL_KEYS["compute_platform.cuda"]
    )
    assert set(document["system"]) == EXPECTED_FULL_KEYS["system"]
    assert set(document["system"]["env"]) == EXPECTED_FULL_KEYS["system.env"]
    assert set(document["python"]) == EXPECTED_FULL_KEYS["python"]
    assert set(document["pytorch"]) == EXPECTED_FULL_KEYS["pytorch"]
    assert set(document["cdh"]) == EXPECTED_FULL_KEYS["cdh"]
    assert (
        set(document["cdh"]["downloader"]["httpx"])
        == EXPECTED_FULL_KEYS["cdh.downloader.httpx"]
    )
    assert (
        set(document["cdh"]["downloader"]["aria2"])
        == EXPECTED_FULL_KEYS["cdh.downloader.aria2"]
    )
    assert set(document["build"]) == EXPECTED_FULL_KEYS["build"]
    assert set(document["comfyui"]) == EXPECTED_FULL_KEYS["comfyui"]

    custom_nodes = document["comfyui"]["custom_nodes"]
    assert [node["type"] for node in custom_nodes] == ["registry", "git"]
    assert set(custom_nodes[0]) == {
        "type",
        "id",
        "version",
        "pre_install_scripts",
        "post_install_scripts",
    }
    assert set(custom_nodes[1]) == {
        "type",
        "url",
        "ref",
        "target_dir",
        "pre_install_scripts",
        "post_install_scripts",
    }

    file_keys = [set(item) for item in document["files"]]
    assert file_keys == [EXPECTED_FULL_KEYS["files"], EXPECTED_FULL_KEYS["files"]]


def test_full_example_documents_host_build_defaults() -> None:
    """Keep the reference example aligned with the host build contract."""
    document = _read_toml(EXAMPLES / "full.toml")

    assert "cdh" in document
    assert document["build"]["tags"] == [
        "my-comfy:dev",
        "registry.example.com/my-comfy:dev",
    ]
    assert document["build"]["output"] == "load"
    assert document["cdh"]["default_download_mode"] == "sync"
    assert document["python"]["index_url"] == "https://pypi.org/simple"
    assert document["pytorch"]["index_base_url"] == "https://download.pytorch.org/whl"


def test_examples_readme_documents_host_build_workflow() -> None:
    """Keep user-facing example docs aligned with host build workflows."""
    readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")

    assert "[cdh]" in readme
    assert "default_download_mode" in readme
    assert "[build]" in readme
    assert "[build].tags" in readme
    assert "[build].output" in readme
    assert "--load" in readme
    assert "--push" in readme
    assert "--locked" in readme
    assert "--upgrade-lock" in readme
    assert "config.toml" in readme
    assert "config.lock.toml" in readme
    assert (
        "cdh host build -f examples/full.toml -t registry.example.com/my-comfy:dev "
        "--push"
    ) in readme


def test_user_facing_docs_describe_rendered_context_artifacts() -> None:
    """Keep docs centered on the current root rendered-context artifacts."""
    docs = [Path("README.md")]
    docs.extend(
        path
        for path in sorted(EXAMPLES.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".toml", ".sh"}
    )
    user_facing_text = "\n".join(path.read_text(encoding="utf-8") for path in docs)

    assert "config.toml" in user_facing_text
    assert "config.lock.toml" in user_facing_text
    assert "runtime/config.toml" in user_facing_text
    assert 'ENTRYPOINT ["cdh", "container", "entrypoint"]' in user_facing_text


def test_user_facing_docs_describe_current_lock_and_downloader_layout() -> None:
    """Keep user-facing docs aligned with current lock and downloader layout."""
    docs = [Path("README.md")]
    docs.extend(
        path
        for path in sorted(EXAMPLES.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".toml", ".sh"}
    )
    user_facing_text = "\n".join(path.read_text(encoding="utf-8") for path in docs)

    assert "--locked" in user_facing_text
    assert "--upgrade-lock" in user_facing_text
    assert "[cdh]" in user_facing_text
    assert "default_download_mode" in user_facing_text
    assert "CDH_DEFAULT_DOWNLOAD_MODE" in user_facing_text
    assert "[cdh]" in (EXAMPLES / "full.toml").read_text(encoding="utf-8")


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)
