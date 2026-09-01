"""Deterministic Dockerfile rendering from BuildPlan only."""

import json
import shlex
from pathlib import PurePosixPath

from comfyui_docker_helper.build_ssh import KNOWN_HOSTS_MOUNTS
from comfyui_docker_helper.config.credentials.git import git_credential_secret_target
from comfyui_docker_helper.config.credentials.secrets import (
    downloader_credential_secret_target,
)
from comfyui_docker_helper.config.planning.build_plan import (
    BuildPlan,
    GitNodePlan,
    HttpFilePlan,
    LocalFilePlan,
    LocalTreePlan,
    build_plan_digest,
    downloader_credential_secret_ids,
    git_credential_secret_ids,
)
from comfyui_docker_helper.release_artifacts import WORKSPACE_PROFILE_CONTEXT_PATH

_BUILD_PLAN_MOUNT = (
    "--mount=type=bind,source=build-plan.json,"
    "target=/opt/cdh/build/build-plan.json,readonly"
)
_UV_CACHE_MOUNT = "--mount=type=cache,target=/root/.cache/uv"
_UV_CACHE_DIRECTORY = "/root/.cache/uv"
_UV_LINK_MODE = "copy"


def render_build_plan_dockerfile(plan: BuildPlan) -> str:
    """Render literal locked image identities and BuildPlan inputs."""
    sections = [
        ["# syntax=docker/dockerfile:1.7"],
        _base_phase(plan),
        _os_packages_phase(plan),
        _managed_python_phase(plan),
        _canonical_cdh_phase(plan),
        _isolated_tools_phase(plan),
        _comfyui_phase(plan),
        _custom_nodes_phase(plan),
        _copied_files_phase(plan),
        _final_verification_phase(plan),
        _runtime_phase(),
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def _base_phase(plan: BuildPlan) -> list[str]:
    launch_python = PurePosixPath(plan.runtime.launch_command[0])
    launch_script = PurePosixPath(plan.runtime.launch_command[1])
    runtime_venv = launch_python.parent.parent
    runtime_comfyui = launch_script.parent
    runtime_path = f"/opt/uv/bin:{launch_python.parent.as_posix()}:${{PATH}}"
    lines = [
        "# Base images and runtime inputs",
        f"FROM --platform={plan.toolchain.platform} "
        f"{plan.toolchain.uv_image.reference} AS uv",
        f"FROM --platform={plan.toolchain.platform} "
        f"{plan.toolchain.cuda_image.reference}",
        "COPY --from=uv /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/",
        "RUN mkdir -p /opt/cdh/build",
        "COPY --chmod=0644 runtime/config.toml /opt/cdh/runtime/config.toml",
        "COPY --chmod=0644 "
        f"{WORKSPACE_PROFILE_CONTEXT_PATH.as_posix()} "
        "/etc/profile.d/cdh-workspace.sh",
    ]
    if any(
        node.pre_install_hooks or node.post_install_hooks
        for node in plan.custom_nodes.nodes
    ):
        lines.append("COPY --chmod=0755 build/hooks /opt/cdh/build/hooks")
    if plan.runtime.hooks:
        lines.append("COPY --chmod=0755 runtime/hooks /opt/cdh/runtime/hooks")
    lines.extend(
        (
            f"ENV VIRTUAL_ENV={_docker_word(runtime_venv.as_posix())}",
            f"ENV UV_TOOL_DIR={_docker_word(plan.toolchain.tool_store.tool_dir)}",
            f"ENV UV_TOOL_BIN_DIR={_docker_word(plan.toolchain.tool_store.bin_dir)}",
            f"ENV PATH={_docker_word(runtime_path)}",
            f"ENV WORKSPACE={_docker_word(plan.application.paths.workspace)}",
            f"ENV COMFYUI_PATH={_docker_word(runtime_comfyui.as_posix())}",
            f"WORKDIR {_docker_word(plan.application.paths.workspace)}",
        )
    )
    lines.extend(
        f"ENV {item.name}={_docker_word(item.value)}"
        for item in plan.runtime.environment
    )
    return lines


def _os_packages_phase(plan: BuildPlan) -> list[str]:
    packages = tuple(shlex.quote(item) for item in plan.application.os_packages)
    install_command = _format_command(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends --",
        packages,
    )
    return _phase(
        "OS packages",
        [
            _run(
                (
                    "--mount=type=cache,target=/var/cache/apt,sharing=locked",
                    "--mount=type=cache,target=/var/lib/apt/lists,sharing=locked",
                ),
                (
                    "rm -f /etc/apt/apt.conf.d/docker-clean",
                    "printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d",
                    "chmod +x /usr/sbin/policy-rc.d",
                    "apt-get update",
                    install_command,
                    "rm -f /etc/ssh/ssh_host_*",
                    "rm -f /usr/sbin/policy-rc.d",
                ),
            )
        ],
    )


def _managed_python_phase(plan: BuildPlan) -> list[str]:
    python = plan.toolchain.python
    interpreter = _managed_python_path(plan)
    return _phase(
        "Managed Python and application environment",
        [
            _run(
                (_UV_CACHE_MOUNT,),
                (
                    f"export UV_CACHE_DIR={_UV_CACHE_DIRECTORY} "
                    f"UV_LINK_MODE={_UV_LINK_MODE}",
                    _format_command(
                        "uv --no-config python install",
                        (
                            "--managed-python",
                            "--install-dir /opt/python",
                            f"--no-bin {_shell_word(python.version)}",
                        ),
                        environment=("UV_PYTHON_CACHE_DIR=/root/.cache/uv/python",),
                    ),
                    _format_command(
                        "uv --no-config venv",
                        (
                            f"--python {_shell_word(interpreter)}",
                            "--no-python-downloads",
                            _shell_word(plan.application.paths.venv),
                        ),
                    ),
                    _format_command(
                        "uv --no-config pip install",
                        (
                            "--python "
                            + _shell_word(plan.application.paths.venv + "/bin/python"),
                            "--default-index "
                            + _shell_word(plan.application.python_index_url),
                            f"-- {_shell_word(f'pip=={python.pip_version}')}",
                        ),
                    ),
                ),
            )
        ],
    )


def _canonical_cdh_phase(plan: BuildPlan) -> list[str]:
    cdh = plan.toolchain.tool_store.cdh
    interpreter = _managed_python_path(plan)
    wheel_filename = f"comfyui_docker_helper-{cdh.version}-py3-none-any.whl"
    wheel_mount = f"/tmp/{wheel_filename}"
    wheel_hex_digest = cdh.wheel_digest.removeprefix("sha256:")
    digest_command = _format_command(
        "test",
        (
            f"\"$(sha256sum {_shell_word(wheel_mount)} | cut -d ' ' -f 1)\" "
            f"= {_shell_word(wheel_hex_digest)}",
        ),
    )
    return _phase(
        "Canonical cdh",
        [
            _run(
                (
                    _UV_CACHE_MOUNT,
                    f"--mount=type=bind,source=bootstrap/{wheel_filename},"
                    f"target={wheel_mount},readonly",
                ),
                (
                    digest_command,
                    _uv_tool_install_command(plan, interpreter, wheel_mount),
                ),
            )
        ],
    )


def _isolated_tools_phase(plan: BuildPlan) -> list[str]:
    interpreter = _managed_python_path(plan)
    lines: list[str] = []
    tool_store = plan.toolchain.tool_store
    if tool_store.comfy_cli is not None:
        lines.append(
            _run(
                (_UV_CACHE_MOUNT,),
                (
                    *_command_absence_checks(
                        tool_store.bin_dir, tool_store.comfy_cli.executables
                    ),
                    _uv_tool_install_command(
                        plan, interpreter, tool_store.comfy_cli.requirement
                    ),
                ),
            )
        )
    for tool in tool_store.uv_tools:
        lines.append(
            _run(
                (_UV_CACHE_MOUNT,),
                (_uv_tool_install_command(plan, interpreter, tool.requirement),),
            )
        )
    return _phase("Isolated tools", lines)


def _comfyui_phase(plan: BuildPlan) -> list[str]:
    cdh = plan.toolchain.tool_store.cdh
    return _phase(
        "ComfyUI",
        [
            _run(
                (_UV_CACHE_MOUNT, _BUILD_PLAN_MOUNT),
                (
                    _format_command(
                        f"{_shell_word(cdh.executable)} container install-comfyui",
                        (
                            "--build-plan-digest "
                            + _shell_word(build_plan_digest(plan)),
                            "--constraints "
                            "/opt/cdh/build/python-package-constraints.txt",
                        ),
                    ),
                ),
            )
        ],
    )


def _custom_nodes_phase(plan: BuildPlan) -> list[str]:
    cdh = plan.toolchain.tool_store.cdh
    mounts = [_UV_CACHE_MOUNT]
    command_environment: tuple[str, ...] = ()
    if any(isinstance(node, GitNodePlan) for node in plan.custom_nodes.nodes):
        mounts.append("--mount=type=ssh,id=default,required=false")
        mounts.extend(
            "--mount=type=secret,"
            f"id={descriptor.secret_id},target={descriptor.target},required=false"
            for descriptor in KNOWN_HOSTS_MOUNTS
        )
        mounts.extend(
            "--mount=type=secret,"
            f"id={secret_id},target={git_credential_secret_target(secret_id)},"
            "required=true"
            for secret_id in git_credential_secret_ids(plan.custom_nodes)
        )
        command_environment = (f"GIT_SSH_COMMAND={_shell_word(_git_ssh_command())}",)
    mounts.append(_BUILD_PLAN_MOUNT)
    return _phase(
        "Custom nodes",
        [
            _run(
                tuple(mounts),
                (
                    f"export UV_CACHE_DIR={_UV_CACHE_DIRECTORY} "
                    f"UV_LINK_MODE={_UV_LINK_MODE}",
                    _format_command(
                        f"{_shell_word(cdh.executable)} container install-custom-nodes",
                        (
                            "--build-plan-digest "
                            + _shell_word(build_plan_digest(plan)),
                            "--constraints "
                            "/opt/cdh/build/python-package-constraints.txt",
                            "--build-hooks-directory /opt/cdh/build/hooks",
                        ),
                        environment=command_environment,
                    ),
                ),
            )
        ],
    )


def _copied_files_phase(plan: BuildPlan) -> list[str]:
    cdh = plan.toolchain.tool_store.cdh
    lines: list[str] = []
    local_trees = tuple(
        item for item in plan.files.files if isinstance(item, LocalTreePlan)
    )
    if any(isinstance(item, HttpFilePlan) for item in plan.files.files):
        mounts = [_BUILD_PLAN_MOUNT]
        mounts.extend(
            "--mount=type=secret,"
            f"id={secret_id},target={downloader_credential_secret_target(secret_id)},"
            "required=true"
            for secret_id in downloader_credential_secret_ids(plan.files)
        )
        lines.append(
            _run(
                tuple(mounts),
                (
                    _format_command(
                        f"{_shell_word(cdh.executable)} container download-files",
                        (
                            "--build-plan-digest "
                            + _shell_word(build_plan_digest(plan)),
                        ),
                    ),
                ),
            )
        )
    if local_trees:
        lines.append(
            _run(
                (_BUILD_PLAN_MOUNT,),
                (
                    _format_command(
                        f"{_shell_word(cdh.executable)} container validate-local-trees",
                        (
                            "--build-plan-digest "
                            + _shell_word(build_plan_digest(plan)),
                        ),
                    ),
                ),
            )
        )
    lines.extend(
        "COPY --link --chmod=0644 "
        + json.dumps(
            [item.context_path, _copy_path(item.target)],
            ensure_ascii=True,
        )
        for item in plan.files.files
        if isinstance(item, LocalFilePlan)
    )
    lines.extend(
        "COPY --link "
        + json.dumps(
            [f"{item.context_path}/", f"{_copy_path(item.target)}/"],
            ensure_ascii=True,
        )
        for item in local_trees
    )
    if local_trees:
        lines.append(
            _run(
                (_BUILD_PLAN_MOUNT,),
                (
                    _format_command(
                        f"{_shell_word(cdh.executable)} "
                        "container normalize-local-trees",
                        (
                            "--build-plan-digest "
                            + _shell_word(build_plan_digest(plan)),
                        ),
                    ),
                ),
            )
        )
    return _phase("Copied files", lines)


def _final_verification_phase(plan: BuildPlan) -> list[str]:
    cdh = plan.toolchain.tool_store.cdh
    return _phase(
        "Final verification",
        [
            _run(
                (_UV_CACHE_MOUNT, _BUILD_PLAN_MOUNT),
                (
                    _format_command(
                        f"{_shell_word(cdh.executable)} container emit-final-manifest",
                        (
                            "--build-plan-digest "
                            + _shell_word(build_plan_digest(plan)),
                        ),
                    ),
                ),
            )
        ],
    )


def _runtime_phase() -> list[str]:
    return _phase(
        "Runtime entrypoint",
        [
            "STOPSIGNAL SIGTERM",
            'ENTRYPOINT ["/usr/bin/tini", "--", "/opt/uv/bin/cdh", '
            '"container", "runtime", "serve"]',
        ],
    )


def _managed_python_path(plan: BuildPlan) -> str:
    python = plan.toolchain.python
    return (
        f"/opt/python/{python.catalog_key}/bin/"
        f"python{'.'.join(python.version.split('.')[:2])}"
    )


def _uv_tool_install_command(
    plan: BuildPlan,
    interpreter: str,
    requirement: str,
) -> str:
    return _format_command(
        "uv --no-config tool install",
        (
            f"--python {_shell_word(interpreter)}",
            "--no-python-downloads",
            "--default-index " + _shell_word(plan.application.python_index_url),
            _shell_word(requirement),
        ),
        environment=(
            f"UV_CACHE_DIR={_UV_CACHE_DIRECTORY}",
            f"UV_LINK_MODE={_UV_LINK_MODE}",
        ),
    )


def _phase(title: str, lines: list[str]) -> list[str]:
    return [f"# {title}", *lines]


def _format_command(
    command: str,
    arguments: tuple[str, ...],
    *,
    environment: tuple[str, ...] = (),
) -> str:
    return " \\\n        ".join((*environment, command, *arguments))


def _run(mounts: tuple[str, ...], commands: tuple[str, ...]) -> str:
    if not commands:
        raise ValueError("a rendered RUN must contain a command")
    if mounts:
        lines = [f"RUN {mounts[0]}"]
        for mount in mounts[1:]:
            lines[-1] += " " + "\\"
            lines.append(f"    {mount}")
        lines[-1] += " " + "\\"
        lines.append(f"    {commands[0]}")
    else:
        lines = [f"RUN {commands[0]}"]
    for command in commands[1:]:
        lines[-1] += " " + "\\"
        lines.append(f"    && {command}")
    return "\n".join(lines)


def _git_ssh_command() -> str:
    user_paths = " ".join(
        descriptor.target
        for descriptor in KNOWN_HOSTS_MOUNTS
        if descriptor.scope == "user"
    )
    system_paths = " ".join(
        descriptor.target
        for descriptor in KNOWN_HOSTS_MOUNTS
        if descriptor.scope == "system"
    )
    return (
        "/usr/bin/ssh -F none "
        "-o BatchMode=yes "
        "-o StrictHostKeyChecking=yes "
        "-o KnownHostsCommand=none "
        f'-o UserKnownHostsFile="{user_paths}" '
        f'-o GlobalKnownHostsFile="{system_paths}"'
    )


def _docker_word(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _command_absence_checks(bin_dir: str, commands: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        f"test ! -e {_shell_word(f'{bin_dir}/{command}')} "
        f"&& test ! -L {_shell_word(f'{bin_dir}/{command}')}"
        for command in commands
    )


def _shell_word(value: str) -> str:
    return shlex.quote(value)


def _copy_path(value: str) -> str:
    """Escape Dockerfile COPY variable markers while retaining literal paths."""
    return value.replace("$", r"\$")
