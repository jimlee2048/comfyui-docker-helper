"""Opt-in component proof for Git build-hook ordering and patched installation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
import uuid
from pathlib import Path

import pytest
from tests.acceptance_scenarios import ACCEPTANCE_SCENARIOS
from tests.project_paths import FIXTURES_ROOT
from tests.smoke.application_probes import GIT_PROOF_SOURCE

from comfyui_docker_helper.config.planning.build_plan import (
    BuildPlan,
    GitNodePlan,
    build_plan_hook_identities,
    manifest_binding,
    parse_build_plan_json,
)
from comfyui_docker_helper.config.planning.canonical_lock import (
    dump_canonical_lock_toml,
    parse_canonical_lock_toml,
)
from comfyui_docker_helper.host.context.wheel import build_canonical_wheel

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.network,
    pytest.mark.docker,
    pytest.mark.slow,
]

_SCENARIO = next(item for item in ACCEPTANCE_SCENARIOS if item.id == "hooks")
_FIXTURE_ROOT = FIXTURES_ROOT / "comfyui-build"
_PLAN_MOUNT = "/run/cdh-build-hooks-plan.json"

_IMAGE_PROBE = r"""
import hashlib
import importlib.metadata
import json
import os
import pathlib
import re
import stat
import subprocess

from comfyui_docker_helper.config.evidence.custom_nodes import custom_node_inventory
from comfyui_docker_helper.config.evidence.manifest import (
    dump_final_manifest, parse_final_manifest,
)
from comfyui_docker_helper.config.planning.build_plan import (
    build_plan_hook_identities, manifest_binding, parse_build_plan_json,
)

__GIT_PROOF_SOURCE__

plan_path = pathlib.Path("/run/cdh-build-hooks-plan.json")
plan = parse_build_plan_json(plan_path.read_bytes())
build = pathlib.Path("/opt/cdh/build")
manifest_bytes = (build / "manifest.json").read_bytes()
manifest = parse_final_manifest(manifest_bytes)
assert dump_final_manifest(manifest) == manifest_bytes
assert manifest.binding == manifest_binding(plan)
assert manifest.toolchain.cdh.wheel_digest == plan.toolchain.tool_store.cdh.wheel_digest
assert importlib.metadata.version("comfyui-docker-helper") == (
    plan.toolchain.tool_store.cdh.version
)
assert manifest.custom_nodes == custom_node_inventory(plan.custom_nodes.nodes)

build_hooks, runtime_hooks = build_plan_hook_identities(plan.custom_nodes, plan.runtime)
expected_hooks = {}
for hook in build_hooks.values():
    path = build / "hooks" / hook.relative_path
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o755
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == hook.digest
    expected_hooks[("build", hook.relative_path)] = digest
assert {
    (hook.domain, hook.relative_path): (hook.intended_digest, hook.observed_digest)
    for hook in manifest.hooks
} == {key: (digest, digest) for key, digest in expected_hooks.items()}

comfyui = pathlib.Path(plan.application.paths.comfyui)
nodes = [node.model_dump(mode="json") for node in plan.custom_nodes.nodes]
prove_git_targets(comfyui / "custom_nodes", nodes)
node = pathlib.Path(nodes[0]["target"])
version = subprocess.run(
    [str(pathlib.Path(plan.application.paths.venv) / "bin/python"), "-I", "-c",
     "import importlib.metadata; print(importlib.metadata.version('pyfiglet'))"],
    check=True, capture_output=True, text=True, timeout=30,
).stdout.strip()
assert version == "1.0.2"
assert {item.name: item.version for item in manifest.application.inventory}[
    "pyfiglet"
] == version
assert json.loads((node / "cdh-hook-install.json").read_text()) == {"pyfiglet": version}
assert (node / "requirements.txt").read_text() == "pyfiglet==1.0.2\n"
stages = (comfyui / "cdh-git-hook-probe.log").read_text().splitlines()
assert stages == ["pre-clone", "pre-install", "install.py", "post-install"]
print(json.dumps({
    "binding": manifest.binding.model_dump(mode="json"),
    "wheel_digest": manifest.toolchain.cdh.wheel_digest,
    "stages": stages,
    "pyfiglet": version,
    "hook_digests": {
        name: digest for (_domain, name), digest in expected_hooks.items()
    },
    "git_commit": nodes[0]["commit"],
}, sort_keys=True))
""".replace("__GIT_PROOF_SOURCE__", GIT_PROOF_SOURCE)


def _environment(name: str | None) -> str:
    assert name is not None
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"build-hook component requires environment input {name}")
    return value


def _current_build_plan(context: Path) -> BuildPlan:
    plan = parse_build_plan_json((context / "build-plan.json").read_bytes())
    current_wheel = build_canonical_wheel()
    if current_wheel.digest != plan.toolchain.tool_store.cdh.wheel_digest:
        pytest.fail("build-hook context does not match the current cdh package")
    return plan


def test_formal_image_consumes_pre_install_patch_after_source_preparation() -> None:
    image = _environment(_SCENARIO.image_variable)
    context = Path(_environment(_SCENARIO.context_variable)).resolve(strict=True)
    assert (context / ".cdh-rendered").is_file()
    assert (context / "Dockerfile").is_file()
    plan = _current_build_plan(context)
    lock_bytes = (context / "config.lock.toml").read_bytes()
    lock = parse_canonical_lock_toml(lock_bytes)
    assert dump_canonical_lock_toml(lock).encode() == lock_bytes
    assert plan.lock_digest == f"sha256:{hashlib.sha256(lock_bytes).hexdigest()}"
    fixture = tomllib.loads((_FIXTURE_ROOT / "configs" / _SCENARIO.fixture).read_text())
    expected = fixture["comfyui"]["custom_nodes"][0]
    assert plan.application.comfyui.formal_release == fixture["comfyui"]["version"]
    assert plan.application.comfyui.commit == lock.comfyui.commit
    assert plan.toolchain.python.version == _SCENARIO.python_version
    assert plan.application.comfyui.manager is None
    assert plan.toolchain.tool_store.comfy_cli is None
    assert len(plan.custom_nodes.nodes) == 1
    node = plan.custom_nodes.nodes[0]
    assert isinstance(node, GitNodePlan)
    assert node.url == expected["url"]
    assert node.commit == expected["ref"]
    assert Path(node.target).name == expected["target_dir"]
    assert [(item.url, item.commit) for item in lock.custom_nodes.git] == [
        (node.url, node.commit)
    ]
    for field in ("pre_clone_hooks", "pre_install_hooks", "post_install_hooks"):
        assert [item["relative_path"] for item in node.model_dump()[field]] == (
            expected[field]
        )
    build_hooks, runtime_hooks = build_plan_hook_identities(
        plan.custom_nodes, plan.runtime
    )
    assert not runtime_hooks
    assert {item.relative_path: item.digest for item in lock.hooks.build} == {
        item.relative_path: item.digest for item in build_hooks.values()
    }
    for hook in build_hooks.values():
        source = _FIXTURE_ROOT / "build-hooks" / hook.relative_path
        materialized = context / "build/hooks" / hook.relative_path
        assert source.read_bytes() == materialized.read_bytes()
        assert f"sha256:{hashlib.sha256(materialized.read_bytes()).hexdigest()}" == (
            hook.digest
        )
    wheel = context / (
        "bootstrap/comfyui_docker_helper-"
        f"{plan.toolchain.tool_store.cdh.version}-py3-none-any.whl"
    )
    assert f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}" == (
        plan.toolchain.tool_store.cdh.wheel_digest
    )
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    name = f"cdh-build-hooks-{uuid.uuid4().hex[:12]}"
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--mount",
        f"type=bind,source={context / 'build-plan.json'},target={_PLAN_MOUNT},readonly",
        "--entrypoint",
        "/opt/uv/tools/comfyui-docker-helper/bin/python",
        image_id,
        "-I",
        "-c",
        _IMAGE_PROBE,
    ]
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=120
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        evidence = json.loads(completed.stdout)
        assert evidence["binding"] == manifest_binding(plan).model_dump(mode="json")
        print(json.dumps({"image_id": image_id, **evidence}, sort_keys=True))
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
