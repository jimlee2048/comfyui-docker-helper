"""Custom-node helper configuration and install planning."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import TypeAdapter, ValidationError

from comfyui_docker_helper.config.models import (
    CustomNodeConfig,
    GitCustomNodeConfig,
)
from comfyui_docker_helper.config.plan import (
    CustomNodePlan,
    CustomNodesPlan,
    GitCustomNodePlan,
    RegistryCustomNodePlan,
)
from comfyui_docker_helper.config.validation import resolve_git_target_dir
from comfyui_docker_helper.errors import ApplicationError

_CUSTOM_NODES_ADAPTER = TypeAdapter(list[CustomNodeConfig])
_HOOK_SUFFIXES = frozenset({".py", ".sh"})


class CustomNodesConfigError(ApplicationError):
    """A user-facing custom-node helper configuration failure."""


def load_custom_nodes_plan(
    config_path: str | Path,
    *,
    scripts_dir: str | Path | None = None,
) -> CustomNodesPlan:
    """Load the generated helper TOML and build a deterministic install plan."""

    path = Path(config_path)
    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise CustomNodesConfigError(
            f"custom-node config does not exist: {path}"
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise CustomNodesConfigError(
            f"custom-node config is not valid TOML: {path}: {error}"
        ) from error
    except OSError as error:
        raise CustomNodesConfigError(
            f"custom-node config cannot be read: {path}: {error}"
        ) from error

    return build_custom_nodes_plan(document, scripts_dir=scripts_dir)


def build_custom_nodes_plan(
    document: dict[str, Any],
    *,
    scripts_dir: str | Path | None = None,
) -> CustomNodesPlan:
    """Validate a helper config document and build an ordered node plan."""

    nodes = _extract_custom_nodes(document)
    try:
        custom_nodes = _CUSTOM_NODES_ADAPTER.validate_python(nodes)
    except ValidationError as error:
        raise CustomNodesConfigError(
            f"custom-node config validation failed: {error}"
        ) from error

    items: list[CustomNodePlan] = []
    has_registry = False
    has_hooks = False
    registry_ids: set[str] = set()
    git_urls: set[str] = set()
    git_target_dirs: dict[str, str] = {}
    resolved_scripts_dir = _resolve_scripts_dir(
        any(
            node.pre_install_scripts or node.post_install_scripts
            for node in custom_nodes
        ),
        scripts_dir,
    )

    for index, node in enumerate(custom_nodes):
        pre_hooks = tuple(node.pre_install_scripts)
        post_hooks = tuple(node.post_install_scripts)
        has_hooks = has_hooks or bool(pre_hooks or post_hooks)
        _validate_hooks(index, pre_hooks, post_hooks, resolved_scripts_dir)

        if isinstance(node, GitCustomNodeConfig):
            if node.url in git_urls:
                raise CustomNodesConfigError(
                    f"custom-node config duplicates Git URL at item {index}: {node.url}"
                )
            git_urls.add(node.url)
            try:
                git_target_dir = resolve_git_target_dir(node.url, node.target_dir)
            except ValueError as error:
                raise CustomNodesConfigError(
                    f"custom-node config has invalid Git target directory at "
                    f"item {index}: {error}"
                ) from error

            existing_url = git_target_dirs.get(git_target_dir)
            if existing_url is not None and existing_url != node.url:
                raise CustomNodesConfigError(
                    f"custom-node config duplicates Git target directory at "
                    f"item {index}: {git_target_dir}"
                )
            git_target_dirs.setdefault(git_target_dir, node.url)
            items.append(
                GitCustomNodePlan(
                    type="git",
                    url=node.url,
                    ref=node.ref,
                    target_dir=node.target_dir,
                    target=node.url if node.ref is None else f"{node.url}@{node.ref}",
                    pre_install_scripts=pre_hooks,
                    post_install_scripts=post_hooks,
                )
            )
        else:
            if node.id in registry_ids:
                raise CustomNodesConfigError(
                    f"custom-node config duplicates registry ID at item {index}: "
                    f"{node.id}"
                )
            registry_ids.add(node.id)
            has_registry = True
            items.append(
                RegistryCustomNodePlan(
                    type="registry",
                    id=node.id,
                    version=node.version,
                    target=(
                        node.id if node.version is None else f"{node.id}@{node.version}"
                    ),
                    pre_install_scripts=pre_hooks,
                    post_install_scripts=post_hooks,
                )
            )

    return CustomNodesPlan(
        items=tuple(items),
        update_cache=has_registry,
        has_hooks=has_hooks,
        scripts_source_dir=resolved_scripts_dir,
    )


def _extract_custom_nodes(document: dict[str, Any]) -> Any:
    if set(document) != {"comfyui"}:
        raise CustomNodesConfigError(
            "custom-node config must contain only a [comfyui] table"
        )

    comfyui = document["comfyui"]
    if not isinstance(comfyui, dict):
        raise CustomNodesConfigError("custom-node config [comfyui] must be a table")

    if set(comfyui) != {"custom_nodes"}:
        raise CustomNodesConfigError(
            "custom-node config [comfyui] must contain only custom_nodes"
        )

    nodes = comfyui["custom_nodes"]
    if not isinstance(nodes, list):
        raise CustomNodesConfigError(
            "custom-node config comfyui.custom_nodes must be a list"
        )
    return nodes


def _resolve_scripts_dir(
    has_hooks: bool,
    scripts_dir: str | Path | None,
) -> Path | None:
    if not has_hooks:
        return None
    if scripts_dir is None:
        raise CustomNodesConfigError(
            "custom-node hooks require --scripts-dir to be provided"
        )

    path = Path(scripts_dir)
    if not path.is_dir():
        raise CustomNodesConfigError(
            f"custom-node hooks require an existing scripts directory: {path}"
        )
    return path.resolve()


def _validate_hooks(
    node_index: int,
    pre_hooks: tuple[str, ...],
    post_hooks: tuple[str, ...],
    scripts_dir: Path | None,
) -> None:
    for field_name, hooks in (
        ("pre_install_scripts", pre_hooks),
        ("post_install_scripts", post_hooks),
    ):
        for hook_index, hook in enumerate(hooks):
            _validate_hook(
                hook,
                location=f"custom_nodes.{node_index}.{field_name}.{hook_index}",
                scripts_dir=scripts_dir,
            )


def _validate_hook(
    hook: str,
    *,
    location: str,
    scripts_dir: Path | None,
) -> None:
    hook_path = PurePosixPath(hook)
    if hook_path.is_absolute():
        raise CustomNodesConfigError(
            f"custom-node hook {location} must be relative to --scripts-dir"
        )
    if ".." in hook_path.parts:
        raise CustomNodesConfigError(
            f"custom-node hook {location} must not contain '..'"
        )
    if hook_path.suffix not in _HOOK_SUFFIXES:
        raise CustomNodesConfigError(
            f"custom-node hook {location} must end in .sh or .py"
        )

    if scripts_dir is None:
        return
    source = scripts_dir.joinpath(*hook_path.parts)
    if not source.is_file():
        raise CustomNodesConfigError(
            f"custom-node hook {location} must reference an existing file: {hook}"
        )
