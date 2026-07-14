"""Deterministic checks for the mixed custom-node image smoke harness."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.utils import canonicalize_name
from packaging.version import Version
from tests.smoke.test_custom_node_image_live import (
    _CUSTOM_NODE_PROBE,
    _GIT_PROOF_SOURCE,
    _REGISTRY_PROOF_SOURCE,
)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", repository, *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "cdh tests")


def _commit(repository: Path, content: str) -> str:
    repository.joinpath("content.txt").write_text(content)
    _git(repository, "add", "content.txt")
    _git(repository, "commit", "-m", content.strip())
    return _git(repository, "rev-parse", "HEAD")


def _recursive_gitlink_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, str, str]:
    root = tmp_path / "root"
    _repository(root)
    middle = root / "deps/middle"
    _repository(middle)
    leaf = middle / "nested/leaf"
    _repository(leaf)
    first = _commit(leaf, "first\n")
    second = _commit(leaf, "second\n")
    _git(leaf, "checkout", "--detach", first)
    _git(
        middle,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{first},nested/leaf",
    )
    _git(middle, "commit", "-m", "add leaf gitlink")
    middle_commit = _git(middle, "rev-parse", "HEAD")
    _git(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{middle_commit},deps/middle",
    )
    _git(root, "commit", "-m", "add middle gitlink")
    return root, middle, leaf, first, second


def _verify_committed_gitlinks(repository: Path) -> None:
    namespace = {
        "os": os,
        "pathlib": pathlib,
        "subprocess": subprocess,
    }
    exec(_GIT_PROOF_SOURCE, namespace)
    namespace["verify_committed_gitlinks"](repository)


def _scan_registry_projects_after_git_proof(root: Path, nodes: list[dict]):
    namespace = {
        "Version": Version,
        "canonicalize_name": canonicalize_name,
        "os": os,
        "pathlib": pathlib,
        "subprocess": subprocess,
        "tomllib": tomllib,
    }
    exec(_GIT_PROOF_SOURCE, namespace)
    exec(_REGISTRY_PROOF_SOURCE, namespace)
    return namespace["scan_registry_projects_after_git_proof"](root, nodes)


def _write_project(path: Path, name: str, version: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.joinpath("pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )


def test_custom_node_image_probe_has_valid_shell_and_python_syntax() -> None:
    completed = subprocess.run(
        ["/bin/sh", "-n"],
        input=_CUSTOM_NODE_PROBE,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    python_source = _CUSTOM_NODE_PROBE.split("/opt/venv/bin/python -I -c '\n", 1)[
        1
    ].split("\n'\n\nuv ", 1)[0]
    compile(python_source, "<custom-node-image-probe>", "exec")


def test_committed_gitlink_proof_allows_recursive_index_and_worktree_drift(
    tmp_path: Path,
) -> None:
    root, middle, leaf, _first, second = _recursive_gitlink_fixture(tmp_path)
    _git(
        middle,
        "update-index",
        "--cacheinfo",
        f"160000,{second},nested/leaf",
    )
    leaf.joinpath("content.txt").write_text("dirty worktree\n")

    _verify_committed_gitlinks(root)


def test_index_and_child_drift_cannot_replace_committed_recursive_gitlink(
    tmp_path: Path,
) -> None:
    root, middle, leaf, _first, second = _recursive_gitlink_fixture(tmp_path)
    _git(
        middle,
        "update-index",
        "--cacheinfo",
        f"160000,{second},nested/leaf",
    )
    _git(leaf, "checkout", "--detach", second)

    with pytest.raises(AssertionError):
        _verify_committed_gitlinks(root)


def test_proven_metadata_bearing_git_target_is_excluded_from_registry_scan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    git_target = root / "git-node"
    _repository(git_target)
    _write_project(git_target, "Shared_Node", "9.0.0")
    _git(git_target, "add", "pyproject.toml")
    _git(git_target, "commit", "-m", "add project metadata")
    commit = _git(git_target, "rev-parse", "HEAD")
    _git(git_target, "checkout", "--detach", commit)
    _write_project(root / "registry-node", "shared-node", "1.2.3")
    nodes = [
        {
            "type": "git",
            "target": str(git_target),
            "commit": commit,
        },
        {
            "type": "registry",
            "id": "shared-node",
            "version": "1.2.3",
        },
    ]

    projects = _scan_registry_projects_after_git_proof(root, nodes)

    assert projects == {"shared-node": Version("1.2.3")}


def test_unproven_build_plan_git_path_cannot_exclude_project_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    fake_git_target = root / "fake-git"
    _write_project(fake_git_target, "shared-node", "9.0.0")
    _write_project(root / "registry-node", "shared-node", "1.2.3")
    nodes = [
        {
            "type": "git",
            "target": str(fake_git_target),
            "commit": "1" * 40,
        },
        {
            "type": "registry",
            "id": "shared-node",
            "version": "1.2.3",
        },
    ]

    with pytest.raises(subprocess.CalledProcessError):
        _scan_registry_projects_after_git_proof(root, nodes)


def test_duplicate_registry_project_identity_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    _write_project(root / "first", "duplicate_node", "1.0.0")
    _write_project(root / "second", "duplicate-node", "1.0.0")

    with pytest.raises(AssertionError):
        _scan_registry_projects_after_git_proof(root, [])
