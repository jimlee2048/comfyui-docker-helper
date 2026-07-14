"""Opt-in disposable-image acceptance for mixed custom-node installs."""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.docker, pytest.mark.slow]

_IMAGE_VARIABLE = "CDH_CUSTOM_NODE_IMAGE"

_GIT_PROOF_SOURCE = r"""
def require_real_directory(path):
    path = pathlib.Path(path)
    metadata = path.lstat()
    resolved = path.resolve(strict=True)
    assert not stat.S_ISLNK(metadata.st_mode)
    assert stat.S_ISDIR(metadata.st_mode)
    assert resolved == path
    return resolved

def git_output(repository, *arguments):
    return subprocess.run(
        ["git", "-C", repository, *arguments],
        check=True,
        capture_output=True,
    ).stdout

def absolute_git_path(repository, *arguments):
    output = git_output(repository, *arguments)
    assert output.endswith(b"\n")
    assert output.count(b"\n") == 1
    path = pathlib.Path(os.fsdecode(output[:-1]))
    assert path.is_absolute()
    return path

def git_directories(repository):
    actual = absolute_git_path(repository, "rev-parse", "--absolute-git-dir")
    common = absolute_git_path(
        repository,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    return actual, common

def verify_exact_repository_root(repository):
    top = git_output(
        repository,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
    )
    assert top == os.fsencode(repository) + b"\n"

def verify_exact_detached_head(repository, expected_commit):
    head = git_output(repository, "rev-parse", "--verify", "HEAD")
    assert head == expected_commit.encode("ascii") + b"\n"
    symbolic = subprocess.run(
        ["git", "-C", repository, "symbolic-ref", "-q", "HEAD"],
        check=False,
        capture_output=True,
    )
    assert symbolic.returncode == 1

def verify_root_git_directory(repository):
    dot_git = require_real_directory(repository / ".git")
    assert dot_git == repository / ".git"
    actual, common = git_directories(repository)
    assert actual == dot_git
    assert common == dot_git
    return dot_git

def require_contained_git_directory(path, root_git_directory):
    root_git_directory = require_real_directory(root_git_directory)
    assert path != root_git_directory
    relative = path.relative_to(root_git_directory)
    current = root_git_directory
    for part in relative.parts:
        current = current / part
        assert not stat.S_ISLNK(current.lstat().st_mode)
    return require_real_directory(path)

def verify_submodule_git_directory(repository, root_git_directory):
    dot_git = repository / ".git"
    metadata = dot_git.lstat()
    assert not stat.S_ISLNK(metadata.st_mode)
    assert stat.S_ISREG(metadata.st_mode)
    actual, common = git_directories(repository)
    assert actual == common
    require_contained_git_directory(actual, root_git_directory)

def verify_committed_gitlinks(
    repository,
    repository_root,
    custom_nodes_root,
    root_git_directory,
    seen,
):
    tree = subprocess.run(
        ["git", "-C", repository, "ls-tree", "-rz", "--full-tree", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata_fields, separator, raw_path = record.partition(b"\t")
        fields = metadata_fields.split(b" ", 2)
        assert separator == b"\t"
        assert len(fields) == 3
        mode, object_type, object_id = fields
        if mode != b"160000":
            continue
        assert object_type == b"commit"
        assert len(object_id) == 40
        assert all(character in b"0123456789abcdef" for character in object_id)
        path_text = os.fsdecode(raw_path)
        relative = pathlib.PurePosixPath(path_text)
        assert path_text
        assert not relative.is_absolute()
        assert relative.as_posix() == path_text
        assert all(part not in {"", ".", ".."} for part in relative.parts)
        child = repository.joinpath(*relative.parts)
        child = require_real_directory(child)
        assert child != repository
        assert child.is_relative_to(repository_root)
        assert child.is_relative_to(custom_nodes_root)
        assert child not in seen
        seen.add(child)
        verify_submodule_git_directory(child, root_git_directory)
        verify_exact_repository_root(child)
        verify_exact_detached_head(child, object_id.decode("ascii"))
        verify_committed_gitlinks(
            child,
            repository_root,
            custom_nodes_root,
            root_git_directory,
            seen,
        )

def prove_git_targets(root, nodes):
    root = require_real_directory(root)
    assert root.is_absolute()
    proven = []
    for node in nodes:
        if node["type"] != "git":
            continue
        target = pathlib.Path(node["target"])
        assert target.is_absolute()
        assert target.parent == root
        assert target.name not in {"", ".", ".."}
        assert re.fullmatch(r"[A-Za-z0-9._-]+", target.name) is not None
        assert target not in proven
        target = require_real_directory(target)
        verify_exact_repository_root(target)
        root_git_directory = verify_root_git_directory(target)
        verify_exact_detached_head(target, node["commit"])
        verify_committed_gitlinks(
            target,
            target,
            root,
            root_git_directory,
            {target},
        )
        proven.append(target)
    return frozenset(proven)
"""

_REGISTRY_PROOF_SOURCE = r"""
def scan_registry_projects_after_git_proof(root, nodes):
    root = require_real_directory(root)
    excluded_git_targets = prove_git_targets(root, nodes)
    assert all(target.parent == root for target in excluded_git_targets)
    projects = {}
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child in excluded_git_targets:
            continue
        child_metadata = child.lstat()
        assert not stat.S_ISLNK(child_metadata.st_mode)
        if stat.S_ISREG(child_metadata.st_mode):
            continue
        assert stat.S_ISDIR(child_metadata.st_mode)
        resolved_child = require_real_directory(child)
        assert resolved_child.parent == root
        project_file = child / "pyproject.toml"
        try:
            project_metadata = project_file.lstat()
        except FileNotFoundError:
            continue
        assert not stat.S_ISLNK(project_metadata.st_mode)
        assert stat.S_ISREG(project_metadata.st_mode)
        resolved_project = project_file.resolve(strict=True)
        assert resolved_project.parent == resolved_child
        assert resolved_project.is_relative_to(root)
        project = tomllib.loads(project_file.read_bytes().decode("utf-8"))["project"]
        normalized = canonicalize_name(project["name"], validate=True)
        assert normalized not in projects
        projects[normalized] = Version(project["version"])
    expected_projects = {}
    for node in nodes:
        if node["type"] != "registry":
            continue
        normalized = canonicalize_name(node["id"], validate=True)
        assert normalized not in expected_projects
        expected_projects[normalized] = Version(node["version"])
    assert projects == expected_projects
    return projects
"""

_CUSTOM_NODE_PROBE = r"""
set -eu

test ! -e /opt/uv/tools/comfy-cli
test ! -L /opt/uv/tools/comfy-cli
for command in comfy comfy-cli comfycli; do
  test ! -e "/opt/uv/bin/$command"
  test ! -L "/opt/uv/bin/$command"
done

inventory=/opt/cdh/build/custom-node-inventory.json
test -f "$inventory"
test ! -L "$inventory"
test "$(stat -c '%a' "$inventory")" = 444
test "$(stat -c '%u:%g' "$inventory")" = 0:0

/opt/venv/bin/python -I -c '
import importlib.metadata as metadata
import json
import os
import pathlib
import re
import stat
import subprocess
import tomllib

from packaging.utils import canonicalize_name
from packaging.version import Version

__GIT_PROOF_SOURCE__
__REGISTRY_PROOF_SOURCE__

build = pathlib.Path("/opt/cdh/build")
plan = json.loads(build.joinpath("build-plan.json").read_text())
phase = plan["custom_nodes"]
assert phase["user_directory"] == os.path.join(os.environ["COMFYUI_PATH"], "user")
assert phase["custom_node_inventory"] == str(
    build / "custom-node-inventory.json"
)
expected = phase["nodes"]
assert any(node["type"] == "registry" for node in expected)
assert any(node["type"] == "git" for node in expected)

def inventory_entry(node):
    if node["type"] == "registry":
        return {
            "type": "registry",
            "id": node["id"],
            "version": node["version"],
            "verification": "registry-version",
            "control": "direct-cm-cli",
        }
    return {
        "type": "git",
        "url": node["url"],
        "commit": node["commit"],
        "target": pathlib.Path(node["target"]).name,
        "verification": "git-commit",
        "control": "direct-git",
    }

expected_inventory = {
    "schema_version": 1,
    "nodes": [inventory_entry(node) for node in expected],
}
expected_bytes = (
    json.dumps(
        expected_inventory,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    + "\n"
).encode("utf-8")
observed_bytes = build.joinpath("custom-node-inventory.json").read_bytes()
assert observed_bytes == expected_bytes
observed = json.loads(observed_bytes)
for item in observed["nodes"]:
    if item["type"] == "registry":
        assert set(item) == {"type", "id", "version", "verification", "control"}
    else:
        assert set(item) == {
            "type", "url", "commit", "target", "verification", "control"
        }

root = pathlib.Path(os.environ["COMFYUI_PATH"]) / "custom_nodes"
scan_registry_projects_after_git_proof(root, expected)

owners = [
    distribution.metadata["Name"]
    for distribution in metadata.distributions()
    for item in distribution.entry_points
    if item.group == "console_scripts" and item.name == "cm-cli"
]
assert owners == ["comfyui-manager"]
'

uv --no-config pip check --python /opt/venv/bin/python --no-python-downloads
""".replace("__GIT_PROOF_SOURCE__", _GIT_PROOF_SOURCE).replace(
    "__REGISTRY_PROOF_SOURCE__", _REGISTRY_PROOF_SOURCE
)


def test_custom_node_image_has_exact_installed_state_and_inventory() -> None:
    image = os.environ.get(_IMAGE_VARIABLE)
    if not image:
        pytest.skip(f"set {_IMAGE_VARIABLE} to a locally built acceptance image")
    name = f"cdh-custom-node-smoke-{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--name",
                name,
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                _CUSTOM_NODE_PROBE,
            ],
            check=True,
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
