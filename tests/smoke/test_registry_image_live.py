"""Opt-in disposable-image acceptance for direct Manager Registry installs."""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.docker, pytest.mark.slow]

_IMAGE_VARIABLE = "CDH_REGISTRY_IMAGE"

_REGISTRY_PROBE = r"""
set -eu

test ! -e /opt/uv/tools/comfy-cli
test ! -L /opt/uv/tools/comfy-cli
for command in comfy comfy-cli comfycli; do
  test ! -e "/opt/uv/bin/$command"
  test ! -L "/opt/uv/bin/$command"
done

inventory=/opt/cdh/build/registry-inventory.json
test -f "$inventory"
test ! -L "$inventory"
test "$(stat -c '%a' "$inventory")" = 444
test "$(stat -c '%u:%g' "$inventory")" = 0:0

/opt/venv/bin/python -I -c '
import importlib.metadata as metadata
import json
import os
import pathlib
import tomllib

from packaging.utils import canonicalize_name
from packaging.version import Version

build = pathlib.Path("/opt/cdh/build")
plan = json.loads(build.joinpath("build-plan.json").read_text())
phase = plan["custom_nodes"]
assert phase["user_directory"] == os.path.join(os.environ["COMFYUI_PATH"], "user")
assert phase["registry_inventory"] == str(
    build / "registry-inventory.json"
)
expected = [node for node in phase["nodes"] if node["type"] == "registry"]
assert expected
expected_inventory = {
    "schema_version": 1,
    "nodes": [
        {
            "type": "registry",
            "id": node["id"],
            "version": node["version"],
            "verification": "registry-version",
            "control": "direct-cm-cli",
        }
        for node in expected
    ],
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
observed_bytes = build.joinpath("registry-inventory.json").read_bytes()
assert observed_bytes == expected_bytes
observed = json.loads(observed_bytes)
assert all(
    set(item) == {"type", "id", "version", "verification", "control"}
    for item in observed["nodes"]
)

root = pathlib.Path(os.environ["COMFYUI_PATH"]) / "custom_nodes"
projects = {}
for child in root.iterdir():
    project_file = child / "pyproject.toml"
    if not child.is_dir() or not project_file.is_file():
        continue
    project = tomllib.loads(project_file.read_text())["project"]
    normalized = canonicalize_name(project["name"])
    assert normalized not in projects
    projects[normalized] = Version(project["version"])
for node in expected:
    assert projects[canonicalize_name(node["id"])] == Version(node["version"])

owners = [
    distribution.metadata["Name"]
    for distribution in metadata.distributions()
    for item in distribution.entry_points
    if item.group == "console_scripts" and item.name == "cm-cli"
]
assert owners == ["comfyui-manager"]
'

uv --no-config pip check --python /opt/venv/bin/python --no-python-downloads
"""


def test_registry_image_has_exact_installed_state_and_inventory() -> None:
    image = os.environ.get(_IMAGE_VARIABLE)
    if not image:
        pytest.skip(f"set {_IMAGE_VARIABLE} to a locally built acceptance image")
    name = f"cdh-registry-smoke-{uuid.uuid4().hex[:12]}"
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
                _REGISTRY_PROBE,
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
