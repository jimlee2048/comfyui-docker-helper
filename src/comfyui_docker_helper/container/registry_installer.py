"""Exact Registry custom-node installation through checkout-owned Manager."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    CustomNodesPhase,
    RegistryNodePlan,
)
from comfyui_docker_helper.container.application_installer import (
    _isolated_install_environment,
)
from comfyui_docker_helper.container.comfyui_installer import (
    capture_manager_registry_authority,
    verify_manager_registry_capability,
)
from comfyui_docker_helper.container.phase_inputs import load_phase_input
from comfyui_docker_helper.container.runners import (
    ContainerRuntime,
    run_argv,
    run_hook,
)
from comfyui_docker_helper.errors import ApplicationError

_UV_PATH = Path("/usr/local/bin/uv")
_BUILD_DIRECTORY = Path("/opt/cdh/build")
_CONSTRAINTS_PATH = _BUILD_DIRECTORY / "python-package-constraints.txt"
_HOOKS_DIRECTORY = _BUILD_DIRECTORY / "inputs"


class RegistryInstallError(ApplicationError):
    """A Registry process or installed-state invariant failed."""


@dataclass(frozen=True, slots=True)
class _ObservedRegistryIdentity:
    name: str
    normalized_name: str
    version: str
    parsed_version: Version


def install_registry_nodes(
    custom_nodes_phase_path: str | Path,
    application_phase_path: str | Path,
    *,
    expected_build_plan_digest: str,
    runtime: ContainerRuntime,
    uv_path: Path = _UV_PATH,
    constraints_path: Path = _CONSTRAINTS_PATH,
    hooks_directory: Path = _HOOKS_DIRECTORY,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Install and prove every declared Registry node in one ordered sequence."""
    custom_nodes = load_phase_input(
        custom_nodes_phase_path,
        "custom-nodes",
        expected_build_plan_digest=expected_build_plan_digest,
    )
    application = load_phase_input(
        application_phase_path,
        "application",
        expected_build_plan_digest=expected_build_plan_digest,
    )
    if not isinstance(custom_nodes, CustomNodesPhase) or not isinstance(
        application, ApplicationPhase
    ):  # pragma: no cover - strict phase loader owns the types.
        raise RegistryInstallError("invalid Registry phase inputs")
    nodes = tuple(
        node for node in custom_nodes.nodes if isinstance(node, RegistryNodePlan)
    )
    _validate_inputs(custom_nodes, application, nodes, runtime)
    manager = application.comfyui.manager
    if manager is None:  # pragma: no cover - _validate_inputs owns this branch.
        raise RegistryInstallError("Registry nodes require Manager")
    manager_authority = capture_manager_registry_authority(application, runtime)

    custom_nodes_root = runtime.comfyui_path / "custom_nodes"
    admitted: list[RegistryNodePlan] = []
    command_environment = _registry_environment(
        runtime,
        application.python_index_url,
        constraints_path,
        environ,
    )
    for node in nodes:
        for hook in node.pre_install:
            run_hook(
                hook.relative_path,
                scripts_dir=hooks_directory,
                runtime=runtime,
                env=environ,
            )
        verify_manager_registry_capability(
            application,
            runtime,
            manager_authority,
        )
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
        _verify_registry_set(
            custom_nodes_root,
            (node,),
            allowed=(*admitted, node),
        )
        admitted.append(node)
        for hook in node.post_install:
            run_hook(
                hook.relative_path,
                scripts_dir=hooks_directory,
                runtime=runtime,
                env=environ,
            )
            verify_manager_registry_capability(
                application,
                runtime,
                manager_authority,
            )
            _verify_registry_set(custom_nodes_root, admitted)
        # This is intentionally unconditional so an empty post phase still
        # catches a current pre-hook mutation of an earlier admitted node.
        verify_manager_registry_capability(
            application,
            runtime,
            manager_authority,
        )
        _verify_registry_set(custom_nodes_root, admitted)

    verify_manager_registry_capability(
        application,
        runtime,
        manager_authority,
    )
    _verify_registry_set(custom_nodes_root, nodes)
    _write_registry_inventory(
        Path(custom_nodes.registry_inventory),
        _registry_inventory_bytes(nodes),
    )
    run_argv(
        (
            uv_path,
            "--no-config",
            "pip",
            "check",
            "--python",
            runtime.python,
            "--no-python-downloads",
        ),
        cwd=_BUILD_DIRECTORY,
        env=_isolated_install_environment(environ),
        description="application dependency verification after Registry install",
    )


def _validate_inputs(
    custom_nodes: CustomNodesPhase,
    application: ApplicationPhase,
    nodes: tuple[RegistryNodePlan, ...],
    runtime: ContainerRuntime,
) -> None:
    if not nodes:
        raise RegistryInstallError("Registry phase contains no Registry nodes")
    if not custom_nodes.install_manager or application.comfyui.manager is None:
        raise RegistryInstallError("Registry nodes require Manager")
    if runtime.workspace != Path(application.paths.workspace):
        raise RegistryInstallError("Registry workspace does not match BuildPlan")
    if runtime.comfyui_path != Path(application.paths.comfyui):
        raise RegistryInstallError("Registry ComfyUI path does not match BuildPlan")
    if runtime.virtual_env != Path(application.paths.venv):
        raise RegistryInstallError("Registry application venv does not match BuildPlan")
    if Path(custom_nodes.user_directory) != runtime.comfyui_path / "user":
        raise RegistryInstallError("Registry user directory does not match BuildPlan")
    declared: set[str] = set()
    for node in nodes:
        try:
            normalized = canonicalize_name(node.id, validate=True)
            Version(node.version)
        except InvalidName as error:
            raise RegistryInstallError(
                "Registry node has an invalid locked ID"
            ) from error
        except InvalidVersion as error:
            raise RegistryInstallError(
                f"Registry node {node.id} has an invalid locked version"
            ) from error
        if normalized in declared:
            raise RegistryInstallError(
                f"Registry identity {normalized} is duplicated in BuildPlan"
            )
        declared.add(normalized)


def _registry_environment(
    runtime: ContainerRuntime,
    python_index_url: str,
    constraints_path: Path,
    environ: Mapping[str, str] | None,
) -> dict[str, str]:
    environment = _isolated_install_environment(environ)
    environment.update(
        {
            "COMFYUI_PATH": os.fspath(runtime.comfyui_path),
            "PIP_CONSTRAINT": os.fspath(constraints_path),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_INDEX_URL": python_index_url,
            "UV_CONSTRAINT": os.fspath(constraints_path),
            "UV_DEFAULT_INDEX": python_index_url,
            "UV_NO_CONFIG": "1",
            "VIRTUAL_ENV": os.fspath(runtime.virtual_env),
            "WORKSPACE": os.fspath(runtime.workspace),
            "PATH": (f"{runtime.virtual_env}/bin:/usr/local/bin:/usr/bin:/bin"),
        }
    )
    return environment


def _verify_registry_set(
    custom_nodes_root: Path,
    expected: Sequence[RegistryNodePlan],
    *,
    allowed: Sequence[RegistryNodePlan] | None = None,
) -> None:
    observed = _scan_registry_identities(custom_nodes_root)
    for node in expected:
        normalized = canonicalize_name(node.id, validate=True)
        identity = observed.get(normalized)
        if identity is None:
            raise RegistryInstallError(
                f"Registry node {node.id}@{node.version} is not installed"
            )
        try:
            expected_version = Version(node.version)
        except InvalidVersion as error:
            raise RegistryInstallError(
                f"Registry node {node.id} has an invalid locked version"
            ) from error
        if identity.parsed_version != expected_version:
            raise RegistryInstallError(
                f"Registry node {node.id} version does not match BuildPlan"
            )
    allowed_nodes = expected if allowed is None else allowed
    allowed_identities = {
        canonicalize_name(node.id, validate=True) for node in allowed_nodes
    }
    if set(observed) != allowed_identities:
        raise RegistryInstallError(
            "installed Registry identities do not match the admitted declaration prefix"
        )


def _scan_registry_identities(
    custom_nodes_root: Path,
) -> dict[str, _ObservedRegistryIdentity]:
    root = _require_real_directory(custom_nodes_root, "custom-nodes root")
    observed: dict[str, _ObservedRegistryIdentity] = {}
    try:
        children = tuple(
            sorted(custom_nodes_root.iterdir(), key=lambda item: item.name)
        )
    except OSError as error:
        raise RegistryInstallError("custom-nodes root could not be scanned") from error
    for child in children:
        try:
            child_metadata = child.lstat()
        except OSError as error:
            raise RegistryInstallError(
                "custom-node entry could not be inspected"
            ) from error
        if stat.S_ISLNK(child_metadata.st_mode):
            raise RegistryInstallError("custom-node entries must not be symlinks")
        if stat.S_ISREG(child_metadata.st_mode):
            continue
        if not stat.S_ISDIR(child_metadata.st_mode):
            raise RegistryInstallError(
                "custom-node entries must be regular files or real directories"
            )
        resolved_child = _require_real_directory(child, "custom-node directory")
        if resolved_child.parent != root:
            raise RegistryInstallError(
                "custom-node directory escapes the declared root"
            )
        project_file = child / "pyproject.toml"
        try:
            project_metadata = project_file.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RegistryInstallError(
                "custom-node metadata could not be inspected"
            ) from error
        if stat.S_ISLNK(project_metadata.st_mode) or not stat.S_ISREG(
            project_metadata.st_mode
        ):
            raise RegistryInstallError("custom-node metadata must be one regular file")
        try:
            resolved_project = project_file.resolve(strict=True)
            content = project_file.read_bytes()
        except OSError as error:
            raise RegistryInstallError(
                "custom-node metadata could not be read"
            ) from error
        if (
            resolved_project.parent != resolved_child
            or not resolved_project.is_relative_to(root)
        ):
            raise RegistryInstallError("custom-node metadata escapes the declared root")
        identity = _parse_project_identity(content)
        if identity.normalized_name in observed:
            raise RegistryInstallError(
                f"Registry identity {identity.normalized_name} is duplicated"
            )
        observed[identity.normalized_name] = identity
    return observed


def _require_real_directory(path: Path, subject: str) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RegistryInstallError(f"{subject} is unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path
    ):
        raise RegistryInstallError(f"{subject} must be one real directory")
    return resolved


def _parse_project_identity(content: bytes) -> _ObservedRegistryIdentity:
    try:
        document = tomllib.loads(content.decode("utf-8"))
        project = document["project"]
        name = project["name"]
        version = project["version"]
        if not isinstance(name, str) or not isinstance(version, str):
            raise TypeError
        normalized_name = canonicalize_name(name, validate=True)
        parsed_version = Version(version)
    except (
        InvalidName,
        InvalidVersion,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
    ) as error:
        raise RegistryInstallError(
            "custom-node pyproject.toml has invalid project identity"
        ) from error
    return _ObservedRegistryIdentity(
        name=name,
        normalized_name=normalized_name,
        version=version,
        parsed_version=parsed_version,
    )


def _registry_inventory_bytes(nodes: Sequence[RegistryNodePlan]) -> bytes:
    document = {
        "schema_version": 1,
        "nodes": [
            {
                "type": "registry",
                "id": node.id,
                "version": node.version,
                "verification": "registry-version",
                "control": "direct-cm-cli",
            }
            for node in nodes
        ],
    }
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_registry_inventory(
    path: Path,
    content: bytes,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    parent = _require_real_directory(path.parent, "Registry inventory parent")
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise RegistryInstallError(
            "Registry inventory target could not be inspected"
        ) from error
    else:
        raise RegistryInstallError("Registry inventory target already exists")

    temporary: Path | None = None
    linked = False
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
            os.fchown(stream.fileno(), owner_uid, owner_gid)
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        temporary.unlink()
        temporary = None
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or path.read_bytes() != content
        ):
            raise RegistryInstallError("Registry inventory verification failed")
    except RegistryInstallError:
        if linked:
            path.unlink(missing_ok=True)
        raise
    except OSError as error:
        if linked:
            path.unlink(missing_ok=True)
        raise RegistryInstallError("Registry inventory could not be created") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
