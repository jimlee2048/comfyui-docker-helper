"""Registry and direct-Git custom-node orchestration contracts."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.test_build_plan import accepted_resolution, build_plan, final_config

from comfyui_docker_helper.comfyui_requirements import ParsedComfyUIRequirements
from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    CustomNodePlan,
    CustomNodesPhase,
    GitNodePlan,
    HookPlan,
    RegistryNodePlan,
)
from comfyui_docker_helper.container import comfyui_installer, custom_node_installer
from comfyui_docker_helper.container.comfyui_installer import ComfyUIInstallError
from comfyui_docker_helper.container.custom_node_installer import (
    CustomNodeInstallError,
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
    # Runtime tests construct already-admitted phase values, including forgeries.
    return RegistryNodePlan.model_construct(
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


def _git_node(
    runtime: ContainerRuntime,
    name: str = "direct",
    *,
    url: str = "https://example.invalid/Raw/Node.git",
    pre: tuple[str, ...] = (),
    post: tuple[str, ...] = (),
) -> GitNodePlan:
    # Runtime tests construct already-admitted phase values, including forgeries.
    return GitNodePlan.model_construct(
        type="git",
        url=url,
        commit="c" * 40,
        target=str(runtime.comfyui_path / "custom_nodes" / name),
        pre_install=tuple(
            HookPlan(relative_path=value, digest=f"sha256:{'c' * 64}") for value in pre
        ),
        post_install=tuple(
            HookPlan(relative_path=value, digest=f"sha256:{'d' * 64}") for value in post
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
    plan = build_plan(final_config(), accepted_resolution())
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
    nodes: tuple[CustomNodePlan, ...],
) -> CustomNodesPhase:
    return CustomNodesPhase(
        install_manager=True,
        user_directory=str(runtime.comfyui_path / "user"),
        custom_node_inventory="/opt/cdh/build/custom-node-inventory.json",
        nodes=nodes,
    )


def _patch_phases(
    monkeypatch: pytest.MonkeyPatch,
    application: ApplicationPhase,
    custom_nodes: CustomNodesPhase,
) -> None:
    monkeypatch.setattr(
        custom_node_installer,
        "capture_application_requirements",
        lambda *_args: ParsedComfyUIRequirements(
            digest=f"sha256:{'a' * 64}",
            protected=(),
            ordinary=("requests>=2",),
        ),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "capture_manager_registry_authority",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "verify_manager_registry_authority",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_registry_capability",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        lambda *_args, **_kwargs: None,
    )


# Mixed custom-node installation preserves exact authority at every mutation boundary.
def test_registry_version_comparison_preserves_raw_complete_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    root.joinpath("example.py").write_text("# built-in example\n")
    root.joinpath("unmanaged-node").mkdir()
    _write_project(
        root,
        "package",
        "Example.Node",
        "1.0.0rc1+CUDA.1",
    )

    custom_node_installer._verify_registry_set(
        root,
        (_node("Example_Node", "1.0.0-rc.1+cuda.1"),),
    )

    with pytest.raises(CustomNodeInstallError, match="version does not match"):
        custom_node_installer._verify_registry_set(
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

    with pytest.raises(CustomNodeInstallError, match="is not installed"):
        custom_node_installer._verify_registry_set(
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

    with pytest.raises(CustomNodeInstallError, match=r"symlink|regular"):
        custom_node_installer._scan_registry_identities(root)


def test_registry_scanner_rejects_symlinked_custom_nodes_root(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-custom-nodes"
    target.mkdir()
    root = tmp_path / "custom_nodes"
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(CustomNodeInstallError, match="real directory"):
        custom_node_installer._scan_registry_identities(root)


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

    with pytest.raises(CustomNodeInstallError, match="one regular file"):
        custom_node_installer._scan_registry_identities(root)


def test_registry_scanner_rejects_parent_symlink_containment_escape(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.joinpath("custom_nodes").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(CustomNodeInstallError, match="real directory"):
        custom_node_installer._scan_registry_identities(alias / "custom_nodes")


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

    with pytest.raises(CustomNodeInstallError, match="invalid project identity"):
        custom_node_installer._scan_registry_identities(root)


def test_registry_scanner_rejects_normalized_duplicate_metadata(tmp_path: Path) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    _write_project(root, "one", "Example_Node", "1.0.0")
    _write_project(root, "two", "example.node", "2.0.0")

    with pytest.raises(CustomNodeInstallError, match="duplicated"):
        custom_node_installer._scan_registry_identities(root)


# Inventory evidence is canonical, declaration-ordered, exclusive, and exact.
def test_custom_node_inventory_is_canonical_raw_ordered_and_minimal() -> None:
    content = custom_node_installer._custom_node_inventory_bytes(
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


def test_mixed_and_empty_inventory_bytes_preserve_typed_declaration_order(
    tmp_path: Path,
) -> None:
    _application_phase, runtime = _application(tmp_path)
    git = _git_node(runtime, url="https://example.invalid/Raw/Node.git/")

    assert custom_node_installer._custom_node_inventory_bytes(()) == (
        b'{"nodes":[],"schema_version":1}\n'
    )
    assert custom_node_installer._custom_node_inventory_bytes(
        (_node("Example_Node", "1.0.0"), git)
    ) == (
        b'{"nodes":[{"control":"direct-cm-cli","id":"Example_Node",'
        b'"type":"registry","verification":"registry-version",'
        b'"version":"1.0.0"},{"commit":"cccccccccccccccccccccccccccccccccccccccc",'
        b'"control":"direct-git","target":"direct","type":"git",'
        b'"url":"https://example.invalid/Raw/Node.git/",'
        b'"verification":"git-commit"}],"schema_version":1}\n'
    )


def test_custom_node_inventory_creation_is_exclusive_read_only_and_exact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "custom-node-inventory.json"
    content = custom_node_installer._custom_node_inventory_bytes((_node("a", "1.0.0"),))

    custom_node_installer._write_custom_node_inventory(
        path,
        content,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert path.read_bytes() == content
    assert stat_mode(path) == 0o444
    with pytest.raises(CustomNodeInstallError, match="already exists"):
        custom_node_installer._write_custom_node_inventory(
            path,
            content,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_empty_plan_writes_exact_inventory_then_checks_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, ())
    _patch_phases(monkeypatch, application, custom_nodes)
    unrelated = runtime.comfyui_path / "custom_nodes/unrelated"
    unrelated.mkdir()
    unrelated.joinpath("pyproject.toml").write_text("not valid toml =")
    events: list[object] = []
    monkeypatch.setattr(
        custom_node_installer,
        "capture_manager_registry_authority",
        lambda *_args: pytest.fail("empty plan must not capture Manager"),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_run_git",
        lambda *_args, **_kwargs: pytest.fail("empty plan must not invoke Git"),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_scan_registry_identities",
        lambda *_args, **_kwargs: pytest.fail(
            "empty plan must not scan unrelated children"
        ),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "run_hook",
        lambda *_args, **_kwargs: pytest.fail("empty plan must not invoke hooks"),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_verify_mixed_state",
        lambda *_args, **_kwargs: events.append(("final-typed-boundary",)),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_write_custom_node_inventory",
        lambda path, content: events.append(("inventory", path, content)),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        lambda *_args, **kwargs: events.append(
            ("application", bool(kwargs.get("write_inventory")))
        ),
    )

    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
    )

    assert events == [
        ("final-typed-boundary",),
        (
            "inventory",
            Path("/opt/cdh/build/custom-node-inventory.json"),
            b'{"nodes":[],"schema_version":1}\n',
        ),
        ("application", True),
    ]


# Runtime admission rejects forged plan identities before invoking installers.
@pytest.mark.parametrize("url", ["-option", "file:///tmp/node.git", "bad"])
def test_runtime_rejects_forged_unsupported_git_locator(
    tmp_path: Path,
    url: str,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, (_git_node(runtime, url=url),))

    with pytest.raises(CustomNodeInstallError, match="URL is invalid"):
        custom_node_installer._validate_inputs(custom_nodes, application, runtime)


def test_git_installer_runs_only_root_requirements_then_install_py(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path / "custom_nodes/direct"
    target.mkdir()
    target.joinpath("requirements.txt").write_text("example==1.0\n")
    target.joinpath("install.py").write_text("print('root')\n")
    nested = target / "nested"
    nested.mkdir()
    nested.joinpath("requirements.txt").write_text("must-not-run==9\n")
    nested.joinpath("install.py").write_text("raise RuntimeError\n")
    node = _git_node(runtime)
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    events: list[str] = []

    def run(argv, **kwargs):
        commands.append((tuple(os.fspath(item) for item in argv), kwargs))
        events.append("requirements" if "--requirements" in argv else "install.py")

    monkeypatch.setattr(custom_node_installer, "run_argv", run)
    constraints = tmp_path / "constraints.txt"
    custom_node_installer._install_git_root_surfaces(
        node,
        target,
        application,
        runtime,
        Path("/usr/local/bin/uv"),
        constraints,
        {"PIP_CONSTRAINT": str(constraints), "UV_CONSTRAINT": str(constraints)},
    )

    assert len(commands) == 2
    assert events == ["requirements", "install.py"]
    requirements_argv, requirements_kwargs = commands[0]
    assert requirements_argv == (
        "/usr/local/bin/uv",
        "--no-config",
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--no-python-downloads",
        "--default-index",
        application.python_index_url,
        "--constraint",
        str(constraints),
        "--requirements",
        str(target / "requirements.txt"),
    )
    assert requirements_kwargs["close_stdin"] is True
    install_argv, install_kwargs = commands[1]
    assert install_argv == ("/opt/venv/bin/python", str(target / "install.py"))
    assert install_kwargs["close_stdin"] is True


def test_git_root_installer_rejects_symlinked_surface(tmp_path: Path) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path / "custom_nodes/direct"
    target.mkdir()
    outside = tmp_path / "requirements.txt"
    outside.write_text("example==1.0\n")
    target.joinpath("requirements.txt").symlink_to(outside)

    with pytest.raises(CustomNodeInstallError, match="one regular file"):
        custom_node_installer._install_git_root_surfaces(
            _git_node(runtime),
            target,
            application,
            runtime,
            Path("/usr/local/bin/uv"),
            tmp_path / "constraints.txt",
            {},
        )


@pytest.mark.parametrize(
    "requirement",
    [
        "--index-url https://packages.invalid/simple\nexample==1\n",
        "--extra-index-url https://packages.invalid/simple\nexample==1\n",
        "-r nested.txt\n",
        "-c constraints.txt\n",
        "example @ https://packages.invalid/example.whl\n",
    ],
)
def test_git_requirements_reject_source_control_before_any_install_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requirement: str,
) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path / "custom_nodes/direct"
    target.mkdir()
    target.joinpath("requirements.txt").write_text(requirement)
    target.joinpath("install.py").write_text("raise RuntimeError\n")
    monkeypatch.setattr(
        custom_node_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("invalid requirements must not execute"),
    )

    with pytest.raises(CustomNodeInstallError, match="requirements are invalid"):
        custom_node_installer._install_git_root_surfaces(
            _git_node(runtime),
            target,
            application,
            runtime,
            Path("/usr/local/bin/uv"),
            tmp_path / "constraints.txt",
            {},
        )


# Mixed execution preserves declaration order and every typed mutation boundary.
def test_mixed_executor_preserves_one_original_order_and_hook_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    nodes: tuple[CustomNodePlan, ...] = (
        _node("first", "1.0.0", post=("first-post.py",)),
        _git_node(runtime, pre=("git-pre.py",), post=("git-post.py",)),
        _node("last", "2.0.0"),
    )
    custom_nodes = _phase(runtime, nodes)
    _patch_phases(monkeypatch, application, custom_nodes)
    events: list[object] = []
    observed_git_environment: dict[str, str] = {}

    def names(items: Sequence[CustomNodePlan]) -> tuple[str, ...]:
        return tuple(
            item.id if isinstance(item, RegistryNodePlan) else item.target
            for item in items
        )

    monkeypatch.setattr(
        custom_node_installer,
        "_verify_mixed_state",
        lambda _root, admitted, future, **_kwargs: events.append(
            ("proof", names(admitted), names(future))
        ),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_install_registry_node",
        lambda node, *_args: events.append(("install", node.id)),
    )

    def install_git(node, *args) -> None:
        observed_git_environment.update(args[6])
        events.append(("install", Path(node.target).name))

    monkeypatch.setattr(custom_node_installer, "_install_git_node", install_git)
    monkeypatch.setattr(
        custom_node_installer,
        "run_hook",
        lambda hook, **_kwargs: events.append(("hook", hook)),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        lambda *_args, **kwargs: events.append(
            ("application", True)
            if kwargs.get("write_inventory")
            else ("application-check",)
        ),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_registry_capability",
        lambda *_args: events.append(("manager-check",)),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_write_custom_node_inventory",
        lambda *_args: events.append(("inventory",)),
    )

    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        environ={"GIT_SSH_COMMAND": "ssh -F /tmp/user-config", "HOME": "/user/home"},
    )

    assert [event for event in events if event[0] == "install"] == [
        ("install", "first"),
        ("install", "direct"),
        ("install", "last"),
    ]
    assert (
        events.index(("hook", "git-pre.py"))
        < events.index(("install", "direct"))
        < events.index(("hook", "git-post.py"))
    )
    assert events[-4:] == [
        ("proof", names(nodes), ()),
        ("manager-check",),
        ("inventory",),
        ("application", True),
    ]
    assert len([event for event in events if event[0] == "proof"]) == 16
    assert events.count(("application-check",)) == 7
    assert events.count(("manager-check",)) == 7
    assert events.index(("application-check",)) < events.index(("install", "first"))
    assert observed_git_environment["GIT_SSH_COMMAND"] == ("ssh -F /tmp/user-config")
    assert observed_git_environment["HOME"] == "/user/home"


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

    monkeypatch.setattr(custom_node_installer, "run_argv", run_command)
    monkeypatch.setattr(
        custom_node_installer,
        "run_hook",
        lambda hook, **_kwargs: events.append(("hook", hook)),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_verify_registry_set",
        lambda _root, expected, **_kwargs: events.append(
            ("verify", tuple(node.id for node in expected))
        ),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_write_custom_node_inventory",
        lambda path, content: events.append(("inventory", path, content)),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        lambda *_args, **kwargs: events.append(
            ("application", True)
            if kwargs.get("write_inventory")
            else ("application-check",)
        ),
    )

    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
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
        ("verify", ()),
        ("hook", "pre.py"),
        ("verify", ()),
        ("verify", ()),
        ("verify", ("first",)),
        ("hook", "post.py"),
        ("verify", ("first",)),
        ("verify", ("first",)),
        ("verify", ("first",)),
        ("verify", ("first",)),
        ("verify", ("first", "second")),
        ("hook", "one.py"),
        ("verify", ("first", "second")),
        ("hook", "two.py"),
        ("verify", ("first", "second")),
        ("verify", ("first", "second")),
        ("verify", ("first", "second")),
    ]
    commands = [event for event in events if event[0] == "command"]
    assert len(commands) == 2
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
    assert events[-1] == ("application", True)


def test_empty_hook_phases_reuse_observations_and_force_fresh_final_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, (_node("only", "1.0.0"),))
    _patch_phases(monkeypatch, application, custom_nodes)
    events: list[tuple[str, object]] = []
    manager_observations = 0
    application_git_paths: list[Path] = []
    application_git_path = tmp_path / "custom-git"

    monkeypatch.setattr(
        custom_node_installer,
        "_verify_mixed_state",
        lambda *_args, **_kwargs: events.append(("typed-boundary", None)),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_install_registry_node",
        lambda *_args: events.append(("process", "cm-cli")),
    )

    def observe_manager(*_args) -> None:
        nonlocal manager_observations
        manager_observations += 1
        events.append(("manager-observation", manager_observations))

    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_registry_capability",
        observe_manager,
    )

    def observe_application(*_args, **kwargs) -> None:
        application_git_paths.append(kwargs["git_path"])
        events.append(("application-observation", bool(kwargs.get("write_inventory"))))

    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        observe_application,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_write_custom_node_inventory",
        lambda *_args: events.append(("custom-node-inventory", None)),
    )

    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        git_path=application_git_path,
    )

    assert events.count(("typed-boundary", None)) == 5
    assert [event for event in events if event[0] == "manager-observation"] == [
        ("manager-observation", 1),
        ("manager-observation", 2),
    ]
    assert [event for event in events if event[0] == "application-observation"] == [
        ("application-observation", False),
        ("application-observation", False),
        ("application-observation", True),
    ]
    assert application_git_paths == [application_git_path] * 3
    assert events[-4:] == [
        ("typed-boundary", None),
        ("manager-observation", 2),
        ("custom-node-inventory", None),
        ("application-observation", True),
    ]


# Prefix and future-target proofs fail closed before admitting later node identities.
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

    monkeypatch.setattr(custom_node_installer, "run_argv", false_zero)

    with pytest.raises(
        CustomNodeInstallError,
        match=r"missing@1\.0\.0 is not installed",
    ):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
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

    monkeypatch.setattr(custom_node_installer, "run_argv", install)

    with pytest.raises(CustomNodeInstallError, match="admitted declaration prefix"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )

    assert commands == ["first@1.0.0"]


def test_mixed_proof_excludes_admitted_git_only_after_fresh_git_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    git = _git_node(runtime)
    target = Path(git.target)
    target.mkdir()
    target.joinpath("pyproject.toml").write_text(
        '[project]\nname="git-project"\nversion="1.0.0"\n'
    )
    events: list[str] = []

    monkeypatch.setattr(
        custom_node_installer,
        "verify_manager_registry_authority",
        lambda *_args: events.append("manager"),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "_verify_git_provenance",
        lambda *_args, **_kwargs: events.append("git"),
    )

    def verify_registry(_root, expected, *, excluded_git_targets=()):
        events.append("registry")
        assert expected == ()
        assert excluded_git_targets == [target]

    monkeypatch.setattr(custom_node_installer, "_verify_registry_set", verify_registry)

    custom_node_installer._verify_mixed_state(
        runtime.comfyui_path / "custom_nodes",
        (git,),
        (),
        application=application,
        runtime=runtime,
        manager_authority=object(),
        git_path=Path("/usr/bin/git"),
        git_environment={},
    )

    assert events == ["manager", "git", "registry"]


def test_future_git_target_is_rejected_before_its_pre_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    git = _git_node(runtime)
    Path(git.target).mkdir()
    monkeypatch.setattr(
        custom_node_installer,
        "verify_manager_registry_authority",
        lambda *_args: None,
    )

    with pytest.raises(CustomNodeInstallError, match="future Git target"):
        custom_node_installer._verify_mixed_state(
            runtime.comfyui_path / "custom_nodes",
            (),
            (git,),
            application=application,
            runtime=runtime,
            manager_authority=object(),
            git_path=Path("/usr/bin/git"),
            git_environment={},
        )


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
        custom_node_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("duplicate phase must not execute"),
    )

    with pytest.raises(CustomNodeInstallError, match="duplicated in BuildPlan"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
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
        custom_node_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("invalid phase must not execute"),
    )

    with pytest.raises(CustomNodeInstallError, match="invalid locked version"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
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

    monkeypatch.setattr(custom_node_installer, "run_argv", fail)
    monkeypatch.setattr(
        custom_node_installer,
        "_verify_registry_set",
        lambda _root, expected, **_kwargs: (
            pytest.fail("state proof must not run after nonzero") if expected else None
        ),
    )

    with pytest.raises(ContainerCommandError, match="cm-cli failed"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
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
# Hook mutations dirty observations and force fresh application, Manager, and
# node proofs.
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
        custom_node_installer,
        "observe_manager_registry_capability",
        verify_capability,
    )
    monkeypatch.setattr(custom_node_installer, "run_hook", mutate)
    monkeypatch.setattr(custom_node_installer, "run_argv", install)

    with pytest.raises(ComfyUIInstallError, match="mutated"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )

    assert commands == expected_commands


def test_first_pre_hook_manager_mutation_stops_before_second_pre_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(
        runtime,
        (_node("first", "1.0.0", pre=("first.py", "second.py")),),
    )
    _patch_phases(monkeypatch, application, custom_nodes)
    valid = {"manager": True}
    hooks: list[str] = []

    def verify(*_args) -> None:
        if not valid["manager"]:
            raise ComfyUIInstallError("Manager capability was mutated")

    def hook(name: str, **_kwargs) -> None:
        hooks.append(name)
        valid["manager"] = False

    monkeypatch.setattr(
        custom_node_installer, "observe_manager_registry_capability", verify
    )
    monkeypatch.setattr(custom_node_installer, "run_hook", hook)
    monkeypatch.setattr(
        custom_node_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("node install must not begin"),
    )

    with pytest.raises(ComfyUIInstallError, match="mutated"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )

    assert hooks == ["first.py"]


def test_application_observation_failure_stops_before_second_pre_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(
        runtime,
        (_node("first", "1.0.0", pre=("first.py", "second.py")),),
    )
    _patch_phases(monkeypatch, application, custom_nodes)
    hooks: list[str] = []
    observations = 0

    monkeypatch.setattr(
        custom_node_installer,
        "run_hook",
        lambda name, **_kwargs: hooks.append(name),
    )

    def fail_observation(*_args, **_kwargs) -> None:
        nonlocal observations
        observations += 1
        if observations > 1:
            raise CustomNodeInstallError("application observation failed")

    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        fail_observation,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("node install must not begin"),
    )

    with pytest.raises(CustomNodeInstallError, match="observation failed"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )

    assert hooks == ["first.py"]
    assert observations == 2


def test_real_hook_is_reproved_before_the_next_cm_cli_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, (_node("first", "1.0.0", pre=("mutate.sh",)),))
    _patch_phases(monkeypatch, application, custom_nodes)
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "mutate.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    events: list[str] = []

    def prove(*_args) -> None:
        events.append("proof-after-hook" if marker.exists() else "proof-before-hook")

    def install(argv, **_kwargs) -> None:
        events.append("cm-cli")
        node_id, version = str(argv[2]).split("@", 1)
        _write_project(
            runtime.comfyui_path / "custom_nodes",
            f"installed-{node_id}",
            node_id,
            version,
        )

    monkeypatch.setattr(
        custom_node_installer, "observe_manager_registry_capability", prove
    )
    monkeypatch.setattr(custom_node_installer, "run_argv", install)
    monkeypatch.setattr(
        custom_node_installer, "_write_custom_node_inventory", lambda *_args: None
    )

    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        hooks_directory=tmp_path,
    )

    assert marker.exists()
    assert events.index("proof-after-hook") < events.index("cm-cli")


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
        custom_node_installer,
        "capture_manager_registry_authority",
        comfyui_installer.capture_manager_registry_authority,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "verify_manager_registry_authority",
        comfyui_installer.verify_manager_registry_authority,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_registry_capability",
        comfyui_installer.observe_manager_registry_capability,
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_declared_manager_distributions",
        prove_distributions,
    )
    monkeypatch.setattr(comfyui_installer, "_verify_cm_cli", lambda *_args: None)
    monkeypatch.setattr(
        comfyui_installer, "run_application_checker", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(custom_node_installer, "run_hook", retarget)
    monkeypatch.setattr(
        custom_node_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("retargeted authority must not execute"),
    )

    with pytest.raises(ComfyUIInstallError, match="authority changed"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
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

    monkeypatch.setattr(custom_node_installer, "run_argv", install)
    monkeypatch.setattr(custom_node_installer, "run_hook", mutate)

    with pytest.raises(CustomNodeInstallError, match="version does not match"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )

    assert commands == ["first@1.0.0"]
