"""Opt-in formal-renderer local-node capture, installation, and CPU registration."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from tests.acceptance_scenarios import ACCEPTANCE_SCENARIOS

from comfyui_docker_helper.config.planning.build_plan import (
    LocalNodePlan,
    manifest_binding,
    parse_build_plan_json,
)
from comfyui_docker_helper.host.context.wheel import build_canonical_wheel

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.network,
    pytest.mark.docker,
    pytest.mark.slow,
]

_SCENARIO = next(item for item in ACCEPTANCE_SCENARIOS if item.id == "local-node")
_PLAN_MOUNT = "/run/cdh-local-node-plan.json"

_IMAGE_PROBE = r"""
import hashlib
import json
import pathlib
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from comfyui_docker_helper.config.evidence.custom_nodes import custom_node_inventory
from comfyui_docker_helper.config.evidence.manifest import parse_final_manifest
from comfyui_docker_helper.config.planning.build_plan import (
    manifest_binding, parse_build_plan_json,
)

plan = parse_build_plan_json(pathlib.Path('/run/cdh-local-node-plan.json').read_bytes())
manifest = parse_final_manifest(
    pathlib.Path('/opt/cdh/build/manifest.json').read_bytes()
)
assert manifest.binding == manifest_binding(plan)
assert manifest.custom_nodes == custom_node_inventory(plan.custom_nodes.nodes)
assert manifest.toolchain.cdh.wheel_digest == plan.toolchain.tool_store.cdh.wheel_digest
node = pathlib.Path(plan.custom_nodes.nodes[0].target)
assert stat.S_ISDIR(node.lstat().st_mode)
assert node.resolve(strict=True) == node
assert not (node / '.git').exists()
assert not (node / 'ignored').exists()
assert not (node / '.venv').exists()
assert (node / '.dockerignore').is_file()
revision = (node / 'revision.txt').read_text().strip()
installed = json.loads((node / 'installed.json').read_text())
assert installed == {'pyfiglet': '1.0.2', 'revision': revision}
assert (node / 'stages.txt').read_text().splitlines() == [
    'pre-install', 'install.py', 'post-install',
]
assert (node / 'requirements.txt').read_text() == (
    '# Applied by files after installation; no automatic reinstall.\n'
)
assert len(manifest.files) == 1
file_evidence = manifest.files[0]
assert file_evidence.target == str(node / 'requirements.txt')
assert file_evidence.observed_checksum == (
    'sha256:' + hashlib.sha256((node / 'requirements.txt').read_bytes()).hexdigest()
)
assert {item.name: item.version for item in manifest.application.inventory}[
    'pyfiglet'
] == '1.0.2'
python = str(pathlib.Path(plan.application.paths.venv) / 'bin/python')
version = subprocess.run(
    [python, '-I', '-c',
     "import importlib.metadata; print(importlib.metadata.version('pyfiglet'))"],
    check=True, capture_output=True, text=True, timeout=30,
).stdout.strip()
assert version == installed['pyfiglet']
comfyui = pathlib.Path(plan.application.paths.comfyui)
with tempfile.TemporaryFile(mode='w+t') as log:
    process = subprocess.Popen(
        [python, 'main.py', '--cpu', '--listen', '127.0.0.1', '--port', '8199',
         '--disable-auto-launch'],
        cwd=comfyui, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 180
        while True:
            assert process.poll() is None, 'ComfyUI exited before registration'
            try:
                with urllib.request.urlopen(
                    'http://127.0.0.1:8199/object_info/CDHLocalProbe', timeout=3,
                ) as response:
                    registered = json.load(response)
                if 'CDHLocalProbe' in registered:
                    break
            except (urllib.error.URLError, TimeoutError):
                pass
            assert time.monotonic() < deadline, 'local node registration timed out'
            time.sleep(1)
        choices = registered['CDHLocalProbe']['input']['required']['revision'][0]
        assert choices == [revision]
    except BaseException:
        log.seek(0)
        print(log.read())
        raise
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
print(json.dumps({
    'binding': manifest.binding.model_dump(mode='json'),
    'revision': revision,
    'wheel_digest': manifest.toolchain.cdh.wheel_digest,
    'installed': installed,
    'registered': choices,
}, sort_keys=True))
"""


def _environment(name: str | None) -> str:
    assert name is not None
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"local-node component requires environment input {name}")
    return value


def test_formal_local_node_image_installs_and_registers_captured_revision() -> None:
    image = _environment(_SCENARIO.image_variable)
    context = Path(_environment(_SCENARIO.context_variable)).resolve(strict=True)
    assert (context / ".cdh-rendered").is_file()
    assert (context / "Dockerfile").is_file()
    plan = parse_build_plan_json((context / "build-plan.json").read_bytes())
    assert build_canonical_wheel().digest == plan.toolchain.tool_store.cdh.wheel_digest
    lock = (context / "config.lock.toml").read_bytes()
    assert plan.lock_digest == f"sha256:{hashlib.sha256(lock).hexdigest()}"
    assert plan.toolchain.python.version == _SCENARIO.python_version
    assert plan.application.comfyui.manager is None
    assert plan.toolchain.tool_store.comfy_cli is None
    assert len(plan.custom_nodes.nodes) == 1
    node = plan.custom_nodes.nodes[0]
    assert isinstance(node, LocalNodePlan)
    assert node.target.rsplit("/", 1)[-1] == "local-probe"
    source = context / node.context_path
    assert not (source / "ignored").exists()
    assert (source / ".dockerignore").is_file()
    revision = (source / "revision.txt").read_text().strip()
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    name = f"cdh-local-node-{uuid.uuid4().hex[:12]}"
    try:
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--mount",
                (
                    f"type=bind,source={context / 'build-plan.json'},"
                    f"target={_PLAN_MOUNT},readonly"
                ),
                "--entrypoint",
                "/opt/uv/tools/comfyui-docker-helper/bin/python",
                image_id,
                "-I",
                "-c",
                _IMAGE_PROBE,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=260,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        evidence = json.loads(completed.stdout)
        assert evidence["binding"] == manifest_binding(plan).model_dump(mode="json")
        assert evidence["revision"] == revision
        assert evidence["registered"] == [revision]
        print(json.dumps({"image_id": image_id, **evidence}, sort_keys=True))
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
