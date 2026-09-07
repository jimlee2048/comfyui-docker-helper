"""Shared builders for container custom-node installer contracts."""

from pathlib import Path

import pytest

from comfyui_docker_helper.config.planning.build_plan import (
    ApplicationPhase,
    CustomNodePlan,
    CustomNodesPhase,
    GitCredentialRoutePlan,
    GitNodePlan,
    HookPlan,
    RegistryNodePlan,
)
from comfyui_docker_helper.config.planning.requirements import ParsedComfyUIRequirements
from comfyui_docker_helper.container.build.custom_nodes import (
    orchestrator as custom_node_installer,
)
from comfyui_docker_helper.container.process.runners import ContainerRuntime
from tests.build_plan_support import accepted_resolution, build_plan, final_config


def registry_node(
    node_id: str,
    version: str,
    *,
    pre: tuple[str, ...] = (),
    post: tuple[str, ...] = (),
) -> RegistryNodePlan:
    return RegistryNodePlan.model_construct(
        type="registry",
        id=node_id,
        version=version,
        pre_install_hooks=tuple(
            HookPlan(relative_path=value, digest=f"sha256:{'a' * 64}") for value in pre
        ),
        post_install_hooks=tuple(
            HookPlan(relative_path=value, digest=f"sha256:{'b' * 64}") for value in post
        ),
    )


def git_node(
    runtime: ContainerRuntime,
    name: str = "direct",
    *,
    url: str = "https://example.invalid/Raw/Node.git",
    pre_clone: tuple[str, ...] = (),
    pre: tuple[str, ...] = (),
    post: tuple[str, ...] = (),
) -> GitNodePlan:
    return GitNodePlan.model_construct(
        type="git",
        url=url,
        commit="c" * 40,
        target=str(runtime.comfyui_path / "custom_nodes" / name),
        pre_clone_hooks=tuple(
            HookPlan(relative_path=value, digest=f"sha256:{'e' * 64}")
            for value in pre_clone
        ),
        pre_install_hooks=tuple(
            HookPlan(relative_path=value, digest=f"sha256:{'c' * 64}") for value in pre
        ),
        post_install_hooks=tuple(
            HookPlan(relative_path=value, digest=f"sha256:{'d' * 64}") for value in post
        ),
    )


def write_project(root: Path, directory: str, name: str, version: str) -> Path:
    target = root / directory
    target.mkdir()
    target.joinpath("pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    return target


_node = registry_node
_git_node = git_node
_write_project = write_project


def application(tmp_path: Path) -> tuple[ApplicationPhase, ContainerRuntime]:
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


def custom_nodes_phase(
    runtime: ContainerRuntime,
    nodes: tuple[CustomNodePlan, ...],
    *,
    install_manager: bool = True,
    git_credentials: tuple[GitCredentialRoutePlan, ...] = (),
) -> CustomNodesPhase:
    return CustomNodesPhase(
        install_manager=install_manager,
        user_directory=str(runtime.comfyui_path / "user"),
        nodes=nodes,
        git_credentials=git_credentials,
    )


def patch_phases(
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
        "capture_manager_authority",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        custom_node_installer,
        "verify_manager_authority",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_capability",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_manager_absence",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        custom_node_installer,
        "observe_application_state",
        lambda *_args, **_kwargs: None,
    )
