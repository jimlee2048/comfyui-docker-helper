"""Container-side custom-node helper planning tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfyui_docker_helper.container.custom_nodes import (
    CustomNodesConfigError,
    build_custom_nodes_plan,
    load_custom_nodes_plan,
)


def test_load_custom_nodes_plan_preserves_order_targets_and_cache_flag(
    tmp_path: Path,
) -> None:
    """Build a deterministic plan from generated helper TOML."""
    config = tmp_path / "custom-nodes.toml"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre-a.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "pre-b.py").write_text("pass\n", encoding="utf-8")
    (scripts / "post.py").write_text("pass\n", encoding="utf-8")
    config.write_text(
        """
[comfyui]

[[comfyui.custom_nodes]]
type = "registry"
id = "first"
version = "1.2.3"
pre_install_scripts = ["pre-a.sh", "pre-b.py"]
post_install_scripts = []

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/second.git"
ref = "stable"
target_dir = "second-custom"
pre_install_scripts = []
post_install_scripts = ["post.py"]

[[comfyui.custom_nodes]]
type = "registry"
id = "third"
pre_install_scripts = []
post_install_scripts = []
""".lstrip(),
        encoding="utf-8",
    )

    plan = load_custom_nodes_plan(config, scripts_dir=scripts)

    assert plan.update_cache is True
    assert plan.has_hooks is True
    assert plan.scripts_source_dir == scripts.resolve()
    assert [node.type for node in plan.items] == ["registry", "git", "registry"]
    assert [node.target for node in plan.items] == [
        "first@1.2.3",
        "https://example.com/second.git@stable",
        "third",
    ]
    assert plan.items[0].pre_install_scripts == ("pre-a.sh", "pre-b.py")
    assert plan.items[1].target_dir == "second-custom"
    assert plan.items[1].post_install_scripts == ("post.py",)


def test_git_only_plan_skips_registry_cache_and_scripts_dir() -> None:
    """Do not update Manager cache or inspect scripts-dir when no hook exists."""
    plan = build_custom_nodes_plan(
        {
            "comfyui": {
                "custom_nodes": [
                    {
                        "type": "git",
                        "url": "https://example.com/node.git",
                    }
                ]
            }
        },
        scripts_dir=Path("/does/not/need/to/exist"),
    )

    assert plan.update_cache is False
    assert plan.has_hooks is False
    assert plan.scripts_source_dir is None
    assert plan.items[0].target == "https://example.com/node.git"
    assert plan.items[0].target_dir is None


def test_empty_custom_nodes_plan_is_valid_and_deterministic() -> None:
    """Allow the generated helper shape even when it contains no nodes."""
    plan = build_custom_nodes_plan({"comfyui": {"custom_nodes": []}})

    assert plan.items == ()
    assert plan.update_cache is False
    assert plan.has_hooks is False
    assert plan.scripts_source_dir is None


def test_hooks_require_existing_scripts_dir() -> None:
    """Fail early if generated hook references cannot be resolved."""
    document = {
        "comfyui": {
            "custom_nodes": [
                {
                    "type": "registry",
                    "id": "node",
                    "pre_install_scripts": ["before.sh"],
                }
            ]
        }
    }

    with pytest.raises(CustomNodesConfigError, match="require --scripts-dir"):
        build_custom_nodes_plan(document)

    with pytest.raises(CustomNodesConfigError, match="existing scripts directory"):
        build_custom_nodes_plan(document, scripts_dir=Path("/missing"))


@pytest.mark.parametrize(
    ("hook", "message"),
    [
        ("/absolute.sh", "must be relative"),
        ("../escape.py", "must not contain"),
        ("notes.txt", "must end in"),
        ("missing.sh", "must reference an existing file"),
    ],
)
def test_hooks_are_defensively_validated(
    tmp_path: Path,
    hook: str,
    message: str,
) -> None:
    """Repeat host-side hook path checks for generated helper TOML."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    document = {
        "comfyui": {
            "custom_nodes": [
                {
                    "type": "registry",
                    "id": "node",
                    "pre_install_scripts": [hook],
                }
            ]
        }
    }

    with pytest.raises(CustomNodesConfigError, match=message):
        build_custom_nodes_plan(document, scripts_dir=scripts)


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        (
            [
                {"type": "registry", "id": "node"},
                {"type": "registry", "id": "node"},
            ],
            "duplicates registry ID",
        ),
        (
            [
                {"type": "git", "url": "https://example.com/node.git"},
                {"type": "git", "url": "https://example.com/node.git"},
            ],
            "duplicates Git URL",
        ),
    ],
)
def test_duplicate_node_sources_are_rejected(
    nodes: list[dict[str, str]],
    message: str,
) -> None:
    """Defensively reject generated helper TOML with duplicate node sources."""
    with pytest.raises(CustomNodesConfigError, match=message):
        build_custom_nodes_plan({"comfyui": {"custom_nodes": nodes}})


def test_duplicate_git_target_directories_are_rejected() -> None:
    """Reject different Git URLs that would clone into the same directory."""
    nodes = [
        {"type": "git", "url": "https://example.com/shared.git"},
        {"type": "git", "url": "https://mirror.example.com/shared"},
    ]

    with pytest.raises(CustomNodesConfigError, match="duplicates Git target"):
        build_custom_nodes_plan({"comfyui": {"custom_nodes": nodes}})


@pytest.mark.parametrize(
    "target_dir",
    ["", ".", "..", "/absolute", "nested/node", "node name"],
)
def test_invalid_git_target_directories_are_rejected(target_dir: str) -> None:
    """Defensively reject helper TOML with unsafe explicit Git clone directories."""
    nodes = [
        {
            "type": "git",
            "url": "https://example.com/node.git",
            "target_dir": target_dir,
        }
    ]

    with pytest.raises(CustomNodesConfigError, match="invalid Git target"):
        build_custom_nodes_plan({"comfyui": {"custom_nodes": nodes}})


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "only a \\[comfyui\\] table"),
        ({"comfyui": [], "extra": {}}, "only a \\[comfyui\\] table"),
        ({"comfyui": []}, "\\[comfyui\\] must be a table"),
        ({"comfyui": {}}, "must contain only custom_nodes"),
        ({"comfyui": {"custom_nodes": {}, "extra": []}}, "must contain only"),
        ({"comfyui": {"custom_nodes": {}}}, "must be a list"),
        ({"comfyui": {"custom_nodes": [{"type": "registry"}]}}, "validation failed"),
    ],
)
def test_defensive_validation_rejects_malformed_helper_documents(
    document: dict[str, object],
    message: str,
) -> None:
    """Reject shapes outside the generated helper TOML contract."""
    with pytest.raises(CustomNodesConfigError, match=message):
        build_custom_nodes_plan(document)


def test_load_custom_nodes_plan_reports_missing_and_invalid_toml(
    tmp_path: Path,
) -> None:
    """Translate file and TOML errors into user-facing helper errors."""
    with pytest.raises(CustomNodesConfigError, match="does not exist"):
        load_custom_nodes_plan(tmp_path / "missing.toml")

    config = tmp_path / "bad.toml"
    config.write_text("[comfyui\n", encoding="utf-8")

    with pytest.raises(CustomNodesConfigError, match="not valid TOML"):
        load_custom_nodes_plan(config)
