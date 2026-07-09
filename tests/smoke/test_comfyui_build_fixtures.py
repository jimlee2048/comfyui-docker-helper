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


@pytest.mark.parametrize(
    "config_name",
    ["hooks.toml", "full.toml"],
    ids=["hooks", "full"],
)
def test_comfyui_build_hooks_cover_all_phase_and_type_combinations(
    config_name: str,
) -> None:
    """Ensure the hook fixture keeps .sh/.py and pre/post coverage."""
    plan = load_validate_plan(
        COMFYUI_BUILD_FIXTURES / "configs" / config_name,
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    (node,) = [
        node
        for node in plan.custom_nodes.items
        if node.pre_install_scripts or node.post_install_scripts
    ]
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


@pytest.mark.parametrize(
    ("scenario", "version"),
    [
        ("minimal-pinned", "0.9.2"),
        ("latest", "latest"),
        ("nightly", "nightly"),
    ],
)
def test_comfyui_build_core_version_scenarios_match_matrix(
    scenario: str,
    version: str,
) -> None:
    """Protect the no-manager/no-extra baseline variants."""
    plan = load_validate_plan(
        COMFYUI_BUILD_FIXTURES / "configs" / EXPECTED_SCENARIOS[scenario]["config"],
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    assert plan.comfyui.version == version
    assert plan.comfyui.install_manager is False
    assert plan.custom_nodes.items == ()
    assert plan.files.items == ()


def test_comfyui_build_manager_only_scenario_matches_matrix() -> None:
    """Protect the manager-only smoke scenario."""
    plan = load_validate_plan(
        COMFYUI_BUILD_FIXTURES
        / "configs"
        / EXPECTED_SCENARIOS["manager-only"]["config"],
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    assert plan.comfyui.install_manager is True
    assert plan.custom_nodes.items == ()


def test_comfyui_build_registry_node_scenario_matches_matrix() -> None:
    """Protect the registry custom-node smoke scenario."""
    plan = load_validate_plan(
        COMFYUI_BUILD_FIXTURES
        / "configs"
        / EXPECTED_SCENARIOS["registry-node"]["config"],
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    (registry_node,) = plan.custom_nodes.items
    assert registry_node.type == "registry"
    assert registry_node.id == "comfyui-custom-scripts"
    assert registry_node.version == "latest"
    assert registry_node.target == "comfyui-custom-scripts@latest"
    assert plan.custom_nodes.update_cache is True


def test_comfyui_build_git_node_scenario_matches_matrix() -> None:
    """Protect the Git custom-node smoke scenario."""
    plan = load_validate_plan(
        COMFYUI_BUILD_FIXTURES / "configs" / EXPECTED_SCENARIOS["git-node"]["config"],
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    (git_node,) = plan.custom_nodes.items
    assert git_node.type == "git"
    assert git_node.url == CUSTOM_SCRIPTS_URL
    assert git_node.ref == CUSTOM_SCRIPTS_REF
    assert plan.custom_nodes.update_cache is False


def test_comfyui_build_httpx_files_scenario_matches_matrix() -> None:
    """Protect the httpx download smoke scenario."""
    plan = load_validate_plan(
        COMFYUI_BUILD_FIXTURES
        / "configs"
        / EXPECTED_SCENARIOS["httpx-files"]["config"],
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    httpx_items = plan.files.items
    assert plan.files.downloader.default == "httpx"
    assert [item.downloader for item in httpx_items] == ["httpx", "httpx", "httpx"]
    assert [item.url for item in httpx_items] == [
        RAW_README_URL,
        GITHUB_REDIRECT_README_URL,
        RAW_README_URL,
    ]
    assert len({item.target for item in httpx_items}) == 1
    assert [item.overwrite for item in httpx_items] == [False, True, False]


def test_comfyui_build_aria2_files_scenario_matches_matrix() -> None:
    """Protect the aria2 download smoke scenario."""
    plan = load_validate_plan(
        COMFYUI_BUILD_FIXTURES
        / "configs"
        / EXPECTED_SCENARIOS["aria2-files"]["config"],
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    (aria2_item,) = plan.files.items
    assert plan.files.downloader.default == "aria2"
    assert aria2_item.downloader == "aria2"
    assert aria2_item.url == RAW_README_URL


@pytest.mark.parametrize(
    ("scenario", "details"),
    EXPECTED_SCENARIOS.items(),
    ids=EXPECTED_SCENARIOS,
)
def test_comfyui_build_script_requirements_match_matrix(
    scenario: str,
    details: dict[str, object],
) -> None:
    """Protect which smoke scenarios require hook scripts."""
    plan = load_validate_plan(
        COMFYUI_BUILD_FIXTURES / "configs" / details["config"],
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    assert plan.custom_nodes.has_hooks is details["scripts"], scenario


def test_comfyui_build_full_scenario_matches_matrix() -> None:
    """Protect the full composition smoke scenario."""
    full = load_validate_plan(
        COMFYUI_BUILD_FIXTURES / "configs" / EXPECTED_SCENARIOS["full"]["config"],
        scripts_dir=COMFYUI_BUILD_FIXTURES / "scripts",
    )

    assert full.comfyui.install_manager is True
    assert [node.type for node in full.custom_nodes.items] == ["registry", "git"]
    assert full.custom_nodes.has_hooks is True
    assert [item.downloader for item in full.files.items] == ["httpx", "aria2"]
    assert [(item.name, item.value) for item in full.environment] == [
        ("CDH_SMOKE_ENV", "comfyui-build-full")
    ]
