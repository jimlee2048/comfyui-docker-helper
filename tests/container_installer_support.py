"""Shared builders for container custom-node installer contracts."""

from pathlib import Path

import pytest

from comfyui_docker_helper.comfyui_requirements import ParsedComfyUIRequirements
from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    CustomNodePlan,
    CustomNodesPhase,
    GitCredentialRoutePlan,
)
from comfyui_docker_helper.container import custom_node_installer
from comfyui_docker_helper.container.runners import ContainerRuntime
from tests.build_plan_support import accepted_resolution, build_plan, final_config


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
