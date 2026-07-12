"""Container-side custom-node helper planning tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from tests.artifact_helpers import COMMIT_A, make_lockfile, write_root_artifacts

from comfyui_docker_helper.config import (
    Config,
    GitLockedCustomNode,
    RegistryLockedCustomNode,
    dump_lockfile_toml,
)
from comfyui_docker_helper.container.custom_nodes import (
    CustomNodesConfigError,
    build_custom_nodes_plan,
    load_custom_nodes_plan,
)
from comfyui_docker_helper.container.root_config import custom_nodes_document


def test_load_custom_nodes_plan_preserves_order_targets_and_cache_flag(
    tmp_path: Path,
) -> None:
    """Build a deterministic plan from config metadata and locked selections."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre-a.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "pre-b.py").write_text("pass\n", encoding="utf-8")
    (scripts / "post.py").write_text("pass\n", encoding="utf-8")
    config, lock = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "latest"

[[comfyui.custom_nodes]]
type = "registry"
id = "first"
version = "latest"
pre_install_scripts = ["pre-a.sh", "pre-b.py"]
post_install_scripts = []

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/second.git"
ref = "main"
target_dir = "second-custom"
pre_install_scripts = []
post_install_scripts = ["post.py"]

[[comfyui.custom_nodes]]
type = "registry"
id = "third"
pre_install_scripts = []
post_install_scripts = []
""".lstrip(),
    )
    parsed_config = Config.model_validate(tomllib.loads(config.read_text()))
    lockfile = make_lockfile(parsed_config).model_copy(
        update={
            "custom_nodes": [
                RegistryLockedCustomNode(
                    type="registry",
                    id="first",
                    version="2.0.0",
                ),
                GitLockedCustomNode(
                    type="git",
                    url="https://example.com/second.git",
                    commit=COMMIT_A,
                ),
                RegistryLockedCustomNode(
                    type="registry",
                    id="third",
                    version="3.0.0",
                ),
            ]
        }
    )
    lock.write_text(dump_lockfile_toml(lockfile), encoding="utf-8")

    plan = load_custom_nodes_plan(config, lock, scripts_dir=scripts)

    assert plan.update_cache is True
    assert plan.has_hooks is True
    assert plan.scripts_source_dir == scripts.resolve()
    assert [node.type for node in plan.items] == ["registry", "git", "registry"]
    assert [node.target for node in plan.items] == [
        "first@2.0.0",
        f"https://example.com/second.git@{COMMIT_A}",
        "third@3.0.0",
    ]
    assert plan.items[0].version == "2.0.0"
    assert plan.items[1].ref == COMMIT_A
    assert plan.items[0].pre_install_scripts == ("pre-a.sh", "pre-b.py")
    assert plan.items[1].target_dir == "second-custom"
    assert plan.items[1].post_install_scripts == ("post.py",)


def test_custom_nodes_document_overlays_lock_entries_by_source_in_config_order(
    tmp_path: Path,
) -> None:
    """Lock entries are source-matched while config order is preserved."""
    config, _lock = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "latest"

[[comfyui.custom_nodes]]
type = "registry"
id = "first"
version = "latest"

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/second.git"
ref = "main"
target_dir = "second-custom"

[[comfyui.custom_nodes]]
type = "registry"
id = "third"
""".lstrip(),
    )
    parsed_config = Config.model_validate(tomllib.loads(config.read_text()))
    lockfile = make_lockfile(parsed_config).model_copy(
        update={
            "custom_nodes": [
                RegistryLockedCustomNode(
                    type="registry",
                    id="third",
                    version="3.0.0",
                ),
                GitLockedCustomNode(
                    type="git",
                    url="https://example.com/second.git",
                    commit=COMMIT_A,
                ),
                RegistryLockedCustomNode(
                    type="registry",
                    id="first",
                    version="2.0.0",
                ),
            ]
        }
    )

    document = custom_nodes_document(parsed_config, lockfile)
    root = document["comfyui"]
    assert isinstance(root, dict)
    nodes = root["custom_nodes"]
    assert isinstance(nodes, list)

    assert [
        (
            node["type"],
            node.get("id") or node.get("url"),
            node.get("version") or node.get("ref"),
        )
        for node in nodes
    ] == [
        ("registry", "first", "2.0.0"),
        ("git", "https://example.com/second.git", COMMIT_A),
        ("registry", "third", "3.0.0"),
    ]
    assert nodes[1]["target_dir"] == "second-custom"


@pytest.mark.parametrize(
    ("lock_nodes", "diagnostic_code"),
    [
        (
            [
                GitLockedCustomNode(
                    type="git",
                    url="https://example.com/second.git",
                    commit=COMMIT_A,
                ),
            ],
            "lockfile.registry_missing",
        ),
        (
            [
                RegistryLockedCustomNode(
                    type="registry",
                    id="first",
                    version="1.0.0",
                ),
            ],
            "lockfile.git_missing",
        ),
    ],
)
def test_load_custom_nodes_plan_reports_missing_lock_entries_before_extraction(
    tmp_path: Path,
    lock_nodes: list[RegistryLockedCustomNode | GitLockedCustomNode],
    diagnostic_code: str,
) -> None:
    """Fail at root artifact compatibility instead of raw lock lookup."""
    config, lock = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "latest"

[[comfyui.custom_nodes]]
type = "registry"
id = "first"

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/second.git"
ref = "main"
""".lstrip(),
    )
    parsed_config = Config.model_validate(tomllib.loads(config.read_text()))
    lockfile = make_lockfile(parsed_config).model_copy(
        update={"custom_nodes": lock_nodes}
    )
    lock.write_text(dump_lockfile_toml(lockfile), encoding="utf-8")

    with pytest.raises(CustomNodesConfigError) as error:
        load_custom_nodes_plan(config, lock)

    message = str(error.value)
    assert "root lock is incompatible with root config" in message
    assert diagnostic_code in message


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
    """Repeat host-side hook path checks for extracted custom-node views."""
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
    """Defensively reject extracted custom-node views with duplicate sources."""
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
    ("field", "value", "message"),
    [
        ("url", "", "Git URL at item 0 must be non-empty"),
        ("url", "https://example.com/node.git\nprobe", "Git URL at item 0"),
        ("ref", "", "Git ref at item 0 must be non-empty"),
        ("ref", "main\x7fprobe", "Git ref at item 0"),
    ],
)
def test_container_git_argv_values_reject_empty_and_controls(
    field: str,
    value: str,
    message: str,
) -> None:
    node = {"type": "git", "url": "https://example.com/node.git"}
    node[field] = value

    with pytest.raises(CustomNodesConfigError, match=message):
        build_custom_nodes_plan({"comfyui": {"custom_nodes": [node]}})


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
    """Reject shapes outside the extracted custom-node view contract."""
    with pytest.raises(CustomNodesConfigError, match=message):
        build_custom_nodes_plan(document)


def test_load_custom_nodes_plan_reports_missing_and_invalid_toml(
    tmp_path: Path,
) -> None:
    """Translate file and TOML errors into user-facing helper errors."""
    with pytest.raises(CustomNodesConfigError, match="does not exist"):
        load_custom_nodes_plan(tmp_path / "missing.toml", tmp_path / "missing.lock")

    config = tmp_path / "bad.toml"
    config.write_text("[comfyui\n", encoding="utf-8")
    lock = tmp_path / "config.lock.toml"
    lock.write_text("", encoding="utf-8")

    with pytest.raises(CustomNodesConfigError, match="not valid TOML"):
        load_custom_nodes_plan(config, lock)
