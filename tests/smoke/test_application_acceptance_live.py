"""Opt-in durable acceptance for the complete ComfyUI application image."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import tomllib
import uuid
from pathlib import Path

import pytest
from tests.acceptance_scenarios import (
    RELEASE_SCENARIOS,
    AcceptanceProbe,
    AcceptanceScenario,
    Capability,
)
from tests.smoke.application_probes import (
    COMFY_CLI_BRIDGE_PROBE,
    GIT_PROOF_SOURCE,
    REGISTRY_PROOF_SOURCE,
)

from comfyui_docker_helper.config.build_plan import (
    build_plan_digest,
    manifest_binding,
    parse_build_plan_json,
)
from comfyui_docker_helper.config.canonical_lock import (
    dump_canonical_lock_toml,
    parse_canonical_lock_toml,
)

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.acceptance,
    pytest.mark.docker,
    pytest.mark.network,
    pytest.mark.slow,
]

_COMFYUI_COMMIT = "09725967cf76304371c390ca1d6483e04061da48"
_CUSTOM_SCRIPTS_URL = "https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git"
_CUSTOM_SCRIPTS_COMMIT = "609f3afaa74b2f88ef9ce8d939626065e3247469"
_SUBMODULE_URL = "https://github.com/receyuki/comfyui-prompt-reader-node.git"
_SUBMODULE_COMMIT = "a70cbb0c8d1208a01c0eea72e8f2c3668cac3ba7"
_SUBMODULE_TARGET = "comfyui-prompt-reader-node"
_SUBMODULE_GITLINK_PATH = "stable_diffusion_prompt_reader"
_SUBMODULE_GITLINK_COMMIT = "1a499becb0a88fd28ac3e4e09bd8917ce95c9629"

_INVENTORY_OBSERVER_SOURCE = r"""import importlib.metadata as metadata
import json
import pathlib
import sys

from packaging.utils import canonicalize_name
from packaging.version import Version

observed = {}
for distribution in metadata.distributions():
    raw_name = distribution.metadata["Name"]
    name = canonicalize_name(raw_name, validate=True)
    version = str(Version(distribution.version))
    assert name not in observed
    observed[name] = version
items = sorted(observed.items())
rows = [f"{name}=={version}" for name, version in items]
expected = ("\n".join(rows) + "\n").encode()
content = pathlib.Path(sys.argv[1]).read_bytes()
assert content.endswith(b"\n")
assert content == expected
print(json.dumps(items, ensure_ascii=True, separators=(",", ":")))
"""

_PYTHON_ROUTING_PROOF_SOURCE = r"""def prove_python_routing(
    plan,
    *,
    python_root,
    virtual_env,
    owner_uid,
    owner_gid,
    executable,
    prefix,
    exec_prefix,
    base_executable,
    python_version,
):
    python = plan["toolchain"]["python"]
    exact_version = python["version"]
    major_minor = ".".join(exact_version.split(".")[:2])
    catalog_root = python_root / python["catalog_key"]
    bin_directory = catalog_root / "bin"
    expected = bin_directory / f"python{major_minor}"

    for directory in (python_root.parent, python_root, catalog_root, bin_directory):
        value = directory.lstat()
        assert stat.S_ISDIR(value.st_mode) and not stat.S_ISLNK(value.st_mode)
        assert (value.st_uid, value.st_gid) == (owner_uid, owner_gid)
        assert stat.S_IMODE(value.st_mode) & 0o002 == 0
        assert directory.resolve(strict=True) == directory

    expected_value = expected.lstat()
    assert stat.S_ISREG(expected_value.st_mode)
    assert not stat.S_ISLNK(expected_value.st_mode)
    assert (expected_value.st_uid, expected_value.st_gid) == (owner_uid, owner_gid)
    assert stat.S_IMODE(expected_value.st_mode) & 0o002 == 0
    assert stat.S_IMODE(expected_value.st_mode) & 0o111
    assert expected.resolve(strict=True) == expected

    venv_python = virtual_env / "bin" / "python"
    aliases = (
        venv_python,
        virtual_env / "bin" / "python3",
        virtual_env / "bin" / f"python{major_minor}",
    )
    for alias in aliases:
        value = alias.lstat()
        assert stat.S_ISLNK(value.st_mode)
        assert (value.st_uid, value.st_gid) == (owner_uid, owner_gid)
        assert alias.resolve(strict=True) == expected

    assert pathlib.Path(executable) == venv_python
    assert pathlib.Path(prefix) == virtual_env
    assert pathlib.Path(exec_prefix) == virtual_env
    base_executable_path = pathlib.Path(base_executable)
    assert base_executable_path == expected
    assert base_executable_path.resolve(strict=True) == expected
    assert python_version == exact_version

    for command in (virtual_env / "bin" / "pip", virtual_env / "bin" / "pip3"):
        value = command.lstat()
        assert stat.S_ISREG(value.st_mode) and not stat.S_ISLNK(value.st_mode)
        assert (value.st_uid, value.st_gid) == (owner_uid, owner_gid)
        assert stat.S_IMODE(value.st_mode) & 0o002 == 0
        assert stat.S_IMODE(value.st_mode) & 0o111
        assert command.read_text().splitlines()[0] == f"#!{venv_python}"

    return expected
"""

_MANAGER_FILESYSTEM_PROOF_SOURCE = r"""def prove_manager_filesystem(
    site_packages,
    manager_name,
    cm_cli_path,
    anchor_path,
    workspace,
    owner_uid,
    owner_gid,
):
    site_packages_metadata = site_packages.lstat()
    assert stat.S_ISDIR(site_packages_metadata.st_mode)
    assert not stat.S_ISLNK(site_packages_metadata.st_mode)
    assert site_packages.resolve(strict=True) == site_packages

    manager_root_path = site_packages
    for part in manager_name.split("."):
        manager_root_path = manager_root_path / part
        component_metadata = manager_root_path.lstat()
        assert stat.S_ISDIR(component_metadata.st_mode)
        assert not stat.S_ISLNK(component_metadata.st_mode)
    manager_root = manager_root_path.resolve(strict=True)
    assert manager_root == manager_root_path

    manager_init_path = manager_root_path / "__init__.py"
    if manager_init_path.exists() or manager_init_path.is_symlink():
        manager_init_metadata = manager_init_path.lstat()
        assert stat.S_ISREG(manager_init_metadata.st_mode)
        assert not stat.S_ISLNK(manager_init_metadata.st_mode)
        manager_origin = manager_init_path.resolve(strict=True)
        assert manager_origin == manager_init_path
        assert manager_origin.parent == manager_root
    else:
        manager_origin = None

    cm_cli_metadata = cm_cli_path.lstat()
    assert stat.S_ISREG(cm_cli_metadata.st_mode)
    assert not stat.S_ISLNK(cm_cli_metadata.st_mode)
    assert stat.S_IMODE(cm_cli_metadata.st_mode) & 0o111
    assert (cm_cli_metadata.st_uid, cm_cli_metadata.st_gid) == (
        owner_uid,
        owner_gid,
    )
    assert cm_cli_path.read_text().splitlines()[0] == "#!/opt/venv/bin/python"

    anchor_metadata = anchor_path.lstat()
    assert stat.S_ISREG(anchor_metadata.st_mode)
    assert not stat.S_ISLNK(anchor_metadata.st_mode)
    assert stat.S_IMODE(anchor_metadata.st_mode) == 0o444
    assert (anchor_metadata.st_uid, anchor_metadata.st_gid) == (
        owner_uid,
        owner_gid,
    )
    assert anchor_path.read_text() == f"{workspace}\n"
    return manager_root, manager_origin


def prove_manager_import(manager_name, manager_root, manager_origin, workspace):
    def prove_spec(spec):
        assert spec is not None
        assert spec.name == manager_name
        assert spec.submodule_search_locations is not None
        locations = tuple(
            pathlib.Path(item).resolve(strict=True)
            for item in spec.submodule_search_locations
        )
        assert locations == (manager_root,)
        origin = (
            None
            if spec.origin is None
            else pathlib.Path(spec.origin).resolve(strict=True)
        )
        assert origin == manager_origin

    sys.path.insert(0, str(workspace))
    prove_spec(importlib.util.find_spec(manager_name))
    imported = importlib.import_module(manager_name)
    prove_spec(imported.__spec__)
    imported_locations = tuple(
        pathlib.Path(item).resolve(strict=True) for item in imported.__path__
    )
    assert imported_locations == (manager_root,)
    imported_file = (
        None
        if imported.__file__ is None
        else pathlib.Path(imported.__file__).resolve(strict=True)
    )
    assert imported_file == manager_origin
    assert pathlib.Path(sys.path[0]).resolve(strict=True) == workspace
"""


_SCENARIOS = RELEASE_SCENARIOS
_CPU_AUDIO_SCENARIOS = tuple(
    scenario for scenario in _SCENARIOS if Capability.CPU_AUDIO in scenario.capabilities
)
_CLI_SCENARIOS = tuple(
    scenario for scenario in _SCENARIOS if Capability.CLI in scenario.capabilities
)
_GPU_AUDIO_SCENARIOS = tuple(
    scenario for scenario in _SCENARIOS if Capability.GPU_AUDIO in scenario.capabilities
)


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(
            f"selected release scenario requires environment input {name}",
            pytrace=False,
        )
    return value


@pytest.fixture(autouse=True)
def _select_release_scenario(request: pytest.FixtureRequest) -> None:
    selected = set(request.config.getoption("--acceptance-scenario"))
    if not hasattr(request.node, "callspec"):
        return
    scenario = request.node.callspec.params.get("scenario")
    if not isinstance(scenario, AcceptanceScenario):
        return
    if selected and scenario.id not in selected:
        pytest.skip("release scenario was not selected")
    missing = [
        name
        for name in (scenario.image_variable, scenario.context_variable)
        if name is not None and not os.environ.get(name)
    ]
    if missing:
        pytest.fail(
            "selected release scenario requires environment input(s): "
            + ", ".join(missing),
            pytrace=False,
        )


def _expected_nodes(mixed: bool) -> list[dict[str, object]]:
    if not mixed:
        return []
    return [
        {
            "type": "git",
            "url": _CUSTOM_SCRIPTS_URL,
            "commit": _CUSTOM_SCRIPTS_COMMIT,
            "target": "/workspace/ComfyUI/custom_nodes/git-custom-scripts",
        },
        {
            "type": "registry",
            "id": "comfyui-custom-scripts",
            "version": "1.2.5",
        },
        {
            "type": "git",
            "url": _SUBMODULE_URL,
            "commit": _SUBMODULE_COMMIT,
            "target": f"/workspace/ComfyUI/custom_nodes/{_SUBMODULE_TARGET}",
        },
    ]


def _node_identity(node: dict[str, object]) -> dict[str, object]:
    keys = ("type", "url", "commit", "target")
    if node["type"] == "registry":
        keys = ("type", "id", "version")
    return {key: node[key] for key in keys}


# Release scenarios reuse one context/image while proving exact application
# capabilities.
@pytest.mark.acceptance(probes=(AcceptanceProbe.CONTEXT,))
@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda item: item.id)
def test_rendered_context_routes_exact_lock_plan_and_single_node_layer(
    scenario: AcceptanceScenario,
) -> None:
    context = Path(_environment(scenario.context_variable)).resolve(strict=True)
    assert context.joinpath(".cdh-rendered").is_file()
    plan = parse_build_plan_json(context.joinpath("build-plan.json").read_bytes())
    lock = parse_canonical_lock_toml(context.joinpath("config.lock.toml").read_bytes())
    binding = manifest_binding(plan)
    canonical_lock_bytes = dump_canonical_lock_toml(lock).encode("utf-8")
    assert context.joinpath("config.lock.toml").read_bytes() == canonical_lock_bytes
    lock_digest = f"sha256:{hashlib.sha256(canonical_lock_bytes).hexdigest()}"

    assert binding.build_plan_digest == build_plan_digest(plan)
    assert binding.config_digest == plan.config_digest
    assert binding.lock_digest == plan.lock_digest
    assert plan.lock_digest == lock_digest
    assert plan.toolchain.python.version == scenario.python_version
    assert plan.toolchain.python.pip_version == "26.1.2"
    assert plan.toolchain.cuda_version == "13.0.3"
    assert plan.toolchain.pytorch_channel == "cu130"
    assert plan.application.comfyui.commit == _COMFYUI_COMMIT
    assert plan.application.comfyui.floor_commit == _COMFYUI_COMMIT
    assert plan.application.comfyui.formal_release == "0.11.0"
    assert (plan.application.comfyui.manager is not None) is scenario.install_manager
    assert (plan.toolchain.tool_store.comfy_cli is not None) is scenario.install_cli
    assert plan.application.pytorch.setuptools_specifier == "<82"
    assert [
        (item.name, item.version) for item in plan.application.pytorch.packages
    ] == [
        ("torch", "2.12.1+cu130"),
        ("torchaudio", "2.11.0+cu130"),
        ("torchvision", "0.27.1+cu130"),
    ]
    assert plan.application.pytorch.python_index_url == "https://pypi.org/simple"
    assert plan.application.pytorch.pytorch_index_url == (
        "https://download.pytorch.org/whl/cu130"
    )
    wheel = context / (
        "bootstrap/comfyui_docker_helper-"
        f"{plan.toolchain.tool_store.cdh.version}-py3-none-any.whl"
    )
    assert wheel.is_file()
    assert f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}" == (
        plan.toolchain.tool_store.cdh.wheel_digest
    )
    assert context.joinpath(".dockerignore").read_bytes() == (
        b"/.cdh-rendered\n/config.lock.toml\n"
    )

    expected = _expected_nodes(scenario.mixed)
    fixture_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "comfyui-build"
        / "configs"
        / scenario.fixture
    )
    fixture = tomllib.loads(fixture_path.read_text())
    fixture_nodes = fixture["comfyui"].get("custom_nodes", [])
    assert [item["url"] for item in fixture_nodes if item["type"] == "git"] == [
        item["url"] for item in expected if item["type"] == "git"
    ]
    actual = [
        _node_identity(item.model_dump(mode="json")) for item in plan.custom_nodes.nodes
    ]
    assert actual == expected
    if scenario.hooks:
        first = plan.custom_nodes.nodes[0]
        assert [item.relative_path for item in first.pre_install] == [
            "pre.sh",
            "pre.py",
        ]
        assert [item.relative_path for item in first.post_install] == [
            "post.sh",
            "post.py",
        ]
        assert not plan.custom_nodes.nodes[1].pre_install
        assert not plan.custom_nodes.nodes[1].post_install
        for hook in (*first.pre_install, *first.post_install):
            materialized = context / "inputs" / hook.relative_path
            observed = f"sha256:{hashlib.sha256(materialized.read_bytes()).hexdigest()}"
            assert observed == hook.digest

    assert lock.comfyui.commit == plan.application.comfyui.commit
    assert [(item.url, item.commit) for item in lock.custom_nodes.git] == [
        (node["url"], node["commit"]) for node in expected if node["type"] == "git"
    ]
    assert [(item.id, item.version) for item in lock.custom_nodes.registry] == [
        (node["id"], node["version"]) for node in expected if node["type"] == "registry"
    ]
    cli_entries = [item for item in lock.python.uv_tools if item.name == "comfy-cli"]
    expected_cli_count = 1 if scenario.install_cli else 0
    assert len(cli_entries) == expected_cli_count
    cli_plan = plan.toolchain.tool_store.comfy_cli
    if scenario.install_cli:
        assert cli_plan is not None
        cli_entry = cli_entries[0]
        assert cli_entry.name == cli_plan.name == "comfy-cli"
        assert cli_plan.environment == "uv-tool:comfy-cli"
        assert cli_entry.version == cli_plan.version
    else:
        assert cli_plan is None

    dockerfile = context.joinpath("Dockerfile").read_text()
    assert dockerfile.count("container install-custom-nodes") == 1
    assert dockerfile.count(
        f"--build-plan-digest {binding.build_plan_digest}"
    ) == 3 + bool(plan.files.files)
    assert ("COPY inputs /opt/cdh/build/inputs" in dockerfile) is scenario.hooks
    assert "comfy node" not in dockerfile
    assert "comfy install" not in dockerfile


_COMMON_IMAGE_PROBE = (
    r"""
set -eu

/opt/venv/bin/python -I - <<'PY'
import importlib.metadata as metadata
import importlib
import importlib.util
import json
import os
import pathlib
import platform
import re
import stat
import subprocess
import sys
import tomllib

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

__GIT_PROOF_SOURCE__
__REGISTRY_PROOF_SOURCE__
__MANAGER_FILESYSTEM_PROOF_SOURCE__
__PYTHON_ROUTING_PROOF_SOURCE__

def require_inventory(path_text, mode, python_prefix):
    path = pathlib.Path(path_text)
    value = path.lstat()
    assert stat.S_ISREG(value.st_mode) and not stat.S_ISLNK(value.st_mode)
    assert stat.S_IMODE(value.st_mode) == mode
    assert (value.st_uid, value.st_gid) == (0, 0)
    source = r'''__INVENTORY_OBSERVER_SOURCE__'''
    completed = subprocess.run(
        [python_prefix, "-I", "-c", source, path_text],
        check=True,
        capture_output=True,
        text=True,
    )
    items = json.loads(completed.stdout)
    assert items == sorted(items)
    assert len(items) == len({name for name, _ in items})
    return dict(items)

build = pathlib.Path("/opt/cdh/build")
plan = json.loads(build.joinpath("build-plan.json").read_text())
expected_cli = os.environ["EXPECTED_CLI"] == "1"
expected_manager = os.environ["EXPECTED_MANAGER"] == "1"
expected_mixed = os.environ["EXPECTED_MIXED"] == "1"
assert plan["lock_digest"] == os.environ["EXPECTED_LOCK_DIGEST"]
digest_source = (
    "import pathlib; "
    "from comfyui_docker_helper.config.build_plan import "
    "build_plan_digest, parse_build_plan_json; "
    "path=pathlib.Path('/opt/cdh/build/build-plan.json'); "
    "print(build_plan_digest(parse_build_plan_json(path.read_bytes())))"
)
observed_plan_digest = subprocess.run(
    [
        "/opt/uv/tools/comfyui-docker-helper/bin/python",
        "-I", "-c", digest_source,
    ],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
assert observed_plan_digest == os.environ["EXPECTED_BUILD_PLAN_DIGEST"]
expected_python_version = os.environ["EXPECTED_PYTHON_VERSION"]
assert plan["toolchain"]["python"]["version"] == expected_python_version
assert plan["toolchain"]["python"]["pip_version"] == "26.1.2"
assert plan["toolchain"]["pytorch_channel"] == "cu130"
comfyui = plan["application"]["comfyui"]
assert comfyui["commit"] == "09725967cf76304371c390ca1d6483e04061da48"
assert comfyui["floor_commit"] == comfyui["commit"]
assert comfyui["formal_release"] == "0.11.0"
assert (plan["application"]["comfyui"]["manager"] is not None) == expected_manager
assert (plan["toolchain"]["tool_store"]["comfy_cli"] is not None) == expected_cli

application = require_inventory(
    "/opt/cdh/build/application-inventory.txt", 0o444, "/opt/venv/bin/python",
)
cdh_version = subprocess.run(
    [
        "/opt/uv/tools/comfyui-docker-helper/bin/python",
        "-I",
        "-c",
        "import importlib.metadata as m; print(m.version('comfyui-docker-helper'))",
    ],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
assert cdh_version == plan["toolchain"]["tool_store"]["cdh"]["version"]
assert pathlib.Path("/opt/uv/bin/cdh").resolve(strict=True) == pathlib.Path(
    "/opt/uv/tools/comfyui-docker-helper/bin/cdh"
)
assert application["pip"] == "26.1.2"
assert metadata.version("pip") == "26.1.2"
assert pathlib.Path(importlib.util.find_spec("pip").origin).is_relative_to(
    pathlib.Path("/opt/venv")
)
managed_python = prove_python_routing(
    plan,
    python_root=pathlib.Path("/opt/python"),
    virtual_env=pathlib.Path("/opt/venv"),
    owner_uid=0,
    owner_gid=0,
    executable=sys.executable,
    prefix=sys.prefix,
    exec_prefix=sys.exec_prefix,
    base_executable=sys._base_executable,
    python_version=platform.python_version(),
)
assert managed_python == pathlib.Path(
    "/opt/python",
    plan["toolchain"]["python"]["catalog_key"],
    "bin",
    f"python{'.'.join(expected_python_version.split('.')[:2])}",
)
pip_outputs = []
for command in (
    ["/opt/venv/bin/pip", "--version"],
    ["/opt/venv/bin/pip3", "--version"],
    ["/opt/venv/bin/python", "-m", "pip", "--version"],
):
    pip_outputs.append(
        subprocess.run(command, check=True, capture_output=True, text=True).stdout
    )
assert pip_outputs[0] == pip_outputs[1] == pip_outputs[2]
assert "pip 26.1.2 from /opt/venv/" in pip_outputs[0]

packages = {
    item["name"]: item["version"]
    for item in plan["application"]["pytorch"]["packages"]
}
assert packages == {
    "torch": "2.12.1+cu130",
    "torchaudio": "2.11.0+cu130",
    "torchvision": "0.27.1+cu130",
}
assert {name: metadata.version(name) for name in packages} == packages
setuptools_policy = plan["application"]["pytorch"]["setuptools_specifier"]
assert setuptools_policy == "<82"
assert metadata.version("setuptools") in SpecifierSet(setuptools_policy)
constraints = build.joinpath("python-package-constraints.txt")
constraints_metadata = constraints.lstat()
assert stat.S_IMODE(constraints_metadata.st_mode) == 0o444
assert (constraints_metadata.st_uid, constraints_metadata.st_gid) == (0, 0)
assert constraints.read_text().splitlines() == [
    "setuptools<82",
    "torch==2.12.1+cu130",
    "torchaudio==2.11.0+cu130",
    "torchvision==0.27.1+cu130",
]

workspace = pathlib.Path(os.environ["COMFYUI_PATH"])
head = subprocess.run(
    ["git", "-C", workspace, "rev-parse", "--verify", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
assert head == plan["application"]["comfyui"]["commit"]
assert subprocess.run(
    ["git", "-C", workspace, "symbolic-ref", "-q", "HEAD"],
    capture_output=True,
).returncode == 1
subprocess.run(
    [
        "git", "-C", workspace, "merge-base", "--is-ancestor",
        plan["application"]["comfyui"]["floor_commit"], "HEAD",
    ],
    check=True,
)

manager = plan["application"]["comfyui"]["manager"]
anchor_path = pathlib.Path(
    "/opt/venv/lib",
    f"python{'.'.join(expected_python_version.split('.')[:2])}",
    "site-packages",
    "comfyui-docker-helper-comfyui.pth",
)
cm_cli_path = pathlib.Path("/opt/venv/bin/cm-cli")
if expected_manager:
    assert metadata.version("comfyui-manager") == "4.0.5"
    assert manager["executable"] == "/opt/venv/bin/cm-cli"
    assert pathlib.Path(manager["import_anchor"]) == anchor_path
    site_packages = anchor_path.parent
    manager_root, manager_origin = prove_manager_filesystem(
        site_packages,
        manager["import_name"],
        cm_cli_path,
        anchor_path,
        workspace,
        0,
        0,
    )
    distribution = metadata.distribution("comfyui-manager")
    assert pathlib.Path(distribution.locate_file("")).resolve(strict=True) == (
        site_packages
    )
    distribution_root_path = pathlib.Path(
        distribution.locate_file(manager["import_name"])
    )
    assert distribution_root_path == site_packages / manager["import_name"]
    assert distribution_root_path.resolve(strict=True) == manager_root
    owned_files = tuple(distribution.files or ())
    assert owned_files
    assert any(
        pathlib.Path(distribution.locate_file(item))
        .resolve(strict=True)
        .is_relative_to(manager_root)
        for item in owned_files
    )
    prove_manager_import(
        manager["import_name"], manager_root, manager_origin, workspace
    )
    owners = [
        distribution.metadata["Name"]
        for distribution in metadata.distributions()
        for entrypoint in distribution.entry_points
        if entrypoint.group == "console_scripts" and entrypoint.name == "cm-cli"
    ]
    assert owners == ["comfyui-manager"]
else:
    assert manager is None
    try:
        metadata.version("comfyui-manager")
    except metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError("Manager is installed in a Manager-disabled image")
    assert importlib.util.find_spec("comfyui_manager") is None
    assert not os.path.lexists(cm_cli_path)
    assert not os.path.lexists(anchor_path)

tool = plan["toolchain"]["tool_store"]["comfy_cli"]
if expected_cli:
    tool_inventory = require_inventory(
        tool["inventory_path"], 0o644, "/opt/uv/tools/comfy-cli/bin/python",
    )
    assert tool_inventory["comfy-cli"] == tool["version"]
    for command in ("comfy", "comfy-cli", "comfycli"):
        public = pathlib.Path("/opt/uv/bin") / command
        owned = pathlib.Path("/opt/uv/tools/comfy-cli/bin") / command
        assert public.resolve(strict=True) == owned
else:
    assert tool is None
    assert not os.path.lexists("/opt/uv/tools/comfy-cli")
    assert not os.path.lexists(build / "comfy-cli-inventory.txt")
    commands = ("comfy", "comfy-cli", "comfycli")
    assert all(
        not os.path.lexists(pathlib.Path("/opt/uv/bin", item))
        for item in commands
    )

assert not os.path.lexists(build / "registry-inventory.json")

nodes = plan["custom_nodes"]["nodes"]
assert bool(nodes) == expected_mixed
inventory = build.joinpath("custom-node-inventory.json")
value = inventory.lstat()
assert stat.S_ISREG(value.st_mode) and not stat.S_ISLNK(value.st_mode)
assert stat.S_IMODE(value.st_mode) == 0o444
assert (value.st_uid, value.st_gid) == (0, 0)
def inventory_entry(node):
    if node["type"] == "registry":
        return {
            "control": "direct-cm-cli", "id": node["id"],
            "type": "registry", "verification": "registry-version",
            "version": node["version"],
        }
    return {
        "commit": node["commit"], "control": "direct-git",
        "target": pathlib.Path(node["target"]).name, "type": "git",
        "url": node["url"], "verification": "git-commit",
    }
expected_inventory = {
    "nodes": [inventory_entry(node) for node in nodes], "schema_version": 1,
}
expected_bytes = (
    json.dumps(
        expected_inventory, ensure_ascii=True, separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
).encode()
assert inventory.read_bytes() == expected_bytes
custom_nodes = workspace / "custom_nodes"
scan_registry_projects_after_git_proof(custom_nodes, nodes)
if expected_mixed:
    gitlink = subprocess.run(
        [
            "git", "-C", custom_nodes / "__SUBMODULE_TARGET__", "ls-tree",
            "HEAD", "__SUBMODULE_GITLINK_PATH__",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert gitlink == (
        "160000 commit __SUBMODULE_GITLINK_COMMIT__"
        "\t__SUBMODULE_GITLINK_PATH__"
    )

hook_log = workspace / "cdh-smoke-hook-events.log"
if expected_mixed:
    observed_order = [
        (
            node["type"],
            pathlib.Path(node.get("target", "")).name
            if node["type"] == "git" else node["id"],
        )
        for node in nodes
    ]
    assert observed_order == [
        ("git", "git-custom-scripts"),
        ("registry", "comfyui-custom-scripts"),
        ("git", "__SUBMODULE_TARGET__"),
    ]
    assert nodes[0]["url"] == "https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git"
    assert nodes[2]["url"] == "__SUBMODULE_URL__"
    assert hook_log.is_file()
    lines = hook_log.read_text().splitlines()
    expected_hook_lines = []
    for hook in ("pre.sh", "pre.py", "post.sh", "post.py"):
        expected_hook_lines.extend(
            (
                f"{hook} cwd={workspace}",
                f"{hook} WORKSPACE={workspace.parent}",
                f"{hook} COMFYUI_PATH={workspace}",
                f"{hook} VIRTUAL_ENV=/opt/venv",
            )
        )
    assert lines == expected_hook_lines
else:
    assert inventory.read_bytes() == b'{"nodes":[],"schema_version":1}\n'
    assert not hook_log.exists()
PY

uv --no-config pip check --python /opt/venv/bin/python --no-python-downloads
uv --no-config pip check \
  --python /opt/uv/tools/comfyui-docker-helper/bin/python --no-python-downloads
if test "$EXPECTED_CLI" = 1; then
  uv --no-config pip check \
    --python /opt/uv/tools/comfy-cli/bin/python --no-python-downloads
  for command in comfy comfy-cli comfycli; do
    "/opt/uv/bin/$command" --help >/dev/null
  done
fi
""".replace("__GIT_PROOF_SOURCE__", GIT_PROOF_SOURCE)
    .replace("__REGISTRY_PROOF_SOURCE__", REGISTRY_PROOF_SOURCE)
    .replace("__INVENTORY_OBSERVER_SOURCE__", _INVENTORY_OBSERVER_SOURCE)
    .replace(
        "__MANAGER_FILESYSTEM_PROOF_SOURCE__",
        _MANAGER_FILESYSTEM_PROOF_SOURCE,
    )
    .replace("__PYTHON_ROUTING_PROOF_SOURCE__", _PYTHON_ROUTING_PROOF_SOURCE)
    .replace("__SUBMODULE_TARGET__", _SUBMODULE_TARGET)
    .replace("__SUBMODULE_GITLINK_PATH__", _SUBMODULE_GITLINK_PATH)
    .replace("__SUBMODULE_GITLINK_COMMIT__", _SUBMODULE_GITLINK_COMMIT)
    .replace("__SUBMODULE_URL__", _SUBMODULE_URL)
)


_SERVICE_HELPERS = r"""
application_pid=""
application_log="${application_log:-/tmp/comfy-acceptance.log}"
application_log_emitted=0
launch_application() {
  application_python="${application_python:-/opt/venv/bin/python}"
  application_main="${application_main:-main.py}"
  trampoline='import os, signal, sys; '
  trampoline="${trampoline}signal.signal(signal.SIGINT, signal.SIG_DFL); "
  trampoline="${trampoline}os.execv(sys.argv[1], sys.argv[1:])"
  "$application_python" -I -c "$trampoline" \
    "$application_python" "$application_main" "$@" \
    > "$application_log" 2>&1 &
  application_pid="$!"
}
application_is_non_zombie() {
  test -n "$application_pid" || return 1
  test -r "/proc/$application_pid/stat" || return 1
  state="$(sed -n 's/^.*) \([^ ]\).*/\1/p' "/proc/$application_pid/stat")"
  test -n "$state" && test "$state" != Z && test "$state" != X
}
emit_application_log() {
  if test "$application_log_emitted" -eq 0 && test -f "$application_log"; then
    cat "$application_log"
    application_log_emitted=1
  fi
}
emergency_cleanup() {
  status="$?"
  trap - EXIT INT TERM
  if application_is_non_zombie; then
    kill -INT "$application_pid" 2>/dev/null || true
    attempt=0
    while application_is_non_zombie && test "$attempt" -lt 5; do
      attempt=$((attempt + 1))
      sleep 1
    done
  fi
  if application_is_non_zombie; then
    kill -KILL "$application_pid" 2>/dev/null || true
  fi
  test -z "$application_pid" || wait "$application_pid" 2>/dev/null || true
  emit_application_log
  exit "$status"
}
stop_application_cleanly() {
  kill -INT "$application_pid"
  attempt=0
  while application_is_non_zombie && test "$attempt" -lt 30; do
    attempt=$((attempt + 1))
    sleep 1
  done
  if application_is_non_zombie; then
    return 1
  fi
  set +e
  wait "$application_pid"
  status="$?"
  set -e
  application_pid=""
  emit_application_log
  test "$status" -eq 0
  grep -Fxq 'Stopped server' "$application_log"
  if grep -Eq \
    'Traceback|OutOfMemoryError|CUDA out of memory|Killed' \
    "$application_log"; then
    return 1
  fi
  audio_module='(nodes_audio|comfy_extras[./]nodes_audio(\.py)?)'
  audio_failure='(cannot[[:space:]]+import|warning|error|fail(ed)?|exception)'
  audio_pattern="${audio_failure}.{0,512}${audio_module}|"
  audio_pattern="${audio_pattern}${audio_module}.{0,512}${audio_failure}"
  if tr '\n' ' ' < "$application_log" | grep -Eiq \
    "$audio_pattern"; then
    return 1
  fi
  trap - EXIT INT TERM
}
wait_for_readiness() {
  attempt=0
  until curl --fail --silent --show-error \
    http://127.0.0.1:8291/system_stats >/dev/null; do
    application_is_non_zombie
    attempt=$((attempt + 1))
    test "$attempt" -lt 180
    sleep 1
  done
}
trap emergency_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"""


_API_AND_WRITABILITY_PROBE = r"""
/opt/venv/bin/python -I - <<'PY'
import json
import os
import pathlib
import sqlite3
import urllib.request

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8291" + path, timeout=10) as response:
        assert response.status == 200
        return json.load(response)

stats = get("/system_stats")
assert stats["system"]["comfyui_version"] == "0.11.0"
assert stats["devices"]
objects = get("/object_info")
expected_audio = {
    "ConditioningStableAudio", "EmptyLatentAudio", "LoadAudio",
    "PreviewAudio", "SaveAudio", "VAEDecodeAudio", "VAEEncodeAudio",
}
assert expected_audio <= set(objects)
workspace = pathlib.Path(os.environ["COMFYUI_PATH"])
user = workspace / "user"
write_directories = (
    pathlib.Path(os.environ["WORKSPACE"]),
    workspace / "input",
    workspace / "output",
    user,
)
for directory in write_directories:
    assert directory.is_dir()
    probe = directory / ".cdh-acceptance-write"
    probe.write_text("ok\n")
    assert probe.read_text() == "ok\n"
    probe.unlink()
databases = tuple(user.rglob("*.db"))
assert databases
for database in databases:
    assert os.access(database, os.W_OK)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cdh_acceptance_probe (value INTEGER)"
        )
        connection.rollback()
PY
"""


_CPU_PROBE = (
    r"""
set -eu
/opt/venv/bin/python -I - <<'PY'
import torch
import torchaudio

tensor = torch.arange(12, dtype=torch.float32).reshape(3, 4)
assert tensor.device.type == "cpu"
assert tensor.sum().item() == 66
waveform = torch.sin(torch.linspace(0, 30, 1600)).reshape(1, -1)
resampled = torchaudio.functional.resample(waveform, 16000, 8000)
mel = torchaudio.transforms.MelSpectrogram(sample_rate=16000, n_fft=400)(waveform)
assert resampled.device.type == "cpu" and resampled.shape[-1] == 800
assert mel.device.type == "cpu" and mel.shape[-2] == 128
PY
"""
    + _SERVICE_HELPERS
    + r"""
cd "$COMFYUI_PATH"
set --
if test "$EXPECTED_MANAGER" = 1; then
  set -- --enable-manager
fi
launch_application \
  --listen 127.0.0.1 --port 8291 --disable-auto-launch --cpu "$@"
wait_for_readiness
"""
    + _API_AND_WRITABILITY_PROBE
    + r"""
stop_application_cleanly
"""
)


_GPU_PROBE = (
    r"""
set -eu
/opt/venv/bin/python -I - <<'PY'
import importlib.metadata as metadata
import os
import torch
import torchaudio

assert metadata.version("torch") == "2.12.1+cu130"
assert metadata.version("torchaudio") == "2.11.0+cu130"
assert torch.version.cuda == "13.0"
assert torch.cuda.is_available()
assert torch.cuda.get_device_name(0) == os.environ["EXPECTED_GPU_NAME"]
device = torch.device("cuda:0")
tensor = torch.arange(4096, device=device, dtype=torch.float32)
assert (tensor * tensor).sum().is_cuda
waveform = torch.sin(torch.linspace(0, 30, 1600, device=device)).reshape(1, -1)
resampled = torchaudio.functional.resample(waveform, 16000, 8000)
transform = torchaudio.transforms.MelSpectrogram(sample_rate=16000, n_fft=400)
mel = transform.to(device)(waveform)
assert resampled.is_cuda and resampled.shape[-1] == 800
assert mel.is_cuda and mel.shape[-2] == 128
log_probs = torch.log_softmax(torch.randn(1, 8, 5, device=device), dim=-1)
targets = torch.tensor([[1, 2]], dtype=torch.int32, device=device)
input_lengths = torch.tensor([8], dtype=torch.int32, device=device)
target_lengths = torch.tensor([2], dtype=torch.int32, device=device)
path, scores = torchaudio.functional.forced_align(
    log_probs, targets, input_lengths, target_lengths
)
assert path.is_cuda and scores.is_cuda and path.shape == scores.shape == (1, 8)
torch.cuda.synchronize()
PY
"""
    + _SERVICE_HELPERS
    + r"""
cd "$COMFYUI_PATH"
launch_application \
  --listen 127.0.0.1 --port 8291 --disable-auto-launch
wait_for_readiness
"""
    + _API_AND_WRITABILITY_PROBE
    + r"""
/opt/venv/bin/python -I - <<'PY'
import json
import os
import urllib.request
url = "http://127.0.0.1:8291/system_stats"
with urllib.request.urlopen(url, timeout=10) as response:
    stats = json.load(response)
devices = stats["devices"]
assert any(os.environ["EXPECTED_GPU_NAME"] in item["name"] for item in devices)
PY
stop_application_cleanly
"""
)


def _run_disposable(
    image: str,
    script: str,
    *,
    environment: dict[str, str] | None = None,
    gpu: bool = False,
    timeout: int = 600,
) -> None:
    name = f"cdh-application-acceptance-{uuid.uuid4().hex[:12]}"
    command = ["docker", "run", "--rm", "--name", name]
    if gpu:
        command.extend(("--gpus", "all"))
    for key, value in (environment or {}).items():
        command.extend(("--env", f"{key}={value}"))
    command.extend(("--entrypoint", "/bin/sh", image, "-ec", script))
    try:
        subprocess.run(command, check=True, timeout=timeout)
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


# Every release image must exercise its real entrypoint once in CPU mode; this
# is deliberately narrower than the lifecycle suite's deep hook and force matrix.
@pytest.mark.acceptance(probes=(AcceptanceProbe.ENTRYPOINT_TOPOLOGY,))
@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda item: item.id)
def test_default_entrypoint_has_tini_cdh_topology_and_completes_sigterm_shutdown(
    scenario: AcceptanceScenario,
) -> None:
    image = _environment(scenario.image_variable)
    context = Path(_environment(scenario.context_variable)).resolve(strict=True)
    plan = parse_build_plan_json(context.joinpath("build-plan.json").read_bytes())
    name = f"cdh-application-entrypoint-{uuid.uuid4().hex[:12]}"
    run = [
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--env",
        "CDH_COMFYUI_EXTRA_ARGS=--cpu",
        image,
    ]
    try:
        subprocess.run(run, check=True, capture_output=True, text=True, timeout=120)
        deadline = time.monotonic() + 180
        readiness = (
            "import urllib.request; "
            "response=urllib.request.urlopen("
            "'http://127.0.0.1:8188/system_stats', timeout=2); "
            "assert response.status == 200"
        )
        while time.monotonic() < deadline:
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "/opt/venv/bin/python",
                    "-I",
                    "-c",
                    readiness,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if ready.returncode == 0:
                break
            state = json.loads(
                subprocess.run(
                    ["docker", "inspect", name],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout
            )[0]["State"]
            if not state["Running"]:
                logs = subprocess.run(
                    ["docker", "logs", name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                pytest.fail(
                    "default entrypoint exited before readiness: "
                    f"state={state!r} logs={logs.stdout + logs.stderr}",
                    pytrace=False,
                )
            time.sleep(1)
        else:
            pytest.fail("default entrypoint did not reach CPU readiness", pytrace=False)

        image_config = json.loads(
            subprocess.run(
                ["docker", "image", "inspect", image],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        )[0]["Config"]
        assert image_config["StopSignal"] == "SIGTERM"
        assert image_config["Entrypoint"] == [
            "/usr/bin/tini",
            "--",
            "/opt/uv/bin/cdh",
            "container",
            "entrypoint",
        ]
        pid1 = subprocess.run(
            ["docker", "exec", name, "cat", "/proc/1/comm"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        assert pid1 == "tini"
        children = subprocess.run(
            ["docker", "exec", name, "cat", "/proc/1/task/1/children"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.split()
        expected_argv = [
            f"{plan.toolchain.tool_store.cdh.environment}/bin/python",
            "/opt/uv/bin/cdh",
            "container",
            "entrypoint",
        ]
        expected_cmdline = [item.encode() for item in expected_argv]
        cdh_children = []
        for child_pid in children:
            observed = subprocess.run(
                ["docker", "exec", name, "cat", f"/proc/{child_pid}/cmdline"],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if observed.returncode != 0:
                continue
            if observed.stdout.rstrip(b"\0").split(b"\0") == expected_cmdline:
                cdh_children.append(child_pid)
        assert len(cdh_children) == 1
        cdh_pid = cdh_children[0]
        status = subprocess.run(
            ["docker", "exec", name, "cat", f"/proc/{cdh_pid}/status"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.splitlines()
        assert "PPid:\t1" in status
        cmdline = (
            subprocess.run(
                ["docker", "exec", name, "cat", f"/proc/{cdh_pid}/cmdline"],
                check=True,
                capture_output=True,
                timeout=30,
            )
            .stdout.rstrip(b"\0")
            .split(b"\0")
        )
        assert cmdline == expected_cmdline

        subprocess.run(
            ["docker", "stop", "--time", "15", name],
            check=True,
            capture_output=True,
            text=True,
            timeout=25,
        )
        state = json.loads(
            subprocess.run(
                ["docker", "inspect", name],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        )[0]["State"]
        assert state["Running"] is False
        assert state["Pid"] == 0
        # cdh preserves the real ComfyUI child's SIGTERM-derived exit result.
        assert state["ExitCode"] == 143
        assert state["OOMKilled"] is False
        logs = subprocess.run(
            ["docker", "logs", name],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        combined_logs = logs.stdout + logs.stderr
        assert not any(
            pattern in combined_logs
            for pattern in (
                "Traceback",
                "OutOfMemoryError",
                "CUDA out of memory",
                "Killed",
            )
        )
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


@pytest.mark.acceptance(probes=(AcceptanceProbe.IMAGE_ENVIRONMENT,))
@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda item: item.id)
def test_image_has_exact_environment_and_disposition(
    scenario: AcceptanceScenario,
) -> None:
    context = Path(_environment(scenario.context_variable)).resolve(strict=True)
    plan = parse_build_plan_json(context.joinpath("build-plan.json").read_bytes())
    lock = parse_canonical_lock_toml(context.joinpath("config.lock.toml").read_bytes())
    binding = manifest_binding(plan)
    lock_bytes = dump_canonical_lock_toml(lock).encode("utf-8")
    lock_digest = f"sha256:{hashlib.sha256(lock_bytes).hexdigest()}"
    assert context.joinpath("config.lock.toml").read_bytes() == lock_bytes
    assert plan.lock_digest == lock_digest
    _run_disposable(
        _environment(scenario.image_variable),
        _COMMON_IMAGE_PROBE,
        environment={
            "EXPECTED_CLI": str(int(scenario.install_cli)),
            "EXPECTED_MANAGER": str(int(scenario.install_manager)),
            "EXPECTED_MIXED": str(int(scenario.mixed)),
            "EXPECTED_PYTHON_VERSION": scenario.python_version,
            "EXPECTED_BUILD_PLAN_DIGEST": binding.build_plan_digest,
            "EXPECTED_LOCK_DIGEST": lock_digest,
        },
    )


@pytest.mark.acceptance(probes=(AcceptanceProbe.CPU_AUDIO_APPLICATION,))
@pytest.mark.parametrize("scenario", _CPU_AUDIO_SCENARIOS, ids=lambda item: item.id)
def test_cpu_audio_api_writable_state_and_clean_shutdown(
    scenario: AcceptanceScenario,
) -> None:
    _run_disposable(
        _environment(scenario.image_variable),
        _CPU_PROBE,
        environment={"EXPECTED_MANAGER": str(int(scenario.install_manager))},
    )


@pytest.mark.acceptance(probes=(AcceptanceProbe.CLI_BRIDGE,))
@pytest.mark.parametrize("scenario", _CLI_SCENARIOS, ids=lambda item: item.id)
def test_comfy_cli_workspace_python_bridge(
    scenario: AcceptanceScenario,
) -> None:
    _run_disposable(
        _environment(scenario.image_variable),
        COMFY_CLI_BRIDGE_PROBE,
    )


@pytest.mark.gpu
@pytest.mark.acceptance(probes=(AcceptanceProbe.CUDA_AUDIO,))
@pytest.mark.parametrize("scenario", _GPU_AUDIO_SCENARIOS, ids=lambda item: item.id)
def test_cuda_audio_api_and_clean_shutdown(
    scenario: AcceptanceScenario,
) -> None:
    _run_disposable(
        _environment(scenario.image_variable),
        _GPU_PROBE,
        environment={
            "EXPECTED_GPU_NAME": os.environ.get(
                "CDH_APPLICATION_GPU_NAME", "NVIDIA GeForce RTX 5090"
            )
        },
        gpu=True,
    )
