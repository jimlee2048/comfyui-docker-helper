"""Deterministic checks for the mixed custom-node image smoke harness."""

from __future__ import annotations

import os
import pathlib
import re
import stat
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


def _add_submodule(parent: Path, source: Path, relative: str, commit: str) -> None:
    _git(
        parent,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--",
        str(source),
        relative,
    )
    checkout = parent / relative
    _git(checkout, "checkout", "--detach", commit)
    _git(parent, "add", ".gitmodules", relative)
    _git(parent, "commit", "-m", f"add {relative}")


def _recursive_gitlink_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, str, str]:
    sources = tmp_path / "sources"
    sources.mkdir()
    leaf_source = sources / "leaf"
    _repository(leaf_source)
    first = _commit(leaf_source, "first\n")
    second = _commit(leaf_source, "second\n")
    middle_source = sources / "middle"
    _repository(middle_source)
    _commit(middle_source, "middle\n")
    _add_submodule(middle_source, leaf_source, "nested/leaf", first)
    middle_commit = _git(middle_source, "rev-parse", "HEAD")
    root_source = sources / "root"
    _repository(root_source)
    _commit(root_source, "root\n")
    _add_submodule(root_source, middle_source, "deps/middle", middle_commit)
    root_commit = _git(root_source, "rev-parse", "HEAD")

    custom_nodes = tmp_path / "custom_nodes"
    custom_nodes.mkdir()
    root = custom_nodes / "root"
    subprocess.run(
        ("git", "clone", "--no-checkout", "--", str(root_source), str(root)),
        check=True,
        capture_output=True,
    )
    _git(root, "checkout", "--detach", root_commit, "--")
    _git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "--recursive",
        "--checkout",
    )
    middle = root / "deps/middle"
    leaf = middle / "nested/leaf"
    return root, middle, leaf, first, second


def _verify_committed_gitlinks(repository: Path) -> None:
    namespace = {
        "os": os,
        "pathlib": pathlib,
        "re": re,
        "stat": stat,
        "subprocess": subprocess,
    }
    exec(_GIT_PROOF_SOURCE, namespace)
    namespace["prove_git_targets"](
        repository.parent,
        [
            {
                "type": "git",
                "target": str(repository),
                "commit": _git(repository, "rev-parse", "HEAD"),
            }
        ],
    )


def _scan_registry_projects_after_git_proof(root: Path, nodes: list[dict]):
    namespace = {
        "Version": Version,
        "canonicalize_name": canonicalize_name,
        "os": os,
        "pathlib": pathlib,
        "re": re,
        "stat": stat,
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


def test_linked_worktree_cannot_be_a_proven_git_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _repository(source)
    commit = _commit(source, "source\n")
    custom_nodes = tmp_path / "custom_nodes"
    custom_nodes.mkdir()
    target = custom_nodes / "linked"
    _git(source, "worktree", "add", "--detach", str(target), commit)

    with pytest.raises(AssertionError):
        _verify_committed_gitlinks(target)


def test_wrong_head_valid_repository_cannot_be_excluded(tmp_path: Path) -> None:
    custom_nodes = tmp_path / "custom_nodes"
    custom_nodes.mkdir()
    target = custom_nodes / "node"
    _repository(target)
    first = _commit(target, "first\n")
    second = _commit(target, "second\n")
    _git(target, "checkout", "--detach", first)
    namespace = {
        "os": os,
        "pathlib": pathlib,
        "re": re,
        "stat": stat,
        "subprocess": subprocess,
    }
    exec(_GIT_PROOF_SOURCE, namespace)

    with pytest.raises(AssertionError):
        namespace["prove_git_targets"](
            custom_nodes,
            [{"type": "git", "target": str(target), "commit": second}],
        )


def test_attached_submodule_head_is_rejected(tmp_path: Path) -> None:
    root, middle, _leaf, _first, _second = _recursive_gitlink_fixture(tmp_path)
    _git(middle, "checkout", "main")

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


def test_registry_scan_rejects_immediate_symlink_outside(tmp_path: Path) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    outside = tmp_path / "outside"
    _write_project(outside, "outside", "1.0.0")
    root.joinpath("linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AssertionError):
        _scan_registry_projects_after_git_proof(root, [])


def test_registry_scan_rejects_immediate_special_file(tmp_path: Path) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    os.mkfifo(root / "special")

    with pytest.raises(AssertionError):
        _scan_registry_projects_after_git_proof(root, [])


def test_registry_scan_ignores_regular_files_and_directories_without_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    root.joinpath("regular.txt").write_text("ignored\n")
    root.joinpath("no-metadata").mkdir()

    assert _scan_registry_projects_after_git_proof(root, []) == {}


def test_registry_scan_rejects_pyproject_symlink(tmp_path: Path) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    child = root / "node"
    child.mkdir()
    outside = tmp_path / "outside.toml"
    outside.write_text('[project]\nname = "node"\nversion = "1.0.0"\n')
    child.joinpath("pyproject.toml").symlink_to(outside)

    with pytest.raises(AssertionError):
        _scan_registry_projects_after_git_proof(root, [])


@pytest.mark.parametrize("case", ["unknown", "missing", "version"])
def test_registry_scan_requires_exact_identity_and_version_set(
    tmp_path: Path,
    case: str,
) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    nodes = [{"type": "registry", "id": "expected", "version": "1.0.0"}]
    if case == "unknown":
        _write_project(root / "expected", "expected", "1.0.0")
        _write_project(root / "unknown", "unknown", "1.0.0")
    elif case == "version":
        _write_project(root / "expected", "expected", "2.0.0")

    with pytest.raises(AssertionError):
        _scan_registry_projects_after_git_proof(root, nodes)
