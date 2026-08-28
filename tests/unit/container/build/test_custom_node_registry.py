"""Custom-node registry contracts."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_docker_helper.config.evidence.custom_nodes import custom_node_inventory
from comfyui_docker_helper.config.planning.build_plan import (
    ApplicationPhase,
    RegistryNodePlan,
)
from comfyui_docker_helper.container.build.custom_nodes import (
    orchestrator as custom_node_installer,
)
from comfyui_docker_helper.container.build.custom_nodes import (
    registry as registry_installer,
)
from comfyui_docker_helper.container.build.custom_nodes.contracts import (
    CustomNodeInstallError,
)
from tests.container_installer_support import (
    _node,
    _write_project,
)
from tests.container_installer_support import (
    application as _application,
)
from tests.container_installer_support import (
    custom_nodes_phase as _phase,
)
from tests.container_installer_support import (
    patch_phases as _patch_phases,
)


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

    registry_installer._verify_registry_set(
        root,
        (_node("Example_Node", "1.0.0-rc.1+cuda.1"),),
    )

    with pytest.raises(CustomNodeInstallError, match="version does not match"):
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

    with pytest.raises(CustomNodeInstallError, match="is not installed"):
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

    with pytest.raises(CustomNodeInstallError, match=r"symlink|regular"):
        registry_installer._scan_registry_identities(root)


def test_registry_scanner_rejects_symlinked_custom_nodes_root(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-custom-nodes"
    target.mkdir()
    root = tmp_path / "custom_nodes"
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(CustomNodeInstallError, match="real directory"):
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

    with pytest.raises(CustomNodeInstallError, match="one regular file"):
        registry_installer._scan_registry_identities(root)


def test_registry_scanner_rejects_parent_symlink_containment_escape(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.joinpath("custom_nodes").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(CustomNodeInstallError, match="real directory"):
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

    with pytest.raises(CustomNodeInstallError, match="invalid project identity"):
        registry_installer._scan_registry_identities(root)


def test_registry_scanner_rejects_normalized_duplicate_metadata(tmp_path: Path) -> None:
    root = tmp_path / "custom_nodes"
    root.mkdir()
    _write_project(root, "one", "Example_Node", "1.0.0")
    _write_project(root, "two", "example.node", "2.0.0")

    with pytest.raises(CustomNodeInstallError, match="duplicated"):
        registry_installer._scan_registry_identities(root)


def test_final_observer_rejects_post_install_registry_identity_drift(
    tmp_path: Path,
) -> None:
    _application_phase, runtime = _application(tmp_path)
    node = _node("Example_Node", "1.0.0")
    custom_nodes = _phase(runtime, (node,))
    project = _write_project(
        runtime.comfyui_path / "custom_nodes",
        "installed-example",
        "example.node",
        "1.0.0",
    )

    assert custom_node_installer.observe_custom_node_state(
        custom_nodes,
        runtime=runtime,
    ) == custom_node_inventory((node,))

    project.joinpath("pyproject.toml").write_text(
        '[project]\nname = "example.node"\nversion = "2.0.0"\n'
    )
    with pytest.raises(CustomNodeInstallError, match="version does not match"):
        custom_node_installer.observe_custom_node_state(
            custom_nodes,
            runtime=runtime,
        )


def test_final_observer_scans_exact_empty_registry_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _application_phase, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, ())
    calls: list[tuple[Path, tuple[RegistryNodePlan, ...], tuple[Path, ...]]] = []
    monkeypatch.setattr(
        registry_installer,
        "_verify_registry_set",
        lambda root, expected, **kwargs: calls.append(
            (root, tuple(expected), tuple(kwargs["excluded_git_targets"]))
        ),
    )

    evidence = custom_node_installer.observe_custom_node_state(
        custom_nodes,
        runtime=runtime,
    )

    assert calls == [(runtime.comfyui_path / "custom_nodes", (), ())]
    assert evidence == custom_node_inventory(())


def test_registry_orchestration_uses_shared_managed_python_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    application_document = application.model_dump(mode="python")
    application_document["python_index_url"] = "https://packages.example/simple"
    application_document["pytorch"]["python_index_url"] = (
        "https://packages.example/simple"
    )
    application_document["pytorch"]["pytorch_index_url"] = (
        "https://pytorch.example/whl/cu130"
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
    build_constraint_snapshots: list[tuple[Path, bytes]] = []

    def run_command(argv, **kwargs):
        build_constraints = Path(kwargs["env"]["UV_BUILD_CONSTRAINT"])
        build_constraint_snapshots.append(
            (build_constraints, build_constraints.read_bytes())
        )
        events.append(("command", tuple(str(item) for item in argv), kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(registry_installer, "run_argv", run_command)
    monkeypatch.setattr(
        custom_node_installer,
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
        custom_node_installer,
        "observe_application_state",
        lambda *_args, **_kwargs: events.append(("application-check",)),
    )

    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        constraints_path=tmp_path / "constraints.txt",
        environ={
            "HTTPS_PROXY": "https://proxy.test",
            "PIP_CONFIG_FILE": "/tmp/poison-pip.conf",
            "PIP_BUILD_CONSTRAINT": "/tmp/poison-pip-build-constraints.txt",
            "PIP_CONSTRAINT": "/tmp/poison-pip-constraints.txt",
            "PIP_EXTRA_INDEX_URL": "https://poison-pip.example/simple",
            "PIP_INDEX_URL": "https://poison-pip.example/simple",
            "UV_CONFIG_FILE": "/tmp/poison-uv.toml",
            "UV_CACHE_DIR": "/tmp/poison-uv-cache",
            "UV_BUILD_CONSTRAINT": "/tmp/poison-uv-build-constraints.txt",
            "UV_CONSTRAINT": "/tmp/poison-uv-constraints.txt",
            "UV_DEFAULT_INDEX": "https://poison-uv.example/simple",
            "UV_EXTRA_INDEX_URL": "https://poison-uv-extra.example/simple",
            "UV_INDEX": "poison=https://poison-uv.example/simple",
            "UV_INDEX_STRATEGY": "first-index",
            "UV_LINK_MODE": "hardlink",
            "UV_NO_CONFIG": "0",
            "PATH": "/poison/bin",
            "LIBRARY_PATH": "/usr/local/cuda/lib64/stubs",
            "CUDA_HOME": "/usr/local/cuda",
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
    constraints_path = os.fspath(tmp_path / "constraints.txt")
    assert first_kwargs["env"]["PIP_CONFIG_FILE"] == os.devnull
    assert first_kwargs["env"]["PIP_INDEX_URL"] == ("https://packages.example/simple")
    assert first_kwargs["env"]["PIP_EXTRA_INDEX_URL"] == (
        "https://pytorch.example/whl/cu130"
    )
    assert first_kwargs["env"]["UV_DEFAULT_INDEX"] == (
        "https://packages.example/simple"
    )
    assert first_kwargs["env"]["UV_INDEX"] == "https://pytorch.example/whl/cu130"
    assert first_kwargs["env"]["PIP_CONSTRAINT"] == constraints_path
    assert first_kwargs["env"]["UV_CONSTRAINT"] == constraints_path
    build_constraints_path = first_kwargs["env"]["PIP_BUILD_CONSTRAINT"]
    assert build_constraints_path == first_kwargs["env"]["UV_BUILD_CONSTRAINT"]
    assert build_constraints_path != constraints_path
    assert first_kwargs["env"]["UV_INDEX_STRATEGY"] == "unsafe-best-match"
    assert first_kwargs["env"]["UV_NO_CONFIG"] == "1"
    assert first_kwargs["env"]["UV_CACHE_DIR"] == "/root/.cache/uv"
    assert first_kwargs["env"]["UV_LINK_MODE"] == "copy"
    assert first_kwargs["env"]["PATH"] == (
        "/opt/venv/bin:/usr/local/bin:/usr/local/cuda/bin:/usr/bin:/bin"
    )
    assert first_kwargs["env"]["LIBRARY_PATH"] == ("/usr/local/cuda/lib64/stubs")
    assert first_kwargs["env"]["CUDA_HOME"] == "/usr/local/cuda"
    assert first_kwargs["env"]["COMFYUI_PATH"] == str(runtime.comfyui_path)
    assert first_kwargs["env"]["VIRTUAL_ENV"] == str(runtime.virtual_env)
    assert first_kwargs["env"]["WORKSPACE"] == str(runtime.workspace)
    assert first_kwargs["env"]["HTTPS_PROXY"] == "https://proxy.test"
    assert "UV_CONFIG_FILE" not in first_kwargs["env"]
    assert "UV_EXTRA_INDEX_URL" not in first_kwargs["env"]
    assert "https://packages.example/simple" not in first_argv
    assert "USER_VALUE" not in first_kwargs["env"]
    assert {content for _path, content in build_constraint_snapshots} == {
        b"torch==2.12.1+cu130\ntorchaudio==2.11.0+cu130\ntorchvision==0.27.1+cu130\n"
    }
    assert {path for path, _content in build_constraint_snapshots} == {
        Path(build_constraints_path)
    }
    assert not Path(build_constraints_path).exists()
    assert events[-1] == ("application-check",)
