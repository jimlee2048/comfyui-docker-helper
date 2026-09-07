"""Custom-node orchestration contracts."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_docker_helper.config.evidence.custom_nodes import custom_node_inventory
from comfyui_docker_helper.config.planning.build_plan import (
    ApplicationPhase,
    CustomNodePlan,
    GitNodePlan,
    HookPlan,
    RegistryNodePlan,
)
from comfyui_docker_helper.container.build import comfyui as comfyui_installer
from comfyui_docker_helper.container.build.comfyui import ComfyUIInstallError
from comfyui_docker_helper.container.build.custom_nodes import (
    git as git_installer,
)
from comfyui_docker_helper.container.build.custom_nodes import (
    orchestrator as custom_node_installer,
)
from comfyui_docker_helper.container.build.custom_nodes import (
    registry as registry_installer,
)
from comfyui_docker_helper.container.build.custom_nodes import (
    root_install,
)
from comfyui_docker_helper.container.build.custom_nodes.contracts import (
    CustomNodeInstallError,
)
from comfyui_docker_helper.container.build.events import (
    ContainerHelperEvent,
    ContainerHelperPhase,
    ContainerHelperPhaseCompleted,
    ContainerHelperPhaseStarted,
    CustomNodeCompleted,
    CustomNodesInstallCompleted,
    GitCustomNodeStarted,
    RegistryCustomNodeStarted,
)
from comfyui_docker_helper.container.process.runners import (
    ContainerCommandError,
    ContainerRuntime,
)
from tests.container_installer_support import (
    _git_node,
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


def _patch_node_runner(monkeypatch: pytest.MonkeyPatch, runner) -> None:
    monkeypatch.setattr(registry_installer, "run_argv", runner)
    monkeypatch.setattr(root_install, "run_argv", runner)


def _patch_git_install(monkeypatch: pytest.MonkeyPatch, installer) -> None:
    def prepare(node, *_args) -> Path:
        target = Path(node.target)
        target.mkdir()
        return target

    monkeypatch.setattr(git_installer, "_prepare_git_node", prepare)
    monkeypatch.setattr(root_install, "install_root_surfaces", installer)


def _hook_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _semantic_operation_signature(
    operation: tuple[object, ...],
) -> tuple[object, ...]:
    kind, value, *_details = operation
    if kind != "event":
        return (kind, value)
    if isinstance(value, ContainerHelperPhaseStarted):
        return ("phase-started", value.phase)
    if isinstance(value, ContainerHelperPhaseCompleted):
        return ("phase-completed", value.phase)
    if isinstance(value, RegistryCustomNodeStarted):
        return (
            "registry-started",
            value.index,
            value.total,
            value.id,
            value.version,
            value.pre_hook_count,
            value.post_hook_count,
        )
    if isinstance(value, GitCustomNodeStarted):
        return (
            "git-started",
            value.index,
            value.total,
            value.target_name,
            value.pre_clone_hook_count,
            value.pre_hook_count,
            value.post_hook_count,
        )
    if isinstance(value, CustomNodeCompleted):
        return ("node-completed", value.index, value.total)
    if isinstance(value, CustomNodesInstallCompleted):
        return ("install-completed", value.node_count)
    raise AssertionError(f"unexpected semantic event: {value!r}")


def _local_manager_application(
    tmp_path: Path,
) -> tuple[ApplicationPhase, ContainerRuntime, Path]:
    application, runtime = _application(tmp_path)
    virtual_env = tmp_path / "venv"
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    anchor = (
        virtual_env
        / "lib"
        / f"python{python_minor}"
        / "site-packages"
        / "comfyui-docker-helper-comfyui.pth"
    )
    manager = application.comfyui.manager
    assert manager is not None
    application = application.model_copy(
        update={
            "paths": application.paths.model_copy(update={"venv": str(virtual_env)}),
            "comfyui": application.comfyui.model_copy(
                update={
                    "manager": manager.model_copy(update={"import_anchor": str(anchor)})
                }
            ),
        }
    )
    runtime = ContainerRuntime(
        workspace=runtime.workspace,
        comfyui_path=runtime.comfyui_path,
        virtual_env=virtual_env,
    )
    anchor.parent.mkdir(parents=True)
    return application, runtime, anchor


def test_final_observer_proves_git_before_exact_registry_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _application_phase, runtime = _application(tmp_path)
    git = _git_node(runtime)
    registry = _node("registry-node", "1.0.0")
    custom_nodes = _phase(runtime, (git, registry))
    target = Path(git.target)
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        git_installer,
        "_verify_git_provenance",
        lambda node, observed, *_args, **_kwargs: events.append(
            ("git", node, observed)
        ),
    )
    monkeypatch.setattr(
        registry_installer,
        "_verify_registry_set",
        lambda _root, expected, **kwargs: events.append(
            ("registry", tuple(expected), tuple(kwargs["excluded_git_targets"]))
        ),
    )

    evidence = custom_node_installer.observe_custom_node_state(
        custom_nodes,
        runtime=runtime,
    )

    assert events == [
        ("git", git, target),
        ("registry", (registry,), (target,)),
    ]
    assert evidence == custom_node_inventory((git, registry))


def test_empty_plan_checks_application_without_node_processes(
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
    helper_events: list[ContainerHelperEvent] = []
    monkeypatch.setattr(
        custom_node_installer,
        "capture_manager_authority",
        lambda *_args: pytest.fail("empty plan must not capture Manager"),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_capability",
        lambda *_args: pytest.fail("empty plan must not prove enabled Manager"),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_absence",
        lambda *_args: pytest.fail("empty plan must not prove disabled Manager"),
    )
    monkeypatch.setattr(
        git_installer,
        "_run_git",
        lambda *_args, **_kwargs: pytest.fail("empty plan must not invoke Git"),
    )
    monkeypatch.setattr(
        registry_installer,
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
        "observe_application_state",
        lambda *_args, **_kwargs: events.append(("application",)),
    )

    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        event_sink=SimpleNamespace(emit=helper_events.append),
    )

    assert events == [
        ("final-typed-boundary",),
        ("application",),
    ]
    assert helper_events == [
        ContainerHelperPhaseStarted(ContainerHelperPhase.CUSTOM_NODES_PREPARATION),
        ContainerHelperPhaseCompleted(ContainerHelperPhase.CUSTOM_NODES_PREPARATION),
        ContainerHelperPhaseStarted(
            ContainerHelperPhase.CUSTOM_NODES_FINAL_VERIFICATION
        ),
        ContainerHelperPhaseCompleted(
            ContainerHelperPhase.CUSTOM_NODES_FINAL_VERIFICATION
        ),
        CustomNodesInstallCompleted(node_count=0),
    ]


@pytest.mark.parametrize("manager_enabled", [False, True])
def test_nonempty_plan_rejects_manager_phase_mismatch_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_enabled: bool,
) -> None:
    application, runtime = _application(tmp_path)
    if not manager_enabled:
        document = application.model_dump(mode="python")
        document["comfyui"]["manager"] = None
        application = ApplicationPhase.model_validate(document)
    custom_nodes = _phase(
        runtime,
        (_git_node(runtime, pre=("must-not-run.py",)),),
        install_manager=not manager_enabled,
    )
    for name in (
        "capture_application_requirements",
        "capture_manager_authority",
        "observe_manager_absence",
        "run_hook",
    ):
        monkeypatch.setattr(
            custom_node_installer,
            name,
            lambda *_args, **_kwargs: pytest.fail(
                "phase mismatch must fail before mutation or observation"
            ),
        )
    monkeypatch.setattr(
        git_installer,
        "_prepare_git_node",
        lambda *_args, **_kwargs: pytest.fail(
            "phase mismatch must fail before mutation or observation"
        ),
    )

    with pytest.raises(CustomNodeInstallError, match="does not match application"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )


def test_enabled_git_only_plan_observes_manager_without_registry_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, (_git_node(runtime),))
    _patch_phases(monkeypatch, application, custom_nodes)
    events: list[str] = []

    monkeypatch.setattr(
        custom_node_installer,
        "capture_manager_authority",
        lambda *_args: events.append("capture-manager") or object(),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "verify_manager_authority",
        lambda *_args: events.append("verify-manager-authority"),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_capability",
        lambda *_args: events.append("observe-manager"),
    )
    _patch_git_install(monkeypatch, lambda *_args: events.append("install-git"))
    monkeypatch.setattr(
        git_installer,
        "_verify_git_provenance",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        registry_installer,
        "_scan_registry_identities",
        lambda *_args, **_kwargs: pytest.fail(
            "Git-only plans must not enter Registry scanning"
        ),
    )
    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
    )

    assert events.count("capture-manager") == 1
    assert events.count("install-git") == 1
    installation = events.index("install-git")
    assert "observe-manager" in events[:installation]
    assert events[installation + 1 :] == [
        "verify-manager-authority",
        "observe-manager",
        "verify-manager-authority",
        "observe-manager",
    ]


def test_enabled_git_only_plan_rejects_manager_mutation_at_next_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, (_git_node(runtime),))
    _patch_phases(monkeypatch, application, custom_nodes)
    manager_valid = True

    def observe_manager(*_args) -> None:
        if not manager_valid:
            raise ComfyUIInstallError("Manager capability was mutated")

    def install_git(*_args) -> None:
        nonlocal manager_valid
        manager_valid = False

    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_capability",
        observe_manager,
    )
    _patch_git_install(monkeypatch, install_git)
    monkeypatch.setattr(
        custom_node_installer,
        "_verify_mixed_state",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(ComfyUIInstallError, match="was mutated"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )


def test_enabled_git_only_plan_rejects_anchor_drift_at_next_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime, anchor = _local_manager_application(tmp_path)
    manager = application.comfyui.manager
    assert manager is not None
    (anchor.parent / manager.import_name).mkdir()
    custom_nodes = _phase(runtime, (_git_node(runtime),))
    _patch_phases(monkeypatch, application, custom_nodes)
    runtime.comfyui_path.joinpath("manager_requirements.txt").write_text(
        "comfyui_manager==4.0.5\n"
    )
    comfyui_installer._write_import_anchor(
        anchor,
        runtime.comfyui_path,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    verify_anchor = comfyui_installer._verify_manager_import_anchor
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_manager_import_anchor",
        lambda observed_application, observed_manager, observed_runtime: verify_anchor(
            observed_application,
            observed_manager,
            observed_runtime,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        ),
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_declared_manager_distributions",
        lambda *_args: None,
    )
    monkeypatch.setattr(comfyui_installer, "_verify_cm_cli", lambda *_args: None)
    monkeypatch.setattr(
        custom_node_installer,
        "capture_manager_authority",
        comfyui_installer.capture_manager_authority,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "verify_manager_authority",
        comfyui_installer.verify_manager_authority,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_capability",
        comfyui_installer.observe_manager_capability,
    )

    def mutate_anchor(*_args) -> None:
        anchor.chmod(0o644)
        anchor.write_text("wrong\n")
        anchor.chmod(0o444)

    _patch_git_install(monkeypatch, mutate_anchor)
    monkeypatch.setattr(
        git_installer,
        "_verify_git_provenance",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(ComfyUIInstallError, match="content does not match"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )


def test_disabled_git_only_plan_rejects_manager_introduction_at_next_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    document = application.model_dump(mode="python")
    document["comfyui"]["manager"] = None
    application = ApplicationPhase.model_validate(document)
    custom_nodes = _phase(
        runtime,
        (_git_node(runtime),),
        install_manager=False,
    )
    _patch_phases(monkeypatch, application, custom_nodes)
    manager_present = False
    absence_observations = 0

    def observe_absence(*_args) -> None:
        nonlocal absence_observations
        absence_observations += 1
        if manager_present:
            raise ComfyUIInstallError("Manager exists while disabled")

    def install_git(*_args) -> None:
        nonlocal manager_present
        manager_present = True

    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_absence",
        observe_absence,
    )
    _patch_git_install(monkeypatch, install_git)
    monkeypatch.setattr(
        custom_node_installer,
        "_verify_mixed_state",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(ComfyUIInstallError, match="exists while disabled"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )

    assert absence_observations == 3


def test_mixed_executor_preserves_one_original_order_and_hook_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    nodes: tuple[CustomNodePlan, ...] = (
        _node("first", "1.0.0", post=("first-post.py",)),
        _git_node(
            runtime,
            pre_clone=("git-clone-one.py", "git-clone-two.py"),
            pre=("git-pre.py",),
            post=("git-post.py",),
        ),
        _node("last", "2.0.0"),
    )
    custom_nodes = _phase(runtime, nodes)
    _patch_phases(monkeypatch, application, custom_nodes)
    events: list[object] = []
    observed_git_environment: dict[str, str] = {}
    observed_python_environment: dict[str, str] = {}
    source_environment = {
        "GIT_SSH_COMMAND": "ssh -F /tmp/user-config",
        "HOME": "/user/home",
        "PATH": "/ambient/bin",
        "LIBRARY_PATH": "/usr/local/cuda/lib64/stubs",
        "CUDA_HOME": "/usr/local/cuda",
    }

    def names(items: Sequence[CustomNodePlan]) -> tuple[str, ...]:
        return tuple(
            item.id if isinstance(item, RegistryNodePlan) else item.target
            for item in items
        )

    monkeypatch.setattr(
        custom_node_installer,
        "_verify_mixed_state",
        lambda _root, admitted, future, *, prepared_node=None, **_kwargs: events.append(
            ("proof", names(admitted), names(future), prepared_node)
        ),
    )
    monkeypatch.setattr(
        registry_installer,
        "_install_registry_node",
        lambda node, *_args: events.append(("install", node.id)),
    )

    def prepare_git(node, _root, _git_path, git_environment) -> Path:
        observed_git_environment.update(git_environment)
        target = Path(node.target)
        assert not target.exists()
        target.mkdir()
        events.append(("prepare", target.name))
        return target

    def install_git(
        description,
        target,
        _application,
        _runtime,
        _uv_path,
        _constraints_path,
        python_environment,
    ) -> None:
        assert description == f"Git node {target.name}"
        observed_python_environment.update(python_environment)
        events.append(("install", target.name))

    monkeypatch.setattr(git_installer, "_prepare_git_node", prepare_git)
    monkeypatch.setattr(root_install, "install_root_surfaces", install_git)

    def run_hook(hook, **kwargs) -> None:
        assert kwargs["env"] == source_environment
        events.append(("hook", hook, kwargs["expected_digest"]))

    monkeypatch.setattr(
        custom_node_installer,
        "run_hook",
        run_hook,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        lambda *_args, **_kwargs: events.append(("application-check",)),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_capability",
        lambda *_args: events.append(("manager-check",)),
    )
    constraints = tmp_path / "constraints.txt"
    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        constraints_path=constraints,
        environ=source_environment,
        event_sink=SimpleNamespace(emit=lambda event: events.append(("event", event))),
    )

    assert [event for event in events if event[0] == "install"] == [
        ("install", "first"),
        ("install", "direct"),
        ("install", "last"),
    ]
    assert [event for event in events if event[0] == "hook"] == [
        ("hook", "first-post.py", f"sha256:{'b' * 64}"),
        ("hook", "git-clone-one.py", f"sha256:{'e' * 64}"),
        ("hook", "git-clone-two.py", f"sha256:{'e' * 64}"),
        ("hook", "git-pre.py", f"sha256:{'c' * 64}"),
        ("hook", "git-post.py", f"sha256:{'d' * 64}"),
    ]
    assert (
        events.index(("hook", "git-pre.py", f"sha256:{'c' * 64}"))
        < events.index(("install", "direct"))
        < events.index(("hook", "git-post.py", f"sha256:{'d' * 64}"))
    )
    git_pre_index = events.index(("hook", "git-pre.py", f"sha256:{'c' * 64}"))
    git_clone_index = events.index(("hook", "git-clone-one.py", f"sha256:{'e' * 64}"))
    assert [
        event
        for event in events[git_clone_index : git_pre_index + 1]
        if event[0] in {"hook", "proof", "prepare"}
    ] == [
        ("hook", "git-clone-one.py", f"sha256:{'e' * 64}"),
        ("proof", names(nodes[:1]), names(nodes[1:]), None),
        ("hook", "git-clone-two.py", f"sha256:{'e' * 64}"),
        ("proof", names(nodes[:1]), names(nodes[1:]), None),
        ("prepare", "direct"),
        ("proof", names(nodes[:1]), names(nodes[2:]), nodes[1]),
        ("hook", "git-pre.py", f"sha256:{'c' * 64}"),
    ]
    git_install_index = events.index(("install", "direct"))
    git_post_index = events.index(("hook", "git-post.py", f"sha256:{'d' * 64}"))
    assert [
        event
        for event in events[git_pre_index : git_install_index + 1]
        if event[0] in {"hook", "proof", "install"}
    ] == [
        ("hook", "git-pre.py", f"sha256:{'c' * 64}"),
        ("proof", names(nodes[:1]), names(nodes[2:]), nodes[1]),
        ("install", "direct"),
    ]
    assert [
        event
        for event in events[git_install_index : git_post_index + 1]
        if event[0] in {"install", "proof", "hook"}
    ] == [
        ("install", "direct"),
        ("proof", names(nodes[:2]), names(nodes[2:]), None),
        ("hook", "git-post.py", f"sha256:{'d' * 64}"),
    ]
    business_events = [event for event in events if event[0] != "event"]
    assert business_events[-3:] == [
        ("proof", names(nodes), (), None),
        ("manager-check",),
        ("application-check",),
    ]
    semantic_events = [
        event for event in events if event[0] in {"event", "hook", "prepare", "install"}
    ]
    assert [_semantic_operation_signature(event) for event in semantic_events] == [
        ("phase-started", ContainerHelperPhase.CUSTOM_NODES_PREPARATION),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODES_PREPARATION),
        ("registry-started", 1, 3, "first", "1.0.0", 0, 1),
        ("phase-started", ContainerHelperPhase.CUSTOM_NODE_INSTALLATION),
        ("install", "first"),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODE_INSTALLATION),
        ("phase-started", ContainerHelperPhase.CUSTOM_NODE_POST_INSTALL),
        ("hook", "first-post.py"),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODE_POST_INSTALL),
        ("node-completed", 1, 3),
        ("git-started", 2, 3, "direct", 2, 1, 1),
        ("phase-started", ContainerHelperPhase.CUSTOM_NODE_PRE_CLONE),
        ("hook", "git-clone-one.py"),
        ("hook", "git-clone-two.py"),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODE_PRE_CLONE),
        ("phase-started", ContainerHelperPhase.CUSTOM_NODE_SOURCE_PREPARATION),
        ("prepare", "direct"),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODE_SOURCE_PREPARATION),
        ("phase-started", ContainerHelperPhase.CUSTOM_NODE_PRE_INSTALL),
        ("hook", "git-pre.py"),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODE_PRE_INSTALL),
        ("phase-started", ContainerHelperPhase.CUSTOM_NODE_INSTALLATION),
        ("install", "direct"),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODE_INSTALLATION),
        ("phase-started", ContainerHelperPhase.CUSTOM_NODE_POST_INSTALL),
        ("hook", "git-post.py"),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODE_POST_INSTALL),
        ("node-completed", 2, 3),
        ("registry-started", 3, 3, "last", "2.0.0", 0, 0),
        ("phase-started", ContainerHelperPhase.CUSTOM_NODE_INSTALLATION),
        ("install", "last"),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODE_INSTALLATION),
        ("node-completed", 3, 3),
        ("phase-started", ContainerHelperPhase.CUSTOM_NODES_FINAL_VERIFICATION),
        ("phase-completed", ContainerHelperPhase.CUSTOM_NODES_FINAL_VERIFICATION),
        ("install-completed", 3),
    ]
    emitted = [event[1] for event in semantic_events if event[0] == "event"]
    assert "https://example.invalid" not in repr(emitted)
    assert "c" * 40 not in repr(emitted)
    assert "sha256:" not in repr(emitted)
    assert events.index(("application-check",)) < events.index(("install", "first"))
    assert observed_git_environment["GIT_SSH_COMMAND"] == ("ssh -F /tmp/user-config")
    assert observed_git_environment["HOME"] == "/user/home"
    assert observed_git_environment["PATH"] == "/opt/venv/bin:/ambient/bin"
    assert observed_git_environment["LIBRARY_PATH"] == ("/usr/local/cuda/lib64/stubs")
    assert observed_git_environment["CUDA_HOME"] == "/usr/local/cuda"
    assert observed_python_environment["PATH"] == (
        "/opt/venv/bin:/usr/local/bin:/usr/local/cuda/bin:/usr/bin:/bin"
    )
    assert observed_python_environment["LIBRARY_PATH"] == (
        "/usr/local/cuda/lib64/stubs"
    )
    assert observed_python_environment["CUDA_HOME"] == "/usr/local/cuda"
    assert observed_python_environment["PIP_CONSTRAINT"] == str(constraints)
    assert observed_python_environment["UV_CONSTRAINT"] == str(constraints)
    assert observed_python_environment["PIP_INDEX_URL"] == application.python_index_url
    assert observed_python_environment["UV_DEFAULT_INDEX"] == (
        application.python_index_url
    )


def test_temporary_build_constraints_are_cleaned_after_failure(
    tmp_path: Path,
) -> None:
    application, _runtime = _application(tmp_path)
    observed_path: Path | None = None

    with (
        pytest.raises(RuntimeError, match="installer failed"),
        custom_node_installer._temporary_build_constraints(
            application.pytorch,
            directory=tmp_path,
        ) as path,
    ):
        observed_path = path
        assert path.read_bytes() == (
            b"torch==2.12.1+cu130\ntorchaudio==2.11.0+cu130\n"
            b"torchvision==0.27.1+cu130\n"
        )
        raise RuntimeError("installer failed")

    assert observed_path is not None
    assert not observed_path.exists()


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
        registry_installer,
        "_install_registry_node",
        lambda *_args: events.append(("process", "cm-cli")),
    )

    def observe_manager(*_args) -> None:
        nonlocal manager_observations
        manager_observations += 1
        events.append(("manager-observation", manager_observations))

    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_capability",
        observe_manager,
    )

    def observe_application(*_args, **kwargs) -> None:
        application_git_paths.append(kwargs["git_path"])
        events.append(("application-observation", None))

    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        observe_application,
    )
    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        git_path=application_git_path,
    )

    assert events[:3] == [
        ("typed-boundary", None),
        ("application-observation", None),
        ("process", "cm-cli"),
    ]
    assert events[3:6] == [
        ("typed-boundary", None),
        ("manager-observation", 1),
        ("application-observation", None),
    ]
    assert [event for event in events if event[0] == "manager-observation"] == [
        ("manager-observation", 1),
        ("manager-observation", 2),
    ]
    assert [event for event in events if event[0] == "application-observation"] == [
        ("application-observation", None),
        ("application-observation", None),
        ("application-observation", None),
    ]
    assert application_git_paths == [application_git_path] * 3
    assert events[-3:] == [
        ("typed-boundary", None),
        ("manager-observation", 2),
        ("application-observation", None),
    ]


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
    helper_events: list[ContainerHelperEvent] = []

    def false_zero(argv, **_kwargs):
        commands.append(tuple(str(item) for item in argv))
        return SimpleNamespace(returncode=0)

    _patch_node_runner(monkeypatch, false_zero)

    with pytest.raises(
        CustomNodeInstallError,
        match=r"missing@1\.0\.0 is not installed",
    ):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
            event_sink=SimpleNamespace(emit=helper_events.append),
        )

    assert [command[2] for command in commands] == ["missing@1.0.0"]
    assert helper_events[-1] == ContainerHelperPhaseStarted(
        ContainerHelperPhase.CUSTOM_NODE_INSTALLATION
    )
    assert CustomNodeCompleted(index=1, total=2) not in helper_events
    assert CustomNodesInstallCompleted(node_count=2) not in helper_events


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

    _patch_node_runner(monkeypatch, install)

    with pytest.raises(CustomNodeInstallError, match="admitted declaration prefix"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )

    assert commands == ["first@1.0.0"]


@pytest.mark.parametrize("state", ["admitted", "prepared"])
@pytest.mark.parametrize("valid", [True, False], ids=["valid", "identity-drift"])
def test_mixed_proof_excludes_git_only_after_fresh_git_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    valid: bool,
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
        "verify_manager_authority",
        lambda *_args: events.append("manager"),
    )

    def verify_git(*_args, **_kwargs) -> None:
        events.append("git")
        if not valid:
            raise CustomNodeInstallError("Git identity changed")

    monkeypatch.setattr(git_installer, "_verify_git_provenance", verify_git)

    def verify_registry(_root, expected, *, excluded_git_targets=()):
        events.append("registry")
        assert expected == ()
        assert excluded_git_targets == [target]

    monkeypatch.setattr(registry_installer, "_verify_registry_set", verify_registry)

    def prove() -> None:
        custom_node_installer._verify_mixed_state(
            runtime.comfyui_path / "custom_nodes",
            (git,) if state == "admitted" else (),
            (_node("future-registry", "1.0.0"),),
            prepared_node=git if state == "prepared" else None,
            application=application,
            runtime=runtime,
            manager_authority=object(),
            has_registry=True,
            git_path=Path("/usr/bin/git"),
            git_environment={},
        )

    if valid:
        prove()
        assert events == ["manager", "git", "registry"]
    else:
        with pytest.raises(CustomNodeInstallError, match="identity changed"):
            prove()
        assert events == ["manager", "git"]


def test_future_git_target_is_rejected_before_its_pre_clone_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    git = _git_node(runtime)
    Path(git.target).mkdir()
    monkeypatch.setattr(
        custom_node_installer,
        "verify_manager_authority",
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
            has_registry=False,
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
    _patch_node_runner(
        monkeypatch,
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
    _patch_node_runner(
        monkeypatch,
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

    _patch_node_runner(monkeypatch, fail)
    monkeypatch.setattr(
        registry_installer,
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
        "observe_manager_capability",
        verify_capability,
    )
    monkeypatch.setattr(custom_node_installer, "run_hook", mutate)
    _patch_node_runner(monkeypatch, install)

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

    monkeypatch.setattr(custom_node_installer, "observe_manager_capability", verify)
    monkeypatch.setattr(custom_node_installer, "run_hook", hook)
    _patch_node_runner(
        monkeypatch,
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
    _patch_node_runner(
        monkeypatch,
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
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "mutate.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    node = _node("first", "1.0.0", pre=("mutate.sh",)).model_copy(
        update={
            "pre_install_hooks": (
                HookPlan(
                    relative_path="mutate.sh",
                    digest=_hook_digest(hook.read_bytes()),
                ),
            )
        }
    )
    custom_nodes = _phase(runtime, (node,))
    _patch_phases(monkeypatch, application, custom_nodes)
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

    monkeypatch.setattr(custom_node_installer, "observe_manager_capability", prove)
    _patch_node_runner(monkeypatch, install)
    custom_node_installer.install_custom_nodes(
        custom_nodes,
        application,
        runtime=runtime,
        build_hooks_directory=tmp_path,
    )

    assert marker.exists()
    assert events.index("proof-after-hook") < events.index("cm-cli")


def test_first_hook_replacement_of_later_regular_hook_fails_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trusted hook cannot substitute later locked executable bytes."""
    application, runtime = _application(tmp_path)
    first_path = tmp_path / "first.sh"
    later_path = tmp_path / "later.sh"
    replacement_path = tmp_path / "replacement.sh"
    malicious_marker = tmp_path / "malicious-hook-ran"
    install_marker = tmp_path / "node-install-ran"
    malicious = f"touch {malicious_marker}"
    first_content = (
        f"printf '%s\\n' '{malicious}' > {replacement_path}\n"
        f"mv {replacement_path} {later_path}\n"
    ).encode()
    later_content = b"true\n"
    first_path.write_bytes(first_content)
    later_path.write_bytes(later_content)
    node = RegistryNodePlan.model_construct(
        type="registry",
        id="first",
        version="1.0.0",
        pre_install_hooks=(
            HookPlan(relative_path="first.sh", digest=_hook_digest(first_content)),
            HookPlan(relative_path="later.sh", digest=_hook_digest(later_content)),
        ),
        post_install_hooks=(),
    )
    custom_nodes = _phase(runtime, (node,))
    _patch_phases(monkeypatch, application, custom_nodes)
    _patch_node_runner(monkeypatch, lambda *_args, **_kwargs: install_marker.touch())

    with pytest.raises(ContainerCommandError, match="digest does not match"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
            build_hooks_directory=tmp_path,
        )

    assert later_path.read_text() == f"{malicious}\n"
    assert later_path.is_file()
    assert not malicious_marker.exists()
    assert not install_marker.exists()


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
        "capture_manager_authority",
        comfyui_installer.capture_manager_authority,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "verify_manager_authority",
        comfyui_installer.verify_manager_authority,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_capability",
        comfyui_installer.observe_manager_capability,
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_declared_manager_distributions",
        prove_distributions,
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_manager_import_root",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        comfyui_installer,
        "_verify_manager_import_anchor",
        lambda _application, observed_manager, _runtime: (
            Path(observed_manager.import_anchor).parent
        ),
    )
    monkeypatch.setattr(comfyui_installer, "_verify_cm_cli", lambda *_args: None)
    monkeypatch.setattr(custom_node_installer, "run_hook", retarget)
    _patch_node_runner(
        monkeypatch,
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

    _patch_node_runner(monkeypatch, install)
    monkeypatch.setattr(custom_node_installer, "run_hook", mutate)

    with pytest.raises(CustomNodeInstallError, match="version does not match"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
        )

    assert commands == ["first@1.0.0"]


@pytest.mark.parametrize("hook_phase", ["pre_clone", "pre", "post"])
def test_git_hook_mutation_is_proved_before_next_hook_or_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_phase: str,
) -> None:
    application, runtime = _application(tmp_path)
    node = _git_node(runtime, **{hook_phase: ("first.py", "second.py")})
    custom_nodes = _phase(runtime, (node, _git_node(runtime, "later")))
    _patch_phases(monkeypatch, application, custom_nodes)
    operations: list[str] = []
    helper_events: list[ContainerHelperEvent] = []
    manager_valid = True

    def prepare(node, *_args) -> Path:
        operations.append(f"prepare:{Path(node.target).name}")
        target = Path(node.target)
        target.mkdir()
        return target

    def hook(name, **_kwargs) -> None:
        nonlocal manager_valid
        operations.append(name)
        manager_valid = False

    def observe_manager(*_args) -> None:
        if not manager_valid:
            raise ComfyUIInstallError("Manager capability was mutated")

    monkeypatch.setattr(git_installer, "_prepare_git_node", prepare)
    monkeypatch.setattr(
        root_install,
        "install_root_surfaces",
        lambda _description, target, *_args: operations.append(
            f"install:{target.name}"
        ),
    )
    monkeypatch.setattr(git_installer, "_verify_git_provenance", lambda *_args: None)
    monkeypatch.setattr(custom_node_installer, "run_hook", hook)
    monkeypatch.setattr(
        custom_node_installer, "observe_manager_capability", observe_manager
    )

    with pytest.raises(ComfyUIInstallError, match="was mutated"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
            event_sink=SimpleNamespace(emit=helper_events.append),
        )

    expected = {
        "pre_clone": ["first.py"],
        "pre": ["prepare:direct", "first.py"],
        "post": ["prepare:direct", "install:direct", "first.py"],
    }
    phases = {
        "pre_clone": ContainerHelperPhase.CUSTOM_NODE_PRE_CLONE,
        "pre": ContainerHelperPhase.CUSTOM_NODE_PRE_INSTALL,
        "post": ContainerHelperPhase.CUSTOM_NODE_POST_INSTALL,
    }
    assert operations == expected[hook_phase]
    assert helper_events[-1] == ContainerHelperPhaseStarted(phases[hook_phase])


@pytest.mark.parametrize("failed_proof", ["git", "manager", "application"])
def test_source_preparation_proof_failure_prevents_pre_install_and_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_proof: str,
) -> None:
    application, runtime = _application(tmp_path)
    node = _git_node(runtime, pre=("patch.py",), post=("after.py",))
    custom_nodes = _phase(runtime, (node,))
    _patch_phases(monkeypatch, application, custom_nodes)
    operations: list[str] = []
    helper_events: list[ContainerHelperEvent] = []
    prepared = False

    def prepare(node, *_args) -> Path:
        nonlocal prepared
        prepared = True
        operations.append("prepare")
        target = Path(node.target)
        target.mkdir()
        return target

    def prove(kind: str) -> None:
        if prepared and kind == failed_proof:
            raise CustomNodeInstallError("prepared source boundary failed")

    monkeypatch.setattr(git_installer, "_prepare_git_node", prepare)
    monkeypatch.setattr(
        git_installer, "_verify_git_provenance", lambda *_args: prove("git")
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_capability",
        lambda *_args: prove("manager"),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        lambda *_args, **_kwargs: prove("application"),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "run_hook",
        lambda name, **_kwargs: operations.append(name),
    )
    monkeypatch.setattr(
        root_install,
        "install_root_surfaces",
        lambda *_args: operations.append("install"),
    )

    with pytest.raises(CustomNodeInstallError, match="source boundary failed"):
        custom_node_installer.install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
            event_sink=SimpleNamespace(emit=helper_events.append),
        )

    assert operations == ["prepare"]
    assert helper_events[-1] == ContainerHelperPhaseStarted(
        ContainerHelperPhase.CUSTOM_NODE_SOURCE_PREPARATION
    )


def test_empty_git_hook_phases_keep_prepared_proof_and_independent_final_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    node = _git_node(runtime)
    custom_nodes = _phase(runtime, (node,))
    _patch_phases(monkeypatch, application, custom_nodes)
    operations: list[object] = []

    def prove(
        _root,
        admitted,
        future,
        *,
        prepared_node: GitNodePlan | None = None,
        **_kwargs,
    ) -> None:
        operations.append(("proof", tuple(admitted), tuple(future), prepared_node))

    def prepare(node, *_args) -> Path:
        operations.append("prepare")
        target = Path(node.target)
        target.mkdir()
        return target

    monkeypatch.setattr(custom_node_installer, "_verify_mixed_state", prove)
    monkeypatch.setattr(git_installer, "_prepare_git_node", prepare)
    monkeypatch.setattr(
        root_install,
        "install_root_surfaces",
        lambda *_args: operations.append("install"),
    )
    custom_node_installer.install_custom_nodes(
        custom_nodes, application, runtime=runtime
    )

    assert operations == [
        ("proof", (), (node,), None),
        "prepare",
        ("proof", (), (), node),
        "install",
        ("proof", (node,), (), None),
        ("proof", (node,), (), None),
    ]
