"""Validation tests for user-facing final configuration examples."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfyui_docker_helper.config import load_validate_config_result

EXAMPLES = Path("examples")


# Public examples must remain valid inputs to the configuration service.
@pytest.mark.parametrize("name", ["minimal.toml", "full.toml"])
def test_example_configs_validate_offline(name: str) -> None:
    result = load_validate_config_result(
        EXAMPLES / name,
        scripts_dir=EXAMPLES / "scripts",
    )

    assert result.warnings == ()


# Referenced install hooks must ship with the full example they support.
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

    assert hooks
    assert all((EXAMPLES / "scripts" / hook).is_file() for hook in hooks)


# Runtime hook examples preserve executable policy without relying on Git mode repair.
def test_runtime_hook_example_is_regular_0644() -> None:
    hook = EXAMPLES / "hooks/pre-start.d/10-example.sh"

    assert hook.is_file()
    assert hook.stat().st_mode & 0o777 == 0o644
