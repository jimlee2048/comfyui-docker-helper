"""Registry custom-node installation, scanning, and proof helpers."""

from __future__ import annotations

import stat
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.planning.build_plan import (
    ApplicationPhase,
    CustomNodesPhase,
    RegistryNodePlan,
)
from comfyui_docker_helper.config.planning.canonical_lock import normalized_registry_id
from comfyui_docker_helper.config.planning.requirements import ParsedManagerRequirements
from comfyui_docker_helper.container.build.custom_nodes import contracts
from comfyui_docker_helper.container.process.runners import ContainerRuntime, run_argv


@dataclass(frozen=True, slots=True)
class _ObservedRegistryIdentity:
    name: str
    normalized_name: str
    version: str
    parsed_version: Version


def _install_registry_node(
    node: RegistryNodePlan,
    custom_nodes: CustomNodesPhase,
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    manager_authority: ParsedManagerRequirements | None,
    command_environment: Mapping[str, str],
) -> None:
    manager = application.comfyui.manager
    if manager is None or manager_authority is None:  # pragma: no cover - validated.
        raise contracts.CustomNodeInstallError("Registry nodes require Manager")
    run_argv(
        (
            manager.executable,
            "install",
            f"{node.id}@{node.version}",
            "--mode",
            "cache",
            "--user-directory",
            custom_nodes.user_directory,
            "--exit-on-fail",
        ),
        cwd=runtime.comfyui_path,
        env=command_environment,
        description=f"Registry node {node.id}@{node.version} install",
        close_stdin=True,
    )


def _verify_registry_set(
    custom_nodes_root: Path,
    expected: Sequence[RegistryNodePlan],
    *,
    excluded_git_targets: Sequence[Path] = (),
) -> None:
    observed = _scan_registry_identities(
        custom_nodes_root, excluded_git_targets=excluded_git_targets
    )
    for node in expected:
        normalized = normalized_registry_id(node.id)
        identity = observed.get(normalized)
        if identity is None:
            raise contracts.CustomNodeInstallError(
                f"Registry node {node.id}@{node.version} is not installed"
            )
        try:
            expected_version = Version(node.version)
        except InvalidVersion as error:
            raise contracts.CustomNodeInstallError(
                f"Registry node {node.id} has an invalid locked version"
            ) from error
        if identity.parsed_version != expected_version:
            raise contracts.CustomNodeInstallError(
                f"Registry node {node.id} version does not match BuildPlan"
            )
    expected_names = {normalized_registry_id(node.id) for node in expected}
    if set(observed) != expected_names:
        raise contracts.CustomNodeInstallError(
            "installed Registry identities do not match the admitted declaration prefix"
        )


def _scan_registry_identities(
    custom_nodes_root: Path,
    *,
    excluded_git_targets: Sequence[Path] = (),
) -> dict[str, _ObservedRegistryIdentity]:
    root = contracts._require_real_directory(custom_nodes_root, "custom-nodes root")
    excluded = set(excluded_git_targets)
    if any(path.parent != root for path in excluded):
        raise contracts.CustomNodeInstallError(
            "Git exclusion target escapes custom-nodes root"
        )
    observed: dict[str, _ObservedRegistryIdentity] = {}
    try:
        children = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    except OSError as error:
        raise contracts.CustomNodeInstallError(
            "custom-nodes root could not be scanned"
        ) from error
    for child in children:
        if child in excluded:
            continue
        try:
            child_metadata = child.lstat()
        except OSError as error:
            raise contracts.CustomNodeInstallError(
                "custom-node entry could not be inspected"
            ) from error
        if stat.S_ISLNK(child_metadata.st_mode):
            raise contracts.CustomNodeInstallError(
                "custom-node entries must not be symlinks"
            )
        if stat.S_ISREG(child_metadata.st_mode):
            continue
        if not stat.S_ISDIR(child_metadata.st_mode):
            raise contracts.CustomNodeInstallError(
                "custom-node entries must be regular files or real directories"
            )
        resolved_child = contracts._require_real_directory(
            child, "custom-node directory"
        )
        if resolved_child.parent != root:
            raise contracts.CustomNodeInstallError(
                "custom-node directory escapes the declared root"
            )
        project_file = child / "pyproject.toml"
        try:
            project_metadata = project_file.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise contracts.CustomNodeInstallError(
                "custom-node metadata could not be inspected"
            ) from error
        if stat.S_ISLNK(project_metadata.st_mode) or not stat.S_ISREG(
            project_metadata.st_mode
        ):
            raise contracts.CustomNodeInstallError(
                "custom-node metadata must be one regular file"
            )
        try:
            resolved_project = project_file.resolve(strict=True)
            content = project_file.read_bytes()
        except OSError as error:
            raise contracts.CustomNodeInstallError(
                "custom-node metadata could not be read"
            ) from error
        if (
            resolved_project.parent != resolved_child
            or not resolved_project.is_relative_to(root)
        ):
            raise contracts.CustomNodeInstallError(
                "custom-node metadata escapes the declared root"
            )
        identity = _parse_project_identity(content)
        if identity.normalized_name in observed:
            raise contracts.CustomNodeInstallError(
                f"Registry identity {identity.normalized_name} is duplicated"
            )
        observed[identity.normalized_name] = identity
    return observed


def _parse_project_identity(content: bytes) -> _ObservedRegistryIdentity:
    try:
        document = tomllib.loads(content.decode("utf-8"))
        project = document["project"]
        name = project["name"]
        version = project["version"]
        if not isinstance(name, str) or not isinstance(version, str):
            raise TypeError
        normalized_name = normalized_registry_id(name)
        parsed_version = Version(version)
    except (
        ValueError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
    ) as error:
        raise contracts.CustomNodeInstallError(
            "custom-node pyproject.toml has invalid project identity"
        ) from error
    return _ObservedRegistryIdentity(
        name=name,
        normalized_name=normalized_name,
        version=version,
        parsed_version=parsed_version,
    )
