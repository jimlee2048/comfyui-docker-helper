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
        build_hooks_dir=EXAMPLES / "build-hooks",
    )

    assert result.warnings == ()


# Referenced install hooks must ship with the full example they support.
def test_full_example_references_existing_hook_scripts() -> None:
    result = load_validate_config_result(
        EXAMPLES / "full.toml",
        build_hooks_dir=EXAMPLES / "build-hooks",
    )
    hooks = [
        hook
        for node in result.config.comfyui.custom_nodes
        for hook in (*node.pre_install_hooks, *node.post_install_hooks)
    ]

    assert hooks
    assert all((EXAMPLES / "build-hooks" / hook).is_file() for hook in hooks)


# Runtime hook examples preserve executable policy without relying on Git mode repair.
def test_runtime_hook_example_is_regular_0644() -> None:
    hook = EXAMPLES / "runtime-hooks/pre-start.d/10-example.sh"

    assert hook.is_file()
    assert hook.stat().st_mode & 0o777 == 0o644
