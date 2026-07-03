"""Validation tests for ComfyUI build smoke fixture inputs."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfyui_docker_helper.config import load_validate_plan

pytestmark = pytest.mark.smoke

COMFYUI_BUILD_FIXTURES = Path("tests/fixtures/comfyui-build")
CUSTOM_SCRIPTS_REF = "609f3afaa74b2f88ef9ce8d939626065e3247469"
CUSTOM_SCRIPTS_URL = "https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git"
RAW_README_URL = (
    "https://raw.githubusercontent.com/pythongosssss/ComfyUI-Custom-Scripts/"
    f"{CUSTOM_SCRIPTS_REF}/README.md"
)
GITHUB_REDIRECT_README_URL = (
    "https://github.com/pythongosssss/ComfyUI-Custom-Scripts/raw/"
    f"{CUSTOM_SCRIPTS_REF}/README.md"
)
EXPECTED_SCENARIOS = {
    # Keep the smoke fixture set explicit so every expensive Docker scenario has
    # a cheap schema/semantic check here.
    "minimal-pinned": {
        "config": "minimal-pinned.toml",
        "scripts": False,
    },
    "latest": {
        "config": "latest.toml",
        "scripts": False,
    },
    "nightly": {
        "config": "nightly.toml",
        "scripts": False,
    },
    "manager-only": {
        "config": "manager-only.toml",
        "scripts": False,
    },
    "registry-node": {
        "config": "registry-node.toml",
        "scripts": False,
    },
    "git-node": {
        "config": "git-node.toml",
        "scripts": False,
    },
    "hooks": {
        "config": "hooks.toml",
        "scripts": True,
    },
    "httpx-files": {
        "config": "httpx-files.toml",
        "scripts": False,
    },
    "aria2-files": {
        "config": "aria2-files.toml",
        "scripts": False,
    },
    "full": {
        "config": "full.toml",
        "scripts": True,
    },
}


@pytest.mark.parametrize(
    "config_path",
    sorted((COMFYUI_BUILD_FIXTURES / "configs").glob("*.toml")),
    ids=lambda path: path.stem,
)
def test_comfyui_build_smoke_configs_validate(config_path: Path) -> None:
    """Keep resource-heavy smoke configs aligned with the public schema."""
    plan = load_validate_plan(
        config_path,
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    assert plan.output_manifest.always


def test_comfyui_build_hooks_cover_all_phase_and_type_combinations() -> None:
    """Ensure the hook fixture keeps .sh/.py and pre/post coverage."""
    plan = load_validate_plan(
        COMFYUI_BUILD_FIXTURES / "configs" / "hooks.toml",
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    (node,) = plan.custom_nodes.items
    assert node.pre_install_scripts == ("pre.sh", "pre.py")
    assert node.post_install_scripts == ("post.sh", "post.py")
    for script in node.pre_install_scripts + node.post_install_scripts:
        assert (COMFYUI_BUILD_FIXTURES / "scripts" / script).is_file()


def test_comfyui_build_fixture_inventory_matches_expected_scenarios() -> None:
    """Keep the concrete fixture inventory aligned with the smoke matrix."""
    configs = {
        path.name for path in (COMFYUI_BUILD_FIXTURES / "configs").glob("*.toml")
    }

    assert configs == {scenario["config"] for scenario in EXPECTED_SCENARIOS.values()}


def test_comfyui_build_scenario_semantics_match_matrix() -> None:
    """Protect key external-behavior intents without running Docker."""
    plans = {
        scenario: load_validate_plan(
            COMFYUI_BUILD_FIXTURES / "configs" / details["config"],
            scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
        )
        for scenario, details in EXPECTED_SCENARIOS.items()
    }

    assert plans["minimal-pinned"].comfyui.version == "0.9.2"
    assert plans["latest"].comfyui.version == "latest"
    assert plans["nightly"].comfyui.version == "nightly"
    for scenario in ("minimal-pinned", "latest", "nightly"):
        assert plans[scenario].comfyui.install_manager is False
        assert plans[scenario].custom_nodes.items == ()
        assert plans[scenario].files.items == ()

    assert plans["manager-only"].comfyui.install_manager is True
    assert plans["manager-only"].custom_nodes.items == ()

    (registry_node,) = plans["registry-node"].custom_nodes.items
    assert registry_node.type == "registry"
    assert registry_node.id == "comfyui-custom-scripts"
    assert registry_node.version == "latest"
    assert registry_node.target == "comfyui-custom-scripts@latest"
    assert plans["registry-node"].custom_nodes.update_cache is True

    (git_node,) = plans["git-node"].custom_nodes.items
    assert git_node.type == "git"
    assert git_node.url == CUSTOM_SCRIPTS_URL
    assert git_node.ref == CUSTOM_SCRIPTS_REF
    assert plans["git-node"].custom_nodes.update_cache is False

    httpx_items = plans["httpx-files"].files.items
    assert plans["httpx-files"].files.downloader.default == "httpx"
    assert [item.downloader for item in httpx_items] == ["httpx", "httpx", "httpx"]
    assert [item.url for item in httpx_items] == [
        RAW_README_URL,
        GITHUB_REDIRECT_README_URL,
        RAW_README_URL,
    ]
    assert len({item.target for item in httpx_items}) == 1
    assert [item.overwrite for item in httpx_items] == [False, True, False]

    (aria2_item,) = plans["aria2-files"].files.items
    assert plans["aria2-files"].files.downloader.default == "aria2"
    assert aria2_item.downloader == "aria2"
    assert aria2_item.url == RAW_README_URL

    full = plans["full"]
    assert full.comfyui.install_manager is True
    assert [node.type for node in full.custom_nodes.items] == ["registry", "git"]
    assert full.custom_nodes.has_hooks is True
    assert [item.downloader for item in full.files.items] == ["httpx", "aria2"]
    assert [(item.name, item.value) for item in full.environment] == [
        ("CDH_SMOKE_ENV", "comfyui-build-full")
    ]
