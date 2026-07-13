from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.test_build_plan import accepted_resolution, final_config

from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    CustomNodesPhase,
    HookPlan,
    RegistryNodePlan,
    construct_build_plan,
)
from comfyui_docker_helper.container import comfyui_installer, registry_installer
from comfyui_docker_helper.container.comfyui_installer import ComfyUIInstallError
from comfyui_docker_helper.container.registry_installer import (
    RegistryInstallError,
)
from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
)


def _node(
    node_id: str,
    version: str,
    *,
    pre: tuple[str, ...] = (),
    post: tuple[str, ...] = (),
) -> RegistryNodePlan:
    return RegistryNodePlan(
        type="registry",
        id=node_id,
        version=version,
        pre_install=tuple(
            HookPlan(relative_path=value, digest=f"sha256:{'a' * 64}") for value in pre
        ),
        post_install=tuple(
            HookPlan(relative_path=value, digest=f"sha256:{'b' * 64}") for value in post
        ),
    )


def _write_project(root: Path, directory: str, name: str, version: str) -> Path:
    target = root / directory
    target.mkdir()
    target.joinpath("pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    return target


def _application(tmp_path: Path) -> tuple[ApplicationPhase, ContainerRuntime]:
    plan = construct_build_plan(final_config(), accepted_resolution())
    workspace = tmp_path / "workspace"
    comfyui = workspace / "ComfyUI"
    comfyui.joinpath("custom_nodes").mkdir(parents=True)
    document = plan.application.model_dump(mode="python")
    document["paths"]["workspace"] = str(workspace)
    document["paths"]["comfyui"] = str(comfyui)
    application = ApplicationPhase.model_validate(document)
    runtime = ContainerRuntime(
        workspace=workspace,
        comfyui_path=comfyui,
        virtual_env=Path(application.paths.venv),
    )
    return application, runtime


def _phase(
    runtime: ContainerRuntime,
    nodes: tuple[RegistryNodePlan, ...],
) -> CustomNodesPhase:
    return CustomNodesPhase(
        install_manager=True,
        user_directory=str(runtime.comfyui_path / "user"),
        registry_inventory="/opt/cdh/build/registry-inventory.json",
        nodes=nodes,
    )


def _patch_phases(
    monkeypatch: pytest.MonkeyPatch,
    application: ApplicationPhase,
    custom_nodes: CustomNodesPhase,
) -> None:
    def load(_path, phase, **_kwargs):
        return application if phase == "application" else custom_nodes

    monkeypatch.setattr(registry_installer, "load_phase_input", load)
    monkeypatch.setattr(
        registry_installer,
        "capture_manager_registry_authority",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        registry_installer,
        "verify_manager_registry_capability",
        lambda *_args: None,
    )


def test_registry_version_comparison_preserves_raw_complete_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    root.joinpath("example.py").write_text("# built-in example\n")
    root.joinpath("legacy-node").mkdir()
    _write_project(
        root,
        "package",
        "Example.Node",
        "1.0.0rc1+CUDA.1",
    )

    registry_installer._verify_registry_set(
        root,
        (_node("Example_Node", "1.0.0-rc.1+cuda.1"),),
    )

    with pytest.raises(RegistryInstallError, match="version does not match"):
        registry_installer._verify_registry_set(
            root,
            (_node("Example_Node", "1.0.0-rc.1+cuda.2"),),
        )


def test_nested_only_registry_metadata_remains_missing(tmp_path: Path) -> None:
    root = tmp_path / "custom_nodes"
    nested = root / "wrapper/nested"
    nested.mkdir(parents=True)
    nested.joinpath("pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "1.0.0"\n'
    )

    with pytest.raises(RegistryInstallError, match="is not installed"):
        registry_installer._verify_registry_set(
            root,
            (_node("example", "1.0.0"),),
        )


@pytest.mark.parametrize("kind", ["child-symlink", "special", "metadata-symlink"])
def test_registry_scanner_rejects_unsafe_immediate_entries(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "child-symlink":
        root.joinpath("linked").symlink_to(outside, target_is_directory=True)
    elif kind == "special":
        os.mkfifo(root / "fifo")
    else:
        child = root / "child"
        child.mkdir()
        metadata = outside / "pyproject.toml"
        metadata.write_text('[project]\nname="example"\nversion="1.0.0"\n')
        child.joinpath("pyproject.toml").symlink_to(metadata)

    with pytest.raises(RegistryInstallError, match=r"symlink|regular"):
        registry_installer._scan_registry_identities(root)


def test_registry_scanner_rejects_symlinked_custom_nodes_root(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-custom-nodes"
    target.mkdir()
    root = tmp_path / "custom_nodes"
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(RegistryInstallError, match="real directory"):
        registry_installer._scan_registry_identities(root)


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_registry_scanner_rejects_non_regular_root_project_metadata(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "custom_nodes"
    project = root / "node/pyproject.toml"
    project.parent.mkdir(parents=True)
    if kind == "directory":
        project.mkdir()
    else:
        os.mkfifo(project)

    with pytest.raises(RegistryInstallError, match="one regular file"):
        registry_installer._scan_registry_identities(root)


def test_registry_scanner_rejects_parent_symlink_containment_escape(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.joinpath("custom_nodes").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(RegistryInstallError, match="real directory"):
        registry_installer._scan_registry_identities(alias / "custom_nodes")


@pytest.mark.parametrize(
    "content",
    [
        b"not toml =",
        b"[project]\nname='example'\n",
        b"[project]\nname='invalid/name'\nversion='1.0.0'\n",
        b"[project]\nname='example'\nversion='not a version'\n",
    ],
)
def test_registry_scanner_rejects_invalid_root_project_metadata(
    tmp_path: Path,
    content: bytes,
) -> None:
    root = tmp_path / "custom_nodes"
    child = root / "child"
    child.mkdir(parents=True)
    child.joinpath("pyproject.toml").write_bytes(content)

    with pytest.raises(RegistryInstallError, match="invalid project identity"):
        registry_installer._scan_registry_identities(root)


def test_registry_scanner_rejects_normalized_duplicate_metadata(tmp_path: Path) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    _write_project(root, "one", "Example_Node", "1.0.0")
    _write_project(root, "two", "example.node", "2.0.0")

    with pytest.raises(RegistryInstallError, match="duplicated"):
        registry_installer._scan_registry_identities(root)


def test_registry_inventory_is_canonical_raw_and_minimal() -> None:
    content = registry_installer._registry_inventory_bytes(
        (
            _node("Example_Node", "1.0.0-rc.1+CUDA.1"),
            _node("second", "2.0.0"),
        )
    )

    assert content == (
        b'{"nodes":[{"control":"direct-cm-cli","id":"Example_Node",'
        b'"type":"registry","verification":"registry-version",'
        b'"version":"1.0.0-rc.1+CUDA.1"},{"control":"direct-cm-cli",'
        b'"id":"second","type":"registry",'
        b'"verification":"registry-version","version":"2.0.0"}],'
        b'"schema_version":1}\n'
    )
    assert list(json.loads(content)["nodes"][0]) == [
        "control",
        "id",
        "type",
        "verification",
        "version",
    ]


def test_registry_inventory_creation_is_exclusive_read_only_and_exact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry-inventory.json"
    content = registry_installer._registry_inventory_bytes((_node("a", "1.0.0"),))

    registry_installer._write_registry_inventory(
        path,
        content,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert path.read_bytes() == content
    assert stat_mode(path) == 0o444
    with pytest.raises(RegistryInstallError, match="already exists"):
        registry_installer._write_registry_inventory(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_registry_inventory_verification_failure_removes_target_and_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "registry-inventory.json"
    content = registry_installer._registry_inventory_bytes((_node("a", "1.0.0"),))
    original_read_bytes = Path.read_bytes

    def corrupt_target(item: Path) -> bytes:
        if item == path:
            return b"corrupt"
        return original_read_bytes(item)

    monkeypatch.setattr(Path, "read_bytes", corrupt_target)

    with pytest.raises(RegistryInstallError, match="verification failed"):
        registry_installer._write_registry_inventory(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert not path.exists()
    assert not list(tmp_path.glob(".registry-inventory.json.*"))


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_registry_orchestration_uses_one_process_and_admitted_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    application_document = application.model_dump(mode="python")
    application_document["python_index_url"] = "https://packages.example/simple"
    application_document["pytorch"]["python_index_url"] = (
        "https://packages.example/simple"
    )
    application_document["python_extras"]["index_url"] = (
        "https://packages.example/simple"
    )
    application = ApplicationPhase.model_validate(application_document)
    first = _node("first", "1.0.0", pre=("pre.py",), post=("post.py",))
    second = _node("second", "2.0.0", post=("one.py", "two.py"))
    custom_nodes = _phase(runtime, (first, second))
    _patch_phases(monkeypatch, application, custom_nodes)
    events: list[object] = []

    def run_command(argv, **kwargs):
        events.append(("command", tuple(str(item) for item in argv), kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(registry_installer, "run_argv", run_command)
    monkeypatch.setattr(
        registry_installer,
        "run_hook",
        lambda hook, **_kwargs: events.append(("hook", hook)),
    )
    monkeypatch.setattr(
        registry_installer,
        "_verify_registry_set",
        lambda _root, expected, **_kwargs: events.append(
            ("verify", tuple(node.id for node in expected))
        ),
    )
    monkeypatch.setattr(
        registry_installer,
        "_write_registry_inventory",
        lambda path, content: events.append(("inventory", path, content)),
    )

    registry_installer.install_registry_nodes(
        "custom.json",
        "application.json",
        expected_build_plan_digest=f"sha256:{'c' * 64}",
        runtime=runtime,
        constraints_path=tmp_path / "constraints.txt",
        environ={
            "HTTPS_PROXY": "https://proxy.test",
            "PIP_CONFIG_FILE": "/tmp/poison-pip.conf",
            "PIP_EXTRA_INDEX_URL": "https://poison-pip.example/simple",
            "PIP_INDEX_URL": "https://poison-pip.example/simple",
            "UV_CONFIG_FILE": "/tmp/poison-uv.toml",
            "UV_DEFAULT_INDEX": "https://poison-uv.example/simple",
            "UV_EXTRA_INDEX_URL": "https://poison-uv-extra.example/simple",
            "UV_INDEX": "poison=https://poison-uv.example/simple",
            "USER_VALUE": "kept-for-hooks",
        },
    )

    operations = [
        (event[0], event[1]) for event in events if event[0] in {"hook", "verify"}
    ]
    assert operations == [
        ("hook", "pre.py"),
        ("verify", ("first",)),
        ("hook", "post.py"),
        ("verify", ("first",)),
        ("verify", ("first",)),
        ("verify", ("second",)),
        ("hook", "one.py"),
        ("verify", ("first", "second")),
        ("hook", "two.py"),
        ("verify", ("first", "second")),
        ("verify", ("first", "second")),
        ("verify", ("first", "second")),
    ]
    commands = [event for event in events if event[0] == "command"]
    assert len(commands) == 3  # two cm-cli calls and the final uv pip check
    first_argv, first_kwargs = commands[0][1:]
    assert first_argv == (
        "/opt/venv/bin/cm-cli",
        "install",
        "first@1.0.0",
        "--mode",
        "cache",
        "--user-directory",
        str(runtime.comfyui_path / "user"),
        "--exit-on-fail",
    )
    assert first_kwargs["close_stdin"] is True
    assert first_kwargs["cwd"] == runtime.comfyui_path
    assert first_kwargs["env"]["UV_CONSTRAINT"].endswith("constraints.txt")
    assert first_kwargs["env"]["PIP_CONSTRAINT"].endswith("constraints.txt")
    assert first_kwargs["env"]["PIP_CONFIG_FILE"] == os.devnull
    assert first_kwargs["env"]["PIP_INDEX_URL"] == ("https://packages.example/simple")
    assert first_kwargs["env"]["UV_DEFAULT_INDEX"] == (
        "https://packages.example/simple"
    )
    assert first_kwargs["env"]["UV_NO_CONFIG"] == "1"
    assert "PIP_EXTRA_INDEX_URL" not in first_kwargs["env"]
    assert "UV_CONFIG_FILE" not in first_kwargs["env"]
    assert "UV_EXTRA_INDEX_URL" not in first_kwargs["env"]
    assert "UV_INDEX" not in first_kwargs["env"]
    assert "https://packages.example/simple" not in first_argv
    assert "USER_VALUE" not in first_kwargs["env"]
    assert events[-2][0] == "inventory"
    assert commands[-1][1][1:4] == ("--no-config", "pip", "check")


def test_false_zero_stops_before_later_registry_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(
        runtime,
        (_node("missing", "1.0.0"), _node("later", "2.0.0")),
    )
    _patch_phases(monkeypatch, application, custom_nodes)
    commands: list[tuple[str, ...]] = []

    def false_zero(argv, **_kwargs):
        commands.append(tuple(str(item) for item in argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(registry_installer, "run_argv", false_zero)

    with pytest.raises(
        RegistryInstallError,
        match=r"missing@1\.0\.0 is not installed",
    ):
        registry_installer.install_registry_nodes(
            "custom.json",
            "application.json",
            expected_build_plan_digest=f"sha256:{'c' * 64}",
            runtime=runtime,
        )

    assert [command[2] for command in commands] == ["missing@1.0.0"]


def test_future_registry_identity_is_rejected_before_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(
        runtime,
        (_node("first", "1.0.0"), _node("future", "2.0.0")),
    )
    _patch_phases(monkeypatch, application, custom_nodes)
    commands: list[str] = []

    def install(argv, **_kwargs):
        commands.append(str(argv[2]))
        root = runtime.comfyui_path / "custom_nodes"
        _write_project(root, "installed-first", "first", "1.0.0")
        _write_project(root, "installed-future", "future", "2.0.0")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(registry_installer, "run_argv", install)

    with pytest.raises(RegistryInstallError, match="admitted declaration prefix"):
        registry_installer.install_registry_nodes(
            "custom.json",
            "application.json",
            expected_build_plan_digest=f"sha256:{'c' * 64}",
            runtime=runtime,
        )

    assert commands == ["first@1.0.0"]


def test_runtime_rejects_normalized_duplicate_locked_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(
        runtime,
        (_node("Example_Node", "1.0.0"), _node("example.node", "1.0.0")),
    )
    _patch_phases(monkeypatch, application, custom_nodes)
    monkeypatch.setattr(
        registry_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("duplicate phase must not execute"),
    )

    with pytest.raises(RegistryInstallError, match="duplicated in BuildPlan"):
        registry_installer.install_registry_nodes(
            "custom.json",
            "application.json",
            expected_build_plan_digest=f"sha256:{'c' * 64}",
            runtime=runtime,
        )


def test_runtime_rejects_invalid_locked_version_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, (_node("example", "not-a-version"),))
    _patch_phases(monkeypatch, application, custom_nodes)
    monkeypatch.setattr(
        registry_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("invalid phase must not execute"),
    )

    with pytest.raises(RegistryInstallError, match="invalid locked version"):
        registry_installer.install_registry_nodes(
            "custom.json",
            "application.json",
            expected_build_plan_digest=f"sha256:{'c' * 64}",
            runtime=runtime,
        )


def test_nonzero_registry_process_stops_before_state_proof_and_later_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(
        runtime,
        (_node("failed", "1.0.0"), _node("later", "2.0.0")),
    )
    _patch_phases(monkeypatch, application, custom_nodes)
    commands: list[str] = []

    def fail(argv, **_kwargs):
        commands.append(str(argv[2]))
        raise ContainerCommandError("cm-cli failed")

    monkeypatch.setattr(registry_installer, "run_argv", fail)
    monkeypatch.setattr(
        registry_installer,
        "_verify_registry_set",
        lambda *_args: pytest.fail("state proof must not run after nonzero"),
    )

    with pytest.raises(ContainerCommandError, match="cm-cli failed"):
        registry_installer.install_registry_nodes(
            "custom.json",
            "application.json",
            expected_build_plan_digest=f"sha256:{'c' * 64}",
            runtime=runtime,
        )

    assert commands == ["failed@1.0.0"]


@pytest.mark.parametrize(
    ("mutation_point", "expected_commands"),
    [
        ("first-pre", []),
        ("first-post", ["first@1.0.0"]),
        ("second-pre", ["first@1.0.0"]),
        ("second-post", ["first@1.0.0", "second@2.0.0"]),
    ],
)
def test_hook_manager_mutation_fails_at_next_capability_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_point: str,
    expected_commands: list[str],
) -> None:
    application, runtime = _application(tmp_path)
    first = _node(
        "first",
        "1.0.0",
        pre=("mutate.py",) if mutation_point == "first-pre" else (),
        post=("mutate.py",) if mutation_point == "first-post" else (),
    )
    second = _node(
        "second",
        "2.0.0",
        pre=("mutate.py",) if mutation_point == "second-pre" else (),
        post=("mutate.py",) if mutation_point == "second-post" else (),
    )
    custom_nodes = _phase(runtime, (first, second))
    _patch_phases(monkeypatch, application, custom_nodes)
    capability = {"valid": True}
    commands: list[str] = []

    def verify_capability(*_args) -> None:
        if not capability["valid"]:
            raise ComfyUIInstallError("Manager capability was mutated")

    def mutate(_hook, **_kwargs) -> None:
        capability["valid"] = False

    def install(argv, **_kwargs):
        request = str(argv[2])
        commands.append(request)
        node_id, version = request.split("@", 1)
        _write_project(
            runtime.comfyui_path / "custom_nodes",
            f"installed-{node_id}",
            node_id,
            version,
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        registry_installer,
        "verify_manager_registry_capability",
        verify_capability,
    )
    monkeypatch.setattr(registry_installer, "run_hook", mutate)
    monkeypatch.setattr(registry_installer, "run_argv", install)

    with pytest.raises(ComfyUIInstallError, match="mutated"):
        registry_installer.install_registry_nodes(
            "custom.json",
            "application.json",
            expected_build_plan_digest=f"sha256:{'c' * 64}",
            runtime=runtime,
        )

    assert commands == expected_commands


def test_hook_cannot_retarget_requirements_and_installed_manager_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(
        runtime,
        (_node("first", "1.0.0", pre=("retarget.py",)),),
    )
    _patch_phases(monkeypatch, application, custom_nodes)
    requirements = runtime.comfyui_path / "manager_requirements.txt"
    requirements.write_text("comfyui_manager==4.0.5\n")
    installed = {"manager_version": "4.0.5"}
    distribution_proofs: list[str] = []

    def prove_distributions(_application, parsed, _runtime) -> None:
        distribution_proofs.append(parsed.manager_version)
        assert parsed.manager_version == installed["manager_version"]

    def retarget(_hook, **_kwargs) -> None:
        requirements.write_text("comfyui_manager==9.0.0\n")
        installed["manager_version"] = "9.0.0"

    monkeypatch.setattr(
        registry_installer,
        "capture_manager_registry_authority",
        comfyui_installer.capture_manager_registry_authority,
    )
    monkeypatch.setattr(
        registry_installer,
        "verify_manager_registry_capability",
        comfyui_installer.verify_manager_registry_capability,
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_declared_manager_distributions",
        prove_distributions,
    )
    monkeypatch.setattr(comfyui_installer, "_verify_cm_cli", lambda *_args: None)
    monkeypatch.setattr(registry_installer, "run_hook", retarget)
    monkeypatch.setattr(
        registry_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("retargeted authority must not execute"),
    )

    with pytest.raises(ComfyUIInstallError, match="authority changed"):
        registry_installer.install_registry_nodes(
            "custom.json",
            "application.json",
            expected_build_plan_digest=f"sha256:{'c' * 64}",
            runtime=runtime,
        )

    assert distribution_proofs == ["4.0.5"]


@pytest.mark.parametrize("mutation_phase", ["pre", "post"])
def test_hook_mutation_of_admitted_identity_fails_before_next_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_phase: str,
) -> None:
    application, runtime = _application(tmp_path)
    first = _node(
        "first",
        "1.0.0",
        post=("mutate.py",) if mutation_phase == "post" else (),
    )
    second = _node(
        "second",
        "2.0.0",
        pre=("mutate.py",) if mutation_phase == "pre" else (),
    )
    custom_nodes = _phase(runtime, (first, second))
    _patch_phases(monkeypatch, application, custom_nodes)
    commands: list[str] = []

    def install(argv, **_kwargs):
        request = str(argv[2])
        commands.append(request)
        node_id, version = request.split("@", 1)
        _write_project(
            runtime.comfyui_path / "custom_nodes",
            f"installed-{node_id}",
            node_id,
            version,
        )
        return SimpleNamespace(returncode=0)

    def mutate(_hook, **_kwargs):
        metadata = runtime.comfyui_path / "custom_nodes/installed-first/pyproject.toml"
        metadata.write_text('[project]\nname="first"\nversion="9.0.0"\n')

    monkeypatch.setattr(registry_installer, "run_argv", install)
    monkeypatch.setattr(registry_installer, "run_hook", mutate)

    with pytest.raises(RegistryInstallError, match="version does not match"):
        registry_installer.install_registry_nodes(
            "custom.json",
            "application.json",
            expected_build_plan_digest=f"sha256:{'c' * 64}",
            runtime=runtime,
        )

    assert commands == (
        ["first@1.0.0"]
        if mutation_phase == "post"
        else [
            "first@1.0.0",
            "second@2.0.0",
        ]
    )
