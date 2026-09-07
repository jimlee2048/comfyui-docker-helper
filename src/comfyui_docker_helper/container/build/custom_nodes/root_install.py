"""Shared optional root installation for direct Git and local nodes."""

from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path

from comfyui_docker_helper.config.planning.build_plan import ApplicationPhase
from comfyui_docker_helper.config.planning.requirements import (
    ComfyUIRequirementsError,
    parse_ordinary_requirements,
)
from comfyui_docker_helper.container.build.custom_nodes import contracts
from comfyui_docker_helper.container.process.runners import ContainerRuntime, run_argv


def install_root_surfaces(
    description: str,
    target: Path,
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    uv_path: Path,
    constraints_path: Path,
    python_environment: Mapping[str, str],
) -> None:
    requirements = _optional_root_file(target, "requirements.txt", description)
    if requirements is not None:
        try:
            requirements_rows = parse_ordinary_requirements(
                requirements.read_bytes(),
                python_version=application.pytorch.python_version,
                platform=application.pytorch.platform,
                machine="x86_64",
            )
        except (OSError, ComfyUIRequirementsError) as error:
            raise contracts.CustomNodeInstallError(
                f"{description} requirements are invalid"
            ) from error
        if requirements_rows:
            run_argv(
                (
                    uv_path,
                    "--no-config",
                    "pip",
                    "install",
                    "--python",
                    runtime.python,
                    "--no-python-downloads",
                    "--default-index",
                    application.python_index_url,
                    "--constraint",
                    constraints_path,
                    "--requirements",
                    requirements,
                ),
                cwd=target,
                env=python_environment,
                description=f"{description} requirements install",
                close_stdin=True,
            )
    install_script = _optional_root_file(target, "install.py", description)
    if install_script is not None:
        run_argv(
            (runtime.python, install_script),
            cwd=target,
            env=python_environment,
            description=f"{description} install.py",
            close_stdin=True,
        )


def _optional_root_file(root: Path, name: str, description: str) -> Path | None:
    path = root / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise contracts.CustomNodeInstallError(
            f"{description} root {name} could not be inspected"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise contracts.CustomNodeInstallError(
            f"{description} root {name} must be one regular file"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise contracts.CustomNodeInstallError(
            f"{description} root {name} could not be resolved"
        ) from error
    if resolved.parent != root:
        raise contracts.CustomNodeInstallError(
            f"{description} root {name} escapes its node directory"
        )
    return path
