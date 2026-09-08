"""Local-node sequencing, prepared-state proof, and observation scope."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_docker_helper.config.planning.build_plan import HookPlan
from comfyui_docker_helper.container.build.custom_nodes import (
    git,
    local,
    orchestrator,
    registry,
    root_install,
)
from comfyui_docker_helper.container.build.custom_nodes.contracts import (
    CustomNodeInstallError,
)
from comfyui_docker_helper.container.build.events import LocalCustomNodeStarted
from tests.container_installer_support import (
    application,
    custom_nodes_phase,
    git_node,
    local_node,
    patch_phases,
    registry_node,
    write_project,
)


@pytest.mark.parametrize("failure", [None, "copy", "pre", "install", "post"])
def test_three_sources_retain_order_and_fail_fast(tmp_path, monkeypatch, failure):
    app, runtime = application(tmp_path)
    source = tmp_path / "source"
    source.mkdir()

    def hook(name):
        return HookPlan(relative_path=name, digest=f"sha256:{'a' * 64}")

    node = local_node(runtime, source, pre=(hook("pre.py"),), post=(hook("post.py"),))
    nodes = (
        registry_node("first", "1.0.0"),
        git_node(runtime),
        node,
        registry_node("last", "1.0.0"),
    )
    phase = custom_nodes_phase(runtime, nodes)
    patch_phases(monkeypatch, app, phase)
    operations = []

    def operation(name):
        operations.append(name)
        if name == failure:
            raise CustomNodeInstallError("intentional node failure")

    def prepare_git(node, root, *_args):
        operation("git-copy")
        target = Path(node.target)
        target.mkdir()
        return target

    def prepare_local(node, root):
        operation("copy")
        target = Path(node.target)
        # Metadata resembling a Registry node must be excluded only after root proof.
        write_project(root, target.name, "ordinary-local-project", "1.0.0")
        return target

    def install_registry(node, *_args):
        operation(node.id)
        write_project(
            runtime.comfyui_path / "custom_nodes", node.id, node.id, node.version
        )

    monkeypatch.setattr(git, "_prepare_git_node", prepare_git)
    monkeypatch.setattr(git, "_verify_git_provenance", lambda *_args: None)
    monkeypatch.setattr(local, "prepare_local_node", prepare_local)
    monkeypatch.setattr(registry, "_install_registry_node", install_registry)
    monkeypatch.setattr(
        root_install,
        "install_root_surfaces",
        lambda description, *_args: operation(
            "git-install" if description.startswith("Git") else "install"
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "run_hook",
        lambda name, **_kwargs: operation(name.removesuffix(".py")),
    )
    events = []
    if failure is None:
        orchestrator.install_custom_nodes(
            phase, app, runtime=runtime, event_sink=SimpleNamespace(emit=events.append)
        )
    else:
        with pytest.raises(CustomNodeInstallError, match="intentional"):
            orchestrator.install_custom_nodes(
                phase,
                app,
                runtime=runtime,
                event_sink=SimpleNamespace(emit=events.append),
            )
    expected = [
        "first",
        "git-copy",
        "git-install",
        "copy",
        "pre",
        "install",
        "post",
        "last",
    ]
    assert operations == (
        expected if failure is None else expected[: expected.index(failure) + 1]
    )
    assert [event for event in events if isinstance(event, LocalCustomNodeStarted)] == [
        LocalCustomNodeStarted(
            index=3,
            total=4,
            target_name="local-node",
            pre_hook_count=1,
            post_hook_count=1,
        )
    ]


def test_prepared_local_link_fails_before_hooks_or_registry_exclusion(
    tmp_path, monkeypatch
):
    app, runtime = application(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    hook = HookPlan(relative_path="pre.py", digest=f"sha256:{'a' * 64}")
    node = local_node(runtime, source, pre=(hook,))
    phase = custom_nodes_phase(runtime, (node, registry_node("later", "1.0.0")))
    patch_phases(monkeypatch, app, phase)
    exclusions = []

    def prepare(node, root):
        target = Path(node.target)
        target.symlink_to(source, target_is_directory=True)
        return target

    monkeypatch.setattr(local, "prepare_local_node", prepare)
    monkeypatch.setattr(
        orchestrator,
        "run_hook",
        lambda *_args, **_kwargs: pytest.fail("unproved local root must not execute"),
    )
    monkeypatch.setattr(
        registry,
        "_verify_registry_set",
        lambda _root, _expected, *, excluded_direct_targets: exclusions.append(
            tuple(excluded_direct_targets)
        ),
    )
    with pytest.raises(CustomNodeInstallError, match="real directory"):
        orchestrator.install_custom_nodes(phase, app, runtime=runtime)
    assert exclusions == [()]


def test_empty_local_hook_phases_reuse_adjacent_observations(tmp_path, monkeypatch):
    app, runtime = application(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    node = local_node(runtime, source)
    phase = custom_nodes_phase(runtime, (node,))
    patch_phases(monkeypatch, app, phase)
    operations = []

    def prepare(node, root):
        operations.append("copy")
        target = Path(node.target)
        target.mkdir()
        return target

    monkeypatch.setattr(local, "prepare_local_node", prepare)
    monkeypatch.setattr(
        root_install,
        "install_root_surfaces",
        lambda *_args: operations.append("install"),
    )
    monkeypatch.setattr(
        orchestrator,
        "observe_application_state",
        lambda *_args, **_kwargs: operations.append("application"),
    )
    monkeypatch.setattr(
        orchestrator,
        "observe_manager_capability",
        lambda *_args: operations.append("manager"),
    )
    orchestrator.install_custom_nodes(phase, app, runtime=runtime)
    assert operations == [
        "application",
        "copy",
        "manager",
        "application",
        "install",
        "manager",
        "application",
        "manager",
        "application",
    ]


def test_registry_cannot_claim_a_future_local_target(tmp_path, monkeypatch):
    app, runtime = application(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    node = local_node(runtime, source)
    phase = custom_nodes_phase(runtime, (registry_node("first", "1.0.0"), node))
    patch_phases(monkeypatch, app, phase)

    def install(_node, *_args):
        write_project(
            runtime.comfyui_path / "custom_nodes", "local-node", "first", "1.0.0"
        )

    monkeypatch.setattr(registry, "_install_registry_node", install)
    monkeypatch.setattr(
        local,
        "prepare_local_node",
        lambda *_args: pytest.fail("occupied future target must fail first"),
    )
    with pytest.raises(
        CustomNodeInstallError, match=r"future Local target.*already exists"
    ):
        orchestrator.install_custom_nodes(phase, app, runtime=runtime)
