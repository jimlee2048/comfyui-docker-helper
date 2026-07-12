"""Deterministic Dockerfile rendering from BuildPlan only."""

import json

from comfyui_docker_helper.config.build_plan import BuildPlan


def render_build_plan_dockerfile(plan: BuildPlan) -> str:
    """Render literal locked image identities and materialized phase inputs."""
    lines = [
        "# syntax=docker/dockerfile:1.7",
        f"FROM --platform={plan.toolchain.platform} "
        f"{plan.toolchain.uv_image.reference} AS uv",
        f"FROM --platform={plan.toolchain.platform} "
        f"{plan.toolchain.cuda_image.reference}",
        "COPY --from=uv /uv /uvx /",
        "COPY build-plan.json /opt/cdh/build/build-plan.json",
        "COPY manifest-binding.json /opt/cdh/build/manifest-binding.json",
        "COPY phases /opt/cdh/build/phases",
        "COPY runtime/config.toml /opt/cdh/runtime/config.toml",
    ]
    if any(node.pre_install or node.post_install for node in plan.custom_nodes.nodes):
        lines.append("COPY inputs /opt/cdh/build/inputs")
    if plan.runtime.hooks:
        lines.append("COPY runtime/hooks /opt/cdh/runtime/hooks")
    lines.extend(
        (
            f"ENV VIRTUAL_ENV={_docker_word(plan.application.paths.venv)}",
            f"ENV WORKSPACE={_docker_word(plan.application.paths.workspace)}",
            f"ENV COMFYUI_PATH={_docker_word(plan.application.paths.comfyui)}",
            f"WORKDIR {_docker_word(plan.application.paths.workspace)}",
        )
    )
    lines.extend(
        f"ENV {item.name}={_docker_word(item.value)}"
        for item in plan.runtime.environment
    )
    return "\n".join(lines) + "\n"


def _docker_word(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)
