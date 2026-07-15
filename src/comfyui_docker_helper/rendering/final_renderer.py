"""Deterministic Dockerfile rendering from BuildPlan only."""

import json
import shlex
from pathlib import PurePosixPath

from comfyui_docker_helper.config.build_plan import BuildPlan, build_plan_digest


def render_build_plan_dockerfile(plan: BuildPlan) -> str:
    """Render literal locked image identities and materialized phase inputs."""
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
        "COPY --from=uv /uv /uvx /usr/local/bin/",
        "COPY build-plan.json /opt/cdh/build/build-plan.json",
        "COPY manifest-binding.json /opt/cdh/build/manifest-binding.json",
        "COPY phases /opt/cdh/build/phases",
        "COPY --chown=0:0 checkers /opt/cdh/build/checkers",
        "COPY runtime/config.toml /opt/cdh/runtime/config.toml",
        "COPY cdh /opt/cdh/source",
        "COPY cdh-production-requirements.txt "
        "/opt/cdh/build/cdh-production-requirements.txt",
        "COPY cdh-production-inventory.txt /opt/cdh/build/cdh-production-inventory.txt",
        "COPY --chown=0:0 --chmod=0444 pytorch-resolution.toml "
        "/opt/cdh/build/pyproject.toml",
    ]
    if any(node.pre_install or node.post_install for node in plan.custom_nodes.nodes):
        lines.append("COPY inputs /opt/cdh/build/inputs")
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
    return "\n".join(lines) + "\n"


def _toolchain_install_lines(plan: BuildPlan) -> list[str]:
    python = plan.toolchain.python
    interpreter = (
        f"/opt/python/{python.catalog_key}/bin/"
        f"python{'.'.join(python.version.split('.')[:2])}"
    )
    requirements = "/opt/cdh/build/cdh-production-requirements.txt"
    inventory = "/opt/cdh/build/cdh-production-inventory.txt"
    package_separator = " \\" + "\n    "
    packages = package_separator.join(
        shlex.quote(item) for item in plan.application.os_packages
    )
    bootstrap_check = json.dumps(
        {"distributions": {"pip": python.pip_version}},
        sort_keys=True,
        separators=(",", ":"),
    )
    inventory_check = "; ".join(
        (
            "import importlib.metadata as m, pathlib, re, sys",
            "normalize=lambda value: re.sub(r'[-_.]+', '-', value).lower()",
            "expected=dict(line.split('==', 1) for line in "
            "pathlib.Path(sys.argv[1]).read_text().splitlines())",
            "actual={normalize(item.metadata['Name']): item.version "
            "for item in m.distributions()}",
            "assert actual == expected, (expected, actual)",
        )
    )
    lines = [
        "RUN rm -f /etc/apt/apt.conf.d/docker-clean \\",
        " && printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d \\",
        " && chmod +x /usr/sbin/policy-rc.d \\",
        " && apt-get update \\",
        " && DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "--no-install-recommends -- \\",
        f"    {packages} \\",
        " && rm -f /usr/sbin/policy-rc.d",
        f"RUN test \"$(uv --version | cut -d ' ' -f 1-2)\" = "
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
        "-I /opt/cdh/build/checkers/application.py inventory "
        f"{_shell_word(bootstrap_check)} \\",
        f" && test -x {_shell_word(plan.application.paths.venv + '/bin/pip')} \\",
        f" && test -x {_shell_word(plan.application.paths.venv + '/bin/pip3')}",
        f"RUN uv --no-config build --wheel --python {_shell_word(interpreter)} "
        "--out-dir /opt/cdh/wheel /opt/cdh/source \\",
        ' && test "$(find /opt/cdh/wheel -maxdepth 1 -type f '
        "-name '*.whl' | wc -l)\" = 1 \\",
        f" && uv --no-config tool install --python {_shell_word(interpreter)} "
        f"--no-python-downloads --default-index "
        f"{_shell_word(plan.application.python_index_url)} "
        f"--with-requirements {_shell_word(requirements)} /opt/cdh/wheel/*.whl \\",
        f" && test -x {_shell_word(plan.toolchain.tool_store.cdh_executable)} \\",
        f" && {_shell_word(plan.toolchain.tool_store.cdh_environment + '/bin/python')} "
        f"-c {_shell_word(inventory_check)} {_shell_word(inventory)} \\",
        f" && uv --no-config pip check --python "
        f"{_shell_word(plan.toolchain.tool_store.cdh_environment + '/bin/python')} "
        "--no-python-downloads \\",
        " && rm -rf /opt/cdh/source /opt/cdh/wheel",
    ]
    if plan.toolchain.tool_store.comfy_cli is not None:
        tool = plan.toolchain.tool_store.comfy_cli
        tool_python = f"{plan.toolchain.tool_store.tool_dir}/{tool.name}/bin/python"
        tool_environment = f"{plan.toolchain.tool_store.tool_dir}/{tool.name}"
        inventory = tool.inventory_path
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
        inventory_script = "; ".join(
            (
                "import importlib.metadata as m, re",
                "normalize=lambda value: re.sub(r'[-_.]+', '-', value).lower()",
                "items=sorted((normalize(d.metadata['Name']), d.version) "
                "for d in m.distributions())",
                "assert len(items) == len({name for name, _ in items})",
                "print('\\n'.join(f'{name}=={version}' for name, version in items))",
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
            f"RUN {preflight} \\\n"
            f" && uv --no-config tool install --python {_shell_word(interpreter)} "
            f"--no-python-downloads --default-index "
            f"{_shell_word(plan.application.python_index_url)} "
            f"{_shell_word(tool.requirement)} \\\n"
            f" && test -x {_shell_word(tool_python)} \\\n"
            f" && {_shell_word(tool_python)} -c {_shell_word(direct_check)} \\\n"
            f" && uv --no-config pip check --python {_shell_word(tool_python)} "
            f"--no-python-downloads \\\n"
            f" && {_shell_word(tool_python)} -c {_shell_word(inventory_script)} "
            f"> {_shell_word(inventory)} \\\n"
            f"{links} \\\n"
            f" && grep -Fqx {_shell_word(f'comfy-cli=={tool.version}')} "
            f"{_shell_word(inventory)}"
        )
    for tool in plan.toolchain.tool_store.uv_tools:
        tool_python = f"{plan.toolchain.tool_store.tool_dir}/{tool.name}/bin/python"
        direct_check = (
            "import importlib.metadata as m; "
            f"assert m.version({tool.name!r}) == {tool.version!r}"
        )
        lines.append(
            f"RUN uv --no-config tool install --python {_shell_word(interpreter)} "
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
    phase_digest = _shell_word(build_plan_digest(plan))
    lines.append(
        f"RUN {_shell_word(plan.toolchain.tool_store.cdh_executable)} "
        "container install-comfyui "
        "--application-phase /opt/cdh/build/phases/application.json "
        "--toolchain-phase /opt/cdh/build/phases/toolchain.json "
        f"--build-plan-digest {phase_digest} "
        "--resolution-manifest /opt/cdh/build/pyproject.toml "
        "--constraints /opt/cdh/build/python-package-constraints.txt"
    )
    lines.append(
        f"RUN {_shell_word(plan.toolchain.tool_store.cdh_executable)} "
        "container install-custom-nodes "
        "--custom-nodes-phase /opt/cdh/build/phases/custom-nodes.json "
        "--application-phase /opt/cdh/build/phases/application.json "
        f"--build-plan-digest {phase_digest} "
        "--constraints /opt/cdh/build/python-package-constraints.txt "
        "--hooks-directory /opt/cdh/build/inputs"
    )
    return lines


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
