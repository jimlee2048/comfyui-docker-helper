"""Validation tests for user-facing final configuration examples."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from comfyui_docker_helper.config import load_validate_config_result

EXAMPLES = Path("examples")


@pytest.mark.parametrize("name", ["minimal.toml", "full.toml"])
def test_example_configs_validate_offline(name: str) -> None:
    """Keep examples aligned with the single active final schema."""
    result = load_validate_config_result(
        EXAMPLES / name,
        scripts_dir=EXAMPLES / "scripts",
    )

    assert result.config.build.platforms == ["linux/amd64"]
    assert result.config.compute_platform.cuda.version == "13.0.3"
    assert result.config.pytorch.version == "2.12.1"
    assert result.warnings == ()


def test_full_example_references_existing_hook_scripts() -> None:
    result = load_validate_config_result(
        EXAMPLES / "full.toml",
        scripts_dir=EXAMPLES / "scripts",
    )
    hooks = [
        hook
        for node in result.config.comfyui.custom_nodes
        for hook in (*node.pre_install_scripts, *node.post_install_scripts)
    ]

    assert hooks == ["pre.sh", "post.sh"]
    assert all((EXAMPLES / "scripts" / hook).is_file() for hook in hooks)


def test_runtime_hook_example_is_regular_0644() -> None:
    hook = EXAMPLES / "hooks/pre-start.d/10-example.sh"

    assert hook.is_file()
    assert hook.stat().st_mode & 0o777 == 0o644


def test_full_example_covers_active_top_level_schema() -> None:
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
    assert set(document["compute_platform"]["cuda"]) == {"version"}
    assert set(document["build"]) == {"tags", "output", "platforms"}
    assert document["build"]["platforms"] == ["linux/amd64"]
    assert [node["type"] for node in document["comfyui"]["custom_nodes"]] == [
        "registry",
        "git",
    ]


@pytest.mark.parametrize("path", [Path("README.md"), EXAMPLES / "README.md"])
def test_readmes_document_final_planning_authority(path: Path) -> None:
    readme = path.read_text(encoding="utf-8")

    assert "config.lock.toml" in readme
    assert "build-plan.json" in readme
    assert "phase" in readme.lower()
    assert "--locked" in readme
    assert "--check" in readme
    assert "--dry-run" in readme
    assert "--upgrade-lock" in readme
    assert "--hooks-dir" in readme


def test_root_readme_documents_no_root_replanning() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "does not contain a root `config.toml`" in readme
    assert "Container build helpers consume only digest-bound phase inputs" in readme
    assert "literal `tag@sha256` references" in readme
    assert "runtime/config.toml" in readme
    assert "/opt/cdh/runtime/hooks" in readme
    assert "PyTorch configuration versions are selectors" in readme
    assert "2.12.1+cu130" in readme
    assert "Resolved versions never enter `request_digest`" in readme
    assert "separate public/local" in readme


@pytest.mark.parametrize(
    "path",
    [
        Path("README.md"),
        Path("src/comfyui_docker_helper/templates/cdh-release/README.md"),
    ],
)
def test_release_readmes_document_final_application_evidence(path: Path) -> None:
    readme = path.read_text(encoding="utf-8")

    assert "/opt/cdh/build/custom-node-inventory.json" in readme
    assert "/opt/cdh/build/application-inventory.txt" in readme
    assert "exact empty inventory" in readme
    assert "pass the declared URL through unchanged" in readme


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)
