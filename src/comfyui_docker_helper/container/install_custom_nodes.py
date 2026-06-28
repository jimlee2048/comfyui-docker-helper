"""Container-side custom-node installation orchestration."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

from comfyui_docker_helper.config.plan import CustomNodePlan
from comfyui_docker_helper.config.validation import resolve_git_target_dir
from comfyui_docker_helper.container.custom_nodes import load_custom_nodes_plan
from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
    run_argv,
    run_hooks,
)

_DEFAULT_RUNTIME = ContainerRuntime()


class Logger(Protocol):
    """Minimal logger protocol used by the container helper."""

    def __call__(self, message: str) -> None: ...


def install_custom_nodes(
    config_path: str | Path,
    *,
    scripts_dir: str | Path | None = None,
    runtime: ContainerRuntime = _DEFAULT_RUNTIME,
    log: Logger = print,
) -> None:
    """Install custom nodes from the generated helper config."""

    plan = load_custom_nodes_plan(config_path, scripts_dir=scripts_dir)
    env = runtime.env()

    if plan.update_cache:
        log("Updating ComfyUI-Manager registry cache")
        run_argv(
            [str(runtime.python), "-m", "cm_cli", "update-cache"],
            cwd=runtime.comfyui_path,
            env=env,
            description="custom-node registry cache update",
        )

    total = len(plan.items)
    for index, node in enumerate(plan.items, 1):
        log(f"Installing custom node {index}/{total}: {node.target}")
        _run_node_hooks(
            "pre",
            node,
            scripts_dir=scripts_dir,
            runtime=runtime,
            env=env,
            log=log,
        )
        _install_node(node, runtime=runtime, env=env)
        _run_node_hooks(
            "post",
            node,
            scripts_dir=scripts_dir,
            runtime=runtime,
            env=env,
            log=log,
        )


def _install_node(
    node: CustomNodePlan,
    *,
    runtime: ContainerRuntime,
    env: dict[str, str],
) -> None:
    if node.type == "git":
        _install_git_node(node, runtime=runtime, env=env)
        return

    run_argv(
        [
            "comfy",
            "--skip-prompt",
            "--workspace",
            str(runtime.comfyui_path),
            "node",
            "install",
            "--exit-on-fail",
            "--fast-deps",
            node.target,
        ],
        cwd=runtime.comfyui_path,
        env=env,
        description=f"custom-node install {node.target}",
    )


def _install_git_node(
    node: CustomNodePlan,
    *,
    runtime: ContainerRuntime,
    env: dict[str, str],
) -> None:
    if node.type != "git":  # pragma: no cover - caller guards this branch.
        raise ContainerCommandError(f"not a git custom node: {node.target}")

    repo_name = _derive_git_repo_name(node.url, node.target_dir)
    repo_path = runtime.comfyui_path / "custom_nodes" / repo_name
    if repo_path.exists():
        raise ContainerCommandError(
            f"custom-node git target already exists: {repo_path}"
        )

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    run_argv(
        ["git", "clone", "--recursive", node.url, str(repo_path)],
        cwd=runtime.comfyui_path,
        env=env,
        description=f"custom-node git clone {node.target}",
    )

    if node.ref:
        run_argv(
            ["git", "-C", str(repo_path), "checkout", "--detach", node.ref],
            cwd=runtime.comfyui_path,
            env=env,
            description=f"custom-node git checkout {node.target}",
        )
        run_argv(
            [
                "git",
                "-C",
                str(repo_path),
                "submodule",
                "update",
                "--init",
                "--recursive",
            ],
            cwd=runtime.comfyui_path,
            env=env,
            description=f"custom-node git submodules {node.target}",
        )
        _verify_git_ref(node, repo_path=repo_path, env=env)

    requirements = repo_path / "requirements.txt"
    if requirements.is_file():
        run_argv(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(runtime.python),
                "-r",
                str(requirements),
            ],
            cwd=repo_path,
            env=env,
            description=f"custom-node git requirements {node.target}",
        )

    install_script = repo_path / "install.py"
    if install_script.is_file():
        run_argv(
            [str(runtime.python), str(install_script)],
            cwd=repo_path,
            env=env,
            description=f"custom-node git install.py {node.target}",
        )


def _derive_git_repo_name(url: str, target_dir: str | None = None) -> str:
    try:
        return resolve_git_target_dir(url, target_dir)
    except ValueError as error:
        raise ContainerCommandError(
            f"cannot derive git repo directory from URL: {url}: {error}"
        ) from error


def _verify_git_ref(
    node: CustomNodePlan,
    *,
    repo_path: Path,
    env: dict[str, str],
) -> None:
    if node.type != "git" or not node.ref:  # pragma: no cover - caller guards this.
        return
    head = _git_output(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        cwd=repo_path,
        env=env,
        description=f"custom-node git rev-parse {node.target}",
    )
    if re.fullmatch(r"[0-9a-fA-F]{40}", node.ref) and head.lower() != node.ref.lower():
        raise ContainerCommandError(
            f"custom-node git ref verification failed for {node.target}: "
            f"expected {node.ref}, got {head}"
        )


def _git_output(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    description: str,
) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=os.fspath(cwd),
            env=dict(env),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise ContainerCommandError(
            f"{description} executable not found: {argv[0]}"
        ) from error
    except OSError as error:
        raise ContainerCommandError(
            f"{description} failed to start: {error}"
        ) from error

    if result.returncode != 0:
        stderr = result.stderr.strip()
        details = f": {stderr}" if stderr else ""
        raise ContainerCommandError(
            f"{description} failed with exit code {result.returncode}{details}",
            exit_code=result.returncode if result.returncode > 0 else 1,
        )
    return result.stdout.strip()


def _run_node_hooks(
    phase: str,
    node: CustomNodePlan,
    *,
    scripts_dir: str | Path | None,
    runtime: ContainerRuntime,
    env: dict[str, str],
    log: Logger,
) -> None:
    hooks = node.pre_install_scripts if phase == "pre" else node.post_install_scripts
    if not hooks:
        return
    if scripts_dir is None:
        raise RuntimeError("custom-node plan has hooks but no scripts-dir")

    log(f"Running {phase}-install hooks for custom node: {node.target}")
    run_hooks(hooks, scripts_dir=scripts_dir, runtime=runtime, env=env)
