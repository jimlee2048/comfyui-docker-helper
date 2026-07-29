"""Deterministic Dockerfile rendering from BuildPlan only."""

import json
import shlex
from pathlib import PurePosixPath

from comfyui_docker_helper.build_ssh import KNOWN_HOSTS_MOUNTS
from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    GitNodePlan,
    build_plan_digest,
)


def render_build_plan_dockerfile(plan: BuildPlan) -> str:
    """Render literal locked image identities and BuildPlan inputs."""
    launch_python = PurePosixPath(plan.runtime.launch_command[0])
    launch_script = PurePosixPath(plan.runtime.launch_command[1])
    runtime_venv = launch_python.parent.parent
    runtime_comfyui = launch_script.parent
    runtime_path = f"/opt/uv/bin:{launch_python.parent.as_posix()}:${{PATH}}"
    lines = [
        "# syntax=docker/dockerfile:1.7",
        f"FROM --platform={plan.toolchain.platform} "
        f"{plan.toolchain.uv_image.reference} AS uv",
        f"FROM --platform={plan.toolchain.platform} "
        f"{plan.toolchain.cuda_image.reference}",
        "COPY --from=uv /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/",
        "COPY build-plan.json /opt/cdh/build/build-plan.json",
        "COPY runtime/config.toml /opt/cdh/runtime/config.toml",
    ]
    if any(
        node.pre_install_hooks or node.post_install_hooks
        for node in plan.custom_nodes.nodes
    ):
        lines.append("COPY build/hooks /opt/cdh/build/hooks")
    if plan.runtime.hooks:
        lines.append("COPY runtime/hooks /opt/cdh/runtime/hooks")
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
    lines.extend(_toolchain_install_lines(plan))
    lines.extend(
        (
            "STOPSIGNAL SIGTERM",
            'ENTRYPOINT ["/usr/bin/tini", "--", "/opt/uv/bin/cdh", '
            '"container", "entrypoint"]',
        )
    )
    return "\n".join(lines) + "\n"


def _toolchain_install_lines(plan: BuildPlan) -> list[str]:
    python = plan.toolchain.python
    cdh = plan.toolchain.tool_store.cdh
    interpreter = (
        f"/opt/python/{python.catalog_key}/bin/"
        f"python{'.'.join(python.version.split('.')[:2])}"
    )
    package_separator = " \\" + "\n    "
    packages = package_separator.join(
        shlex.quote(item) for item in plan.application.os_packages
    )
    bootstrap_check = (
        "import importlib.metadata as m; "
        f"assert m.version('pip') == {python.pip_version!r}"
    )
    cdh_check = "; ".join(
        (
            "import importlib.metadata as m, pathlib, sys",
            "distribution=m.distribution('comfyui-docker-helper')",
            f"assert distribution.version == {cdh.version!r}",
            "commands={item.name for item in distribution.entry_points "
            "if item.group == 'console_scripts'}",
            "assert 'cdh' in commands",
            f"assert pathlib.Path(sys.prefix) == pathlib.Path({cdh.environment!r})",
            "assert pathlib.Path(sys._base_executable).resolve() == "
            f"pathlib.Path({interpreter!r}).resolve()",
        )
    )
    wheel_filename = f"comfyui_docker_helper-{cdh.version}-py3-none-any.whl"
    wheel_mount = f"/tmp/{wheel_filename}"
    wheel_hex_digest = cdh.wheel_digest.removeprefix("sha256:")
    uv_cache_mount = "--mount=type=cache,target=/root/.cache/uv,sharing=locked"
    uv_cache_export = "export UV_CACHE_DIR=/root/.cache/uv &&"
    lines = [
        "RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \\",
        "    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \\",
        " rm -f /etc/apt/apt.conf.d/docker-clean \\",
        " && printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d \\",
        " && chmod +x /usr/sbin/policy-rc.d \\",
        " && apt-get update \\",
        " && DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "--no-install-recommends -- \\",
        f"    {packages} \\",
        " && test -x /usr/bin/tini \\",
        " && rm -f /etc/ssh/ssh_host_* \\",
        " && rm -f /usr/sbin/policy-rc.d",
        f"RUN {uv_cache_mount} {uv_cache_export} "
        f"test \"$(uv --version | cut -d ' ' -f 1-2)\" = "
        f"{_shell_word(f'uv {plan.toolchain.uv_image.resolved_version}')} \\",
        f" && test \"$(uvx --version | cut -d ' ' -f 1-2)\" = "
        f"{_shell_word(f'uvx {plan.toolchain.uv_image.resolved_version}')} \\",
        f" && uv --no-config python install --managed-python --install-dir "
        f"/opt/python --no-bin {_shell_word(python.version)} \\",
        f" && test -x {_shell_word(interpreter)} \\",
        f' && test "$({_shell_word(interpreter)} -c '
        f"'import platform; print(platform.python_version())')\" = "
        f"{_shell_word(python.version)} \\",
        f" && uv --no-config venv --python {_shell_word(interpreter)} "
        f"--no-python-downloads {_shell_word(plan.application.paths.venv)} \\",
        f" && uv --no-config pip install --python "
        f"{_shell_word(plan.application.paths.venv + '/bin/python')} "
        f"--default-index {_shell_word(plan.application.python_index_url)} -- "
        f"{_shell_word(f'pip=={python.pip_version}')} \\",
        f" && {_shell_word(plan.application.paths.venv + '/bin/python')} "
        "-m pip --version \\",
        f" && {_shell_word(plan.application.paths.venv + '/bin/python')} "
        f"-I -c {_shell_word(bootstrap_check)} \\",
        f" && test -x {_shell_word(plan.application.paths.venv + '/bin/pip')} \\",
        f" && test -x {_shell_word(plan.application.paths.venv + '/bin/pip3')}",
        f"RUN {uv_cache_mount} \\",
        f"    --mount=type=bind,source=bootstrap/{wheel_filename},"
        f"target={wheel_mount},readonly \\",
        f" {uv_cache_export} "
        f"test \"$(sha256sum {_shell_word(wheel_mount)} | cut -d ' ' -f 1)\" = "
        f"{_shell_word(wheel_hex_digest)} \\",
        f" && uv --no-config tool install --python {_shell_word(interpreter)} "
        f"--no-python-downloads --default-index "
        f"{_shell_word(plan.application.python_index_url)} "
        f"{_shell_word(wheel_mount)} \\",
        f" && test -x {_shell_word(cdh.executable)} \\",
        f" && {_shell_word(cdh.environment + '/bin/python')} "
        f"-c {_shell_word(cdh_check)} \\",
        " && "
        + " && ".join(
            _command_ownership_checks(
                plan.toolchain.tool_store.bin_dir,
                plan.toolchain.tool_store.tool_dir,
                cdh.name,
                ("cdh",),
            )
        )
        + " \\",
        f" && uv --no-config pip check --python "
        f"{_shell_word(cdh.environment + '/bin/python')} "
        "--no-python-downloads",
    ]
    if plan.toolchain.tool_store.comfy_cli is not None:
        tool = plan.toolchain.tool_store.comfy_cli
        tool_python = f"{plan.toolchain.tool_store.tool_dir}/{tool.name}/bin/python"
        tool_environment = f"{plan.toolchain.tool_store.tool_dir}/{tool.name}"
        direct_check = "; ".join(
            (
                "import importlib.metadata as m, pathlib, sys",
                f"distribution=m.distribution({tool.name!r})",
                f"assert distribution.version == {tool.version!r}",
                "commands={item.name for item in distribution.entry_points "
                "if item.group == 'console_scripts'}",
                "assert {'comfy', 'comfy-cli', 'comfycli'} <= commands",
                "assert pathlib.Path(sys.prefix) == "
                f"pathlib.Path({tool_environment!r})",
                "assert pathlib.Path(sys._base_executable).resolve() == "
                f"pathlib.Path({interpreter!r}).resolve()",
            )
        )
        commands = tool.executables
        preflight = " && ".join(
            _command_absence_checks(plan.toolchain.tool_store.bin_dir, commands)
        )
        links = " \\\n".join(
            f" && {check}"
            for check in _command_ownership_checks(
                plan.toolchain.tool_store.bin_dir,
                plan.toolchain.tool_store.tool_dir,
                tool.name,
                commands,
            )
        )
        lines.append(
            f"RUN {uv_cache_mount} {uv_cache_export} {preflight} \\\n"
            f" && uv --no-config tool install --python {_shell_word(interpreter)} "
            f"--no-python-downloads --default-index "
            f"{_shell_word(plan.application.python_index_url)} "
            f"{_shell_word(tool.requirement)} \\\n"
            f" && test -x {_shell_word(tool_python)} \\\n"
            f" && {_shell_word(tool_python)} -c {_shell_word(direct_check)} \\\n"
            f" && uv --no-config pip check --python {_shell_word(tool_python)} "
            f"--no-python-downloads \\\n"
            f"{links}"
        )
    for tool in plan.toolchain.tool_store.uv_tools:
        tool_python = f"{plan.toolchain.tool_store.tool_dir}/{tool.name}/bin/python"
        direct_check = (
            "import importlib.metadata as m; "
            f"assert m.version({tool.name!r}) == {tool.version!r}"
        )
        lines.append(
            f"RUN {uv_cache_mount} {uv_cache_export} "
            "uv --no-config tool install --python "
            f"{_shell_word(interpreter)} "
            f"--no-python-downloads --default-index "
            f"{_shell_word(plan.application.python_index_url)} "
            f"{_shell_word(tool.requirement)} \\"
            f"\n && test -x {_shell_word(tool_python)} \\"
            f"\n && {_shell_word(tool_python)} -c {_shell_word(direct_check)} \\"
            f"\n && uv --no-config pip check --python {_shell_word(tool_python)} "
            "--no-python-downloads"
        )
    if plan.toolchain.tool_store.comfy_cli is None:
        commands = ("comfy", "comfy-cli", "comfycli")
        lines.append(
            "RUN "
            + " && ".join(
                _command_absence_checks(plan.toolchain.tool_store.bin_dir, commands)
            )
        )
    else:
        tool = plan.toolchain.tool_store.comfy_cli
        commands = tool.executables
        lines.append(
            "RUN "
            + " && ".join(
                _command_ownership_checks(
                    plan.toolchain.tool_store.bin_dir,
                    plan.toolchain.tool_store.tool_dir,
                    tool.name,
                    commands,
                )
            )
        )
    plan_digest = _shell_word(build_plan_digest(plan))
    lines.append(
        f"RUN {uv_cache_mount} {uv_cache_export} {_shell_word(cdh.executable)} "
        "container install-comfyui "
        f"--build-plan-digest {plan_digest} "
        "--constraints /opt/cdh/build/python-package-constraints.txt"
    )
    lines.append(
        _custom_node_install_line(
            plan,
            uv_cache_mount=uv_cache_mount,
            uv_cache_export=uv_cache_export,
            cdh_executable=cdh.executable,
            plan_digest=plan_digest,
        )
    )
    if plan.files.files:
        lines.append(
            f"RUN {_shell_word(cdh.executable)} "
            "container download-files "
            f"--build-plan-digest {plan_digest}"
        )
    lines.append(
        f"RUN {uv_cache_mount} {uv_cache_export} {_shell_word(cdh.executable)} "
        "container emit-final-manifest "
        f"--build-plan-digest {plan_digest}"
    )
    return lines


def _custom_node_install_line(
    plan: BuildPlan,
    *,
    uv_cache_mount: str,
    uv_cache_export: str,
    cdh_executable: str,
    plan_digest: str,
) -> str:
    mounts = [uv_cache_mount]
    environment = ""
    if any(isinstance(node, GitNodePlan) for node in plan.custom_nodes.nodes):
        mounts.append("--mount=type=ssh,id=default,required=false")
        mounts.extend(
            "--mount=type=secret,"
            f"id={descriptor.secret_id},target={descriptor.target},required=false"
            for descriptor in KNOWN_HOSTS_MOUNTS
        )
        environment = f"GIT_SSH_COMMAND={_shell_word(_git_ssh_command())} "
    mount_prefix = " \\\n    ".join(mounts)
    return (
        f"RUN {mount_prefix} {uv_cache_export} {environment}"
        f"{_shell_word(cdh_executable)} container install-custom-nodes "
        f"--build-plan-digest {plan_digest} "
        "--constraints /opt/cdh/build/python-package-constraints.txt "
        "--build-hooks-directory /opt/cdh/build/hooks"
    )


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


def _command_ownership_checks(
    bin_dir: str,
    tool_dir: str,
    tool_name: str,
    commands: tuple[str, ...],
) -> tuple[str, ...]:
    checks = []
    for command in commands:
        public = _shell_word(f"{bin_dir}/{command}")
        owned = _shell_word(f"{tool_dir}/{tool_name}/bin/{command}")
        checks.append(f'test -x {public} && test "$(readlink -f {public})" = {owned}')
    return tuple(checks)


def _shell_word(value: str) -> str:
    return shlex.quote(value)
