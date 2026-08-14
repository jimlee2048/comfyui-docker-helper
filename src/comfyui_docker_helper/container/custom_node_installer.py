"""Ordered Registry and direct-Git custom-node installation and proof."""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.comfyui_requirements import (
    ComfyUIRequirementsError,
    ParsedComfyUIRequirements,
    ParsedManagerRequirements,
    parse_ordinary_requirements,
)
from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    CustomNodePlan,
    CustomNodesPhase,
    GitNodePlan,
    PyTorchGroupPlan,
    RegistryNodePlan,
    managed_build_constraints_bytes,
)
from comfyui_docker_helper.config.canonical_lock import normalized_registry_id
from comfyui_docker_helper.config.custom_node_inventory import (
    CustomNodeInventory,
    custom_node_inventory,
)
from comfyui_docker_helper.config.final_validation import is_git_source_url
from comfyui_docker_helper.config.registry_validation import (
    validate_registry_node_authority,
)
from comfyui_docker_helper.config.selector_validation import is_safe_git_target_dir
from comfyui_docker_helper.container.application_installer import (
    application_install_environment,
)
from comfyui_docker_helper.container.comfyui_installer import (
    capture_application_requirements,
    capture_manager_authority,
    observe_application_state,
    observe_manager_absence,
    observe_manager_capability,
    verify_manager_authority,
)
from comfyui_docker_helper.container.git_credential_helper import (
    GIT_CREDENTIAL_BUILD_PLAN_DIGEST_ENV,
)
from comfyui_docker_helper.container.runners import ContainerRuntime, run_argv, run_hook
from comfyui_docker_helper.errors import ApplicationError
from comfyui_docker_helper.git_credential_policy import (
    GitCredentialPolicyError,
    git_credential_environment,
)

_GIT_PATH = Path("/usr/bin/git")
_UV_PATH = Path("/usr/local/bin/uv")
_BUILD_DIRECTORY = Path("/opt/cdh/build")
_CONSTRAINTS_PATH = _BUILD_DIRECTORY / "python-package-constraints.txt"
_BUILD_HOOKS_DIRECTORY = _BUILD_DIRECTORY / "hooks"
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_GITLINK_MODE = b"160000"


class CustomNodeInstallError(ApplicationError):
    """A custom-node process, placement, or proof invariant failed."""


@dataclass(frozen=True, slots=True)
class _ObservedRegistryIdentity:
    name: str
    normalized_name: str
    version: str
    parsed_version: Version


@dataclass(slots=True)
class _ObservationEpoch:
    dirty: int = 0
    observed: int | None = None

    @classmethod
    def clean(cls) -> _ObservationEpoch:
        return cls(observed=0)

    def invalidate(self) -> None:
        self.dirty += 1

    def observe(self, action: Callable[[], None], *, force: bool = False) -> None:
        if not force and self.observed == self.dirty:
            return
        action()
        self.observed = self.dirty


@dataclass(slots=True)
class _VerificationObservations:
    application: _ObservationEpoch
    manager: _ObservationEpoch | None

    @classmethod
    def initial(cls, *, has_manager_observer: bool) -> _VerificationObservations:
        return cls(
            application=_ObservationEpoch(),
            manager=_ObservationEpoch.clean() if has_manager_observer else None,
        )

    def invalidate_mutation(self) -> None:
        self.application.invalidate()
        if self.manager is not None:
            self.manager.invalidate()


def install_custom_nodes(
    custom_nodes: CustomNodesPhase,
    application: ApplicationPhase,
    *,
    runtime: ContainerRuntime,
    git_path: Path = _GIT_PATH,
    uv_path: Path = _UV_PATH,
    constraints_path: Path = _CONSTRAINTS_PATH,
    build_hooks_directory: Path = _BUILD_HOOKS_DIRECTORY,
    environ: Mapping[str, str] | None = None,
    build_plan_digest: str | None = None,
) -> None:
    """Install all custom nodes in one original-order admitted-prefix sequence."""
    _validate_inputs(custom_nodes, application, runtime)
    with _temporary_build_constraints(application.pytorch) as build_constraints_path:
        _install_custom_nodes(
            custom_nodes,
            application,
            runtime=runtime,
            git_path=git_path,
            uv_path=uv_path,
            constraints_path=constraints_path,
            build_constraints_path=build_constraints_path,
            build_hooks_directory=build_hooks_directory,
            environ=environ,
            build_plan_digest=build_plan_digest,
        )


def _install_custom_nodes(
    custom_nodes: CustomNodesPhase,
    application: ApplicationPhase,
    *,
    runtime: ContainerRuntime,
    git_path: Path,
    uv_path: Path,
    constraints_path: Path,
    build_constraints_path: Path,
    build_hooks_directory: Path,
    environ: Mapping[str, str] | None,
    build_plan_digest: str | None,
) -> None:
    """Execute one validated custom-node sequence."""

    nodes = custom_nodes.nodes
    has_registry = any(isinstance(node, RegistryNodePlan) for node in nodes)
    manager_authority: ParsedManagerRequirements | None = None
    if nodes:
        if application.comfyui.manager is None:
            observe_manager_absence(application, runtime)
        else:
            manager_authority = capture_manager_authority(application, runtime)
    custom_nodes_root = _require_real_directory(
        runtime.comfyui_path / "custom_nodes", "custom-nodes root"
    )
    application_authority = capture_application_requirements(application, runtime)
    custom_node_python_environment = _managed_python_environment(
        runtime,
        application.python_index_url,
        application.pytorch.pytorch_index_url,
        constraints_path,
        build_constraints_path,
        environ,
    )
    # Git/SSH interpretation belongs to the caller's environment. In particular,
    # cdh neither suppresses nor attests user-managed URL rewrites and transports.
    git_environment = _git_environment(
        custom_nodes,
        runtime.env(environ),
        build_plan_digest=build_plan_digest,
    )
    admitted: list[CustomNodePlan] = []
    observations = _VerificationObservations.initial(has_manager_observer=bool(nodes))

    for index, node in enumerate(nodes):
        future = nodes[index:]
        _verify_boundary(
            custom_nodes_root,
            admitted,
            future,
            application=application,
            runtime=runtime,
            manager_authority=manager_authority,
            has_registry=has_registry,
            git_path=git_path,
            git_environment=git_environment,
            observations=observations,
            uv_path=uv_path,
            constraints_path=constraints_path,
            environ=environ,
            application_authority=application_authority,
        )
        for hook in node.pre_install_hooks:
            observations.invalidate_mutation()
            run_hook(
                hook.relative_path,
                expected_digest=hook.digest,
                build_hooks_dir=build_hooks_directory,
                runtime=runtime,
                env=environ,
            )
            _verify_boundary(
                custom_nodes_root,
                admitted,
                future,
                application=application,
                runtime=runtime,
                manager_authority=manager_authority,
                has_registry=has_registry,
                git_path=git_path,
                git_environment=git_environment,
                observations=observations,
                uv_path=uv_path,
                constraints_path=constraints_path,
                environ=environ,
                application_authority=application_authority,
            )
        # The complete pre phase is a proof boundary even when it was empty.
        _verify_boundary(
            custom_nodes_root,
            admitted,
            future,
            application=application,
            runtime=runtime,
            manager_authority=manager_authority,
            has_registry=has_registry,
            git_path=git_path,
            git_environment=git_environment,
            observations=observations,
            uv_path=uv_path,
            constraints_path=constraints_path,
            environ=environ,
            application_authority=application_authority,
        )

        observations.invalidate_mutation()
        if isinstance(node, RegistryNodePlan):
            _install_registry_node(
                node,
                custom_nodes,
                application,
                runtime,
                manager_authority,
                custom_node_python_environment,
            )
        else:
            _install_git_node(
                node,
                custom_nodes_root,
                application,
                runtime,
                git_path,
                uv_path,
                constraints_path,
                git_environment,
                custom_node_python_environment,
            )

        admitted.append(node)
        remaining = nodes[index + 1 :]
        _verify_boundary(
            custom_nodes_root,
            admitted,
            remaining,
            application=application,
            runtime=runtime,
            manager_authority=manager_authority,
            has_registry=has_registry,
            git_path=git_path,
            git_environment=git_environment,
            observations=observations,
            uv_path=uv_path,
            constraints_path=constraints_path,
            environ=environ,
            application_authority=application_authority,
        )
        for hook in node.post_install_hooks:
            observations.invalidate_mutation()
            run_hook(
                hook.relative_path,
                expected_digest=hook.digest,
                build_hooks_dir=build_hooks_directory,
                runtime=runtime,
                env=environ,
            )
            _verify_boundary(
                custom_nodes_root,
                admitted,
                remaining,
                application=application,
                runtime=runtime,
                manager_authority=manager_authority,
                has_registry=has_registry,
                git_path=git_path,
                git_environment=git_environment,
                observations=observations,
                uv_path=uv_path,
                constraints_path=constraints_path,
                environ=environ,
                application_authority=application_authority,
            )
        # The complete post phase is a proof boundary even when it was empty.
        _verify_boundary(
            custom_nodes_root,
            admitted,
            remaining,
            application=application,
            runtime=runtime,
            manager_authority=manager_authority,
            has_registry=has_registry,
            git_path=git_path,
            git_environment=git_environment,
            observations=observations,
            uv_path=uv_path,
            constraints_path=constraints_path,
            environ=environ,
            application_authority=application_authority,
        )

    _verify_boundary(
        custom_nodes_root,
        admitted,
        (),
        application=application,
        runtime=runtime,
        manager_authority=manager_authority,
        has_registry=has_registry,
        git_path=git_path,
        git_environment=git_environment,
        observations=observations,
        uv_path=uv_path,
        constraints_path=constraints_path,
        environ=environ,
        application_authority=application_authority,
        force_manager=True,
        observe_application=False,
    )
    observations.application.observe(
        lambda: observe_application_state(
            application,
            runtime,
            application_authority,
            git_path=git_path,
            uv_path=uv_path,
            constraints_path=constraints_path,
            environ=environ,
        ),
        force=True,
    )


def _git_environment(
    custom_nodes: CustomNodesPhase,
    environment: Mapping[str, str],
    *,
    build_plan_digest: str | None,
) -> dict[str, str]:
    if not custom_nodes.git_credentials or not any(
        isinstance(node, GitNodePlan) for node in custom_nodes.nodes
    ):
        return dict(environment)
    if not build_plan_digest:
        raise CustomNodeInstallError("Git credential BuildPlan identity is unavailable")
    helper = (
        f"!exec {shlex.quote(sys.executable)} "
        "-m comfyui_docker_helper.container.git_credential_helper"
    )
    try:
        return git_credential_environment(
            environment,
            helper=helper,
            overlay={GIT_CREDENTIAL_BUILD_PLAN_DIGEST_ENV: build_plan_digest},
        )
    except GitCredentialPolicyError:
        raise CustomNodeInstallError(
            "Git credential process policy is invalid"
        ) from None


def observe_custom_node_state(
    custom_nodes: CustomNodesPhase,
    *,
    runtime: ContainerRuntime,
    git_path: Path = _GIT_PATH,
    environ: Mapping[str, str] | None = None,
) -> CustomNodeInventory:
    """Prove final local Git and Registry identities without executing node code."""
    custom_nodes_root = _require_real_directory(
        runtime.comfyui_path / "custom_nodes", "custom-nodes root"
    )
    git_environment = runtime.env(environ)
    git_targets: list[Path] = []
    for node in custom_nodes.nodes:
        if not isinstance(node, GitNodePlan):
            continue
        target = _planned_git_target(node, custom_nodes_root)
        _verify_git_provenance(
            node,
            target,
            custom_nodes_root,
            git_path,
            git_environment,
        )
        git_targets.append(target)
    _verify_registry_set(
        custom_nodes_root,
        tuple(
            node for node in custom_nodes.nodes if isinstance(node, RegistryNodePlan)
        ),
        excluded_git_targets=git_targets,
    )
    return custom_node_inventory(custom_nodes.nodes)


def _validate_inputs(
    custom_nodes: CustomNodesPhase,
    application: ApplicationPhase,
    runtime: ContainerRuntime,
) -> None:
    if custom_nodes.nodes and (
        custom_nodes.install_manager != (application.comfyui.manager is not None)
    ):
        raise CustomNodeInstallError(
            "custom-node Manager state does not match application phase"
        )
    if runtime.workspace != Path(application.paths.workspace):
        raise CustomNodeInstallError("custom-node workspace does not match BuildPlan")
    if runtime.comfyui_path != Path(application.paths.comfyui):
        raise CustomNodeInstallError(
            "custom-node ComfyUI path does not match BuildPlan"
        )
    if runtime.virtual_env != Path(application.paths.venv):
        raise CustomNodeInstallError("custom-node venv does not match BuildPlan")
    if Path(custom_nodes.user_directory) != runtime.comfyui_path / "user":
        raise CustomNodeInstallError("Registry user directory does not match BuildPlan")
    git_targets: set[Path] = set()
    custom_root = runtime.comfyui_path / "custom_nodes"
    registry_nodes = tuple(
        node for node in custom_nodes.nodes if isinstance(node, RegistryNodePlan)
    )
    try:
        validate_registry_node_authority(
            (node.id for node in registry_nodes),
            install_manager=custom_nodes.install_manager,
            has_manager_plan=application.comfyui.manager is not None,
        )
    except ValueError as error:
        message = str(error)
        if "must be unique" in message:
            message = "Registry identity is duplicated in BuildPlan"
        elif "Registry nodes require Manager" not in message:
            message = "Registry node has an invalid locked ID"
        raise CustomNodeInstallError(message) from error
    for node in custom_nodes.nodes:
        if isinstance(node, RegistryNodePlan):
            try:
                Version(node.version)
            except InvalidVersion as error:
                raise CustomNodeInstallError(
                    f"Registry node {node.id} has an invalid locked version"
                ) from error
        else:
            if _COMMIT_PATTERN.fullmatch(node.commit) is None:
                raise CustomNodeInstallError("Git node commit must be exact 40-hex")
            if not is_git_source_url(node.url):
                raise CustomNodeInstallError("Git node URL is invalid")
            target = _planned_git_target(node, custom_root)
            if target in git_targets:
                raise CustomNodeInstallError(
                    f"Git target {target.name} is duplicated in BuildPlan"
                )
            git_targets.add(target)


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
        raise CustomNodeInstallError("Registry nodes require Manager")
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


def _install_git_node(
    node: GitNodePlan,
    custom_nodes_root: Path,
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    git_path: Path,
    uv_path: Path,
    constraints_path: Path,
    git_environment: Mapping[str, str],
    python_environment: Mapping[str, str],
) -> None:
    root = _require_real_directory(custom_nodes_root, "custom-nodes root")
    target = _planned_git_target(node, root)
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as error:
        raise CustomNodeInstallError(
            f"Git target {target.name} already exists"
        ) from error
    except OSError as error:
        raise CustomNodeInstallError(
            f"Git target {target.name} could not be created"
        ) from error
    _run_git(
        (git_path, "clone", "--no-checkout", "--", node.url, target),
        cwd=root,
        env=git_environment,
        description=f"Git node {target.name} clone",
    )
    _run_git(
        (git_path, "-C", target, "checkout", "--detach", node.commit, "--"),
        cwd=root,
        env=git_environment,
        description=f"Git node {target.name} exact checkout",
    )
    _run_git(
        (
            git_path,
            "-C",
            target,
            "submodule",
            "update",
            "--init",
            "--recursive",
            "--checkout",
        ),
        cwd=root,
        env=git_environment,
        description=f"Git node {target.name} recursive submodule checkout",
    )
    _verify_git_provenance(node, target, root, git_path, git_environment)
    _install_git_root_surfaces(
        node,
        target,
        application,
        runtime,
        uv_path,
        constraints_path,
        python_environment,
    )


def _install_git_root_surfaces(
    node: GitNodePlan,
    target: Path,
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    uv_path: Path,
    constraints_path: Path,
    python_environment: Mapping[str, str],
) -> None:
    requirements = _optional_root_file(target, "requirements.txt")
    if requirements is not None:
        try:
            requirements_rows = parse_ordinary_requirements(
                requirements.read_bytes(),
                python_version=application.pytorch.python_version,
                platform=application.pytorch.platform,
                machine="x86_64",
            )
        except (OSError, ComfyUIRequirementsError) as error:
            raise CustomNodeInstallError(
                f"Git node {Path(node.target).name} requirements are invalid"
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
                description=f"Git node {target.name} requirements install",
                close_stdin=True,
            )
    install_script = _optional_root_file(target, "install.py")
    if install_script is not None:
        run_argv(
            (runtime.python, install_script),
            cwd=target,
            env=python_environment,
            description=f"Git node {target.name} install.py",
            close_stdin=True,
        )


def _verify_boundary(
    custom_nodes_root: Path,
    admitted: Sequence[CustomNodePlan],
    future: Sequence[CustomNodePlan],
    *,
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    manager_authority: ParsedManagerRequirements | None,
    has_registry: bool,
    git_path: Path,
    git_environment: Mapping[str, str],
    observations: _VerificationObservations,
    uv_path: Path,
    constraints_path: Path,
    environ: Mapping[str, str] | None,
    application_authority: ParsedComfyUIRequirements,
    force_manager: bool = False,
    observe_application: bool = True,
) -> None:
    _verify_mixed_state(
        custom_nodes_root,
        admitted,
        future,
        application=application,
        runtime=runtime,
        manager_authority=manager_authority,
        has_registry=has_registry,
        git_path=git_path,
        git_environment=git_environment,
    )
    manager_epoch = observations.manager
    if manager_epoch is not None:
        manager_epoch.observe(
            lambda: (
                observe_manager_absence(application, runtime)
                if manager_authority is None
                else observe_manager_capability(application, runtime, manager_authority)
            ),
            force=force_manager,
        )
    if observe_application:
        observations.application.observe(
            lambda: observe_application_state(
                application,
                runtime,
                application_authority,
                git_path=git_path,
                uv_path=uv_path,
                constraints_path=constraints_path,
                environ=environ,
            )
        )


def _verify_mixed_state(
    custom_nodes_root: Path,
    admitted: Sequence[CustomNodePlan],
    future: Sequence[CustomNodePlan],
    *,
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    manager_authority: ParsedManagerRequirements | None,
    has_registry: bool,
    git_path: Path,
    git_environment: Mapping[str, str],
) -> None:
    if manager_authority is not None:
        verify_manager_authority(application, runtime, manager_authority)
    admitted_git_targets: list[Path] = []
    for node in admitted:
        if isinstance(node, GitNodePlan):
            target = _planned_git_target(node, custom_nodes_root)
            _verify_git_provenance(
                node, target, custom_nodes_root, git_path, git_environment
            )
            admitted_git_targets.append(target)
    for node in future:
        if isinstance(node, GitNodePlan):
            _require_absent(
                _planned_git_target(node, custom_nodes_root),
                f"future Git target {Path(node.target).name}",
            )
    if has_registry:
        expected_registry = tuple(
            node for node in admitted if isinstance(node, RegistryNodePlan)
        )
        _verify_registry_set(
            custom_nodes_root,
            expected_registry,
            excluded_git_targets=admitted_git_targets,
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
            raise CustomNodeInstallError(
                f"Registry node {node.id}@{node.version} is not installed"
            )
        try:
            expected_version = Version(node.version)
        except InvalidVersion as error:
            raise CustomNodeInstallError(
                f"Registry node {node.id} has an invalid locked version"
            ) from error
        if identity.parsed_version != expected_version:
            raise CustomNodeInstallError(
                f"Registry node {node.id} version does not match BuildPlan"
            )
    expected_names = {normalized_registry_id(node.id) for node in expected}
    if set(observed) != expected_names:
        raise CustomNodeInstallError(
            "installed Registry identities do not match the admitted declaration prefix"
        )


def _scan_registry_identities(
    custom_nodes_root: Path,
    *,
    excluded_git_targets: Sequence[Path] = (),
) -> dict[str, _ObservedRegistryIdentity]:
    root = _require_real_directory(custom_nodes_root, "custom-nodes root")
    excluded = set(excluded_git_targets)
    if any(path.parent != root for path in excluded):
        raise CustomNodeInstallError("Git exclusion target escapes custom-nodes root")
    observed: dict[str, _ObservedRegistryIdentity] = {}
    try:
        children = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    except OSError as error:
        raise CustomNodeInstallError(
            "custom-nodes root could not be scanned"
        ) from error
    for child in children:
        if child in excluded:
            continue
        try:
            child_metadata = child.lstat()
        except OSError as error:
            raise CustomNodeInstallError(
                "custom-node entry could not be inspected"
            ) from error
        if stat.S_ISLNK(child_metadata.st_mode):
            raise CustomNodeInstallError("custom-node entries must not be symlinks")
        if stat.S_ISREG(child_metadata.st_mode):
            continue
        if not stat.S_ISDIR(child_metadata.st_mode):
            raise CustomNodeInstallError(
                "custom-node entries must be regular files or real directories"
            )
        resolved_child = _require_real_directory(child, "custom-node directory")
        if resolved_child.parent != root:
            raise CustomNodeInstallError(
                "custom-node directory escapes the declared root"
            )
        project_file = child / "pyproject.toml"
        try:
            project_metadata = project_file.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise CustomNodeInstallError(
                "custom-node metadata could not be inspected"
            ) from error
        if stat.S_ISLNK(project_metadata.st_mode) or not stat.S_ISREG(
            project_metadata.st_mode
        ):
            raise CustomNodeInstallError(
                "custom-node metadata must be one regular file"
            )
        try:
            resolved_project = project_file.resolve(strict=True)
            content = project_file.read_bytes()
        except OSError as error:
            raise CustomNodeInstallError(
                "custom-node metadata could not be read"
            ) from error
        if (
            resolved_project.parent != resolved_child
            or not resolved_project.is_relative_to(root)
        ):
            raise CustomNodeInstallError(
                "custom-node metadata escapes the declared root"
            )
        identity = _parse_project_identity(content)
        if identity.normalized_name in observed:
            raise CustomNodeInstallError(
                f"Registry identity {identity.normalized_name} is duplicated"
            )
        observed[identity.normalized_name] = identity
    return observed


def _verify_git_provenance(
    node: GitNodePlan,
    target: Path,
    custom_nodes_root: Path,
    git_path: Path,
    environment: Mapping[str, str],
) -> None:
    root = _require_real_directory(custom_nodes_root, "custom-nodes root")
    expected_target = _planned_git_target(node, root)
    if target != expected_target:
        raise CustomNodeInstallError("Git node target does not match BuildPlan")
    actual = _require_real_directory(target, f"Git node {expected_target.name}")
    if actual.parent != root:
        raise CustomNodeInstallError("Git node target escapes custom-nodes root")
    _verify_repository_root(
        actual,
        actual,
        git_path,
        environment,
        description=f"Git node {expected_target.name}",
    )
    root_git_directory = _verify_root_git_directory(
        actual,
        git_path,
        environment,
        description=f"Git node {expected_target.name}",
    )
    _verify_exact_detached_head(
        actual,
        node.commit,
        root,
        git_path,
        environment,
        description=f"Git node {expected_target.name}",
    )
    _verify_committed_gitlinks(
        actual,
        actual,
        root,
        git_path,
        environment,
        seen={actual},
        root_git_directory=root_git_directory,
        description=f"Git node {expected_target.name}",
    )


def _verify_repository_root(
    repository: Path,
    expected: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> None:
    output = _run_git(
        (
            git_path,
            "-C",
            repository,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
        ),
        cwd=repository,
        env=environment,
        description=f"{description} repository-root verification",
    )
    if output != os.fsencode(expected) + b"\n":
        raise CustomNodeInstallError(
            f"{description} repository root does not match its exact target"
        )


def _verify_root_git_directory(
    repository: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> Path:
    dot_git = repository / ".git"
    git_directory = _require_real_directory(dot_git, f"{description} .git directory")
    if git_directory != dot_git:
        raise CustomNodeInstallError(f"{description} .git directory is not exact")
    actual_git_directory, common_directory = _git_directory_paths(
        repository,
        git_path,
        environment,
        description=description,
    )
    if actual_git_directory != git_directory or common_directory != git_directory:
        raise CustomNodeInstallError(
            f"{description} Git directory escapes its exact root repository"
        )
    return git_directory


def _verify_submodule_git_directory(
    repository: Path,
    root_git_directory: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> None:
    dot_git = repository / ".git"
    try:
        metadata = dot_git.lstat()
    except OSError as error:
        raise CustomNodeInstallError(
            f"{description} .git file is unavailable"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CustomNodeInstallError(
            f"{description} .git must be one regular non-symlink file"
        )
    actual_git_directory, common_directory = _git_directory_paths(
        repository,
        git_path,
        environment,
        description=description,
    )
    if actual_git_directory != common_directory:
        raise CustomNodeInstallError(f"{description} linked worktree is not permitted")
    _require_contained_real_directory(
        actual_git_directory,
        root_git_directory,
        f"{description} Git directory",
    )


def _git_directory_paths(
    repository: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> tuple[Path, Path]:
    git_directory = _single_absolute_git_path(
        _run_git(
            (git_path, "-C", repository, "rev-parse", "--absolute-git-dir"),
            cwd=repository,
            env=environment,
            description=f"{description} Git-directory verification",
        ),
        f"{description} Git directory",
    )
    common_directory = _single_absolute_git_path(
        _run_git(
            (
                git_path,
                "-C",
                repository,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ),
            cwd=repository,
            env=environment,
            description=f"{description} common-Git-directory verification",
        ),
        f"{description} common Git directory",
    )
    return git_directory, common_directory


def _single_absolute_git_path(output: bytes, subject: str) -> Path:
    if not output.endswith(b"\n") or output.count(b"\n") != 1:
        raise CustomNodeInstallError(f"{subject} output is ambiguous")
    path = Path(os.fsdecode(output[:-1]))
    if not path.is_absolute():
        raise CustomNodeInstallError(f"{subject} is not absolute")
    return path


def _require_contained_real_directory(path: Path, root: Path, subject: str) -> Path:
    root = _require_real_directory(root, "root Git directory")
    if path == root:
        raise CustomNodeInstallError(f"{subject} replaces the root Git directory")
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise CustomNodeInstallError(
            f"{subject} escapes root Git management"
        ) from error
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise CustomNodeInstallError(f"{subject} is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise CustomNodeInstallError(f"{subject} contains a symlink")
    return _require_real_directory(path, subject)


def _verify_exact_detached_head(
    repository: Path,
    expected_commit: str,
    cwd: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> None:
    _run_git(
        (git_path, "-C", repository, "cat-file", "-e", f"{expected_commit}^{{commit}}"),
        cwd=cwd,
        env=environment,
        description=f"{description} locked commit object verification",
    )
    head = _run_git(
        (git_path, "-C", repository, "rev-parse", "--verify", "HEAD"),
        cwd=cwd,
        env=environment,
        description=f"{description} commit verification",
    )
    if head != f"{expected_commit}\n".encode("ascii"):
        raise CustomNodeInstallError(f"{description} commit does not match gitlink")
    symbolic = _run_git_allowing(
        (git_path, "-C", repository, "symbolic-ref", "-q", "HEAD"),
        cwd=cwd,
        env=environment,
        description=f"{description} detached HEAD verification",
        allowed_returncodes=(0, 1),
    )
    if symbolic.returncode != 1:
        raise CustomNodeInstallError(f"{description} HEAD must be detached")


def _verify_committed_gitlinks(
    repository: Path,
    repository_root: Path,
    custom_nodes_root: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    seen: set[Path],
    root_git_directory: Path,
    description: str,
) -> None:
    tree = _run_git(
        (git_path, "-C", repository, "ls-tree", "-rz", "--full-tree", "HEAD"),
        cwd=custom_nodes_root,
        env=environment,
        description=f"{description} committed gitlink enumeration",
    )
    for record in tree.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, raw_commit = header.split(b" ", 2)
        except ValueError as error:
            raise CustomNodeInstallError(
                f"{description} committed tree output is invalid"
            ) from error
        if mode != _GITLINK_MODE:
            continue
        if (
            object_type != b"commit"
            or _COMMIT_PATTERN.fullmatch(raw_commit.decode("ascii", errors="ignore"))
            is None
        ):
            raise CustomNodeInstallError(f"{description} gitlink entry is invalid")
        child_relative = _safe_gitlink_path(raw_path, description)
        child = repository.joinpath(*child_relative.parts)
        actual_child = _require_real_directory(child, f"{description} submodule")
        if (
            actual_child == repository
            or not actual_child.is_relative_to(repository_root)
            or not actual_child.is_relative_to(custom_nodes_root)
            or actual_child in seen
        ):
            raise CustomNodeInstallError(f"{description} submodule path is unsafe")
        seen.add(actual_child)
        child_description = f"{description} submodule {child_relative.as_posix()}"
        _verify_submodule_git_directory(
            actual_child,
            root_git_directory,
            git_path,
            environment,
            description=child_description,
        )
        _verify_repository_root(
            actual_child,
            actual_child,
            git_path,
            environment,
            description=child_description,
        )
        expected_commit = raw_commit.decode("ascii")
        _verify_exact_detached_head(
            actual_child,
            expected_commit,
            custom_nodes_root,
            git_path,
            environment,
            description=child_description,
        )
        _verify_committed_gitlinks(
            actual_child,
            repository_root,
            custom_nodes_root,
            git_path,
            environment,
            seen=seen,
            root_git_directory=root_git_directory,
            description=child_description,
        )


def _safe_gitlink_path(raw_path: bytes, description: str) -> PurePosixPath:
    value = os.fsdecode(raw_path)
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CustomNodeInstallError(f"{description} gitlink path is unsafe")
    return path


def _planned_git_target(node: GitNodePlan, custom_nodes_root: Path) -> Path:
    target = Path(node.target)
    if (
        not target.is_absolute()
        or target.parent != custom_nodes_root
        or not is_safe_git_target_dir(target.name)
    ):
        raise CustomNodeInstallError(
            "Git target does not match the safe BuildPlan path"
        )
    return target


def _optional_root_file(root: Path, name: str) -> Path | None:
    path = root / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CustomNodeInstallError(
            f"Git node root {name} could not be inspected"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CustomNodeInstallError(f"Git node root {name} must be one regular file")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CustomNodeInstallError(
            f"Git node root {name} could not be resolved"
        ) from error
    if resolved.parent != root:
        raise CustomNodeInstallError(f"Git node root {name} escapes its repository")
    return path


def _managed_python_environment(
    runtime: ContainerRuntime,
    python_index_url: str,
    pytorch_index_url: str,
    constraints_path: Path,
    build_constraints_path: Path,
    environ: Mapping[str, str] | None,
) -> dict[str, str]:
    environment = application_install_environment(environ)
    environment.update(
        {
            "COMFYUI_PATH": os.fspath(runtime.comfyui_path),
            "PIP_CONSTRAINT": os.fspath(constraints_path),
            "PIP_BUILD_CONSTRAINT": os.fspath(build_constraints_path),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_EXTRA_INDEX_URL": pytorch_index_url,
            "PIP_INDEX_URL": python_index_url,
            "UV_CONSTRAINT": os.fspath(constraints_path),
            "UV_BUILD_CONSTRAINT": os.fspath(build_constraints_path),
            "UV_DEFAULT_INDEX": python_index_url,
            "UV_INDEX": pytorch_index_url,
            "UV_INDEX_STRATEGY": "unsafe-best-match",
            "UV_NO_CONFIG": "1",
            "VIRTUAL_ENV": os.fspath(runtime.virtual_env),
            "WORKSPACE": os.fspath(runtime.workspace),
            "PATH": f"{runtime.virtual_env}/bin:/usr/local/bin:/usr/bin:/bin",
        }
    )
    return environment


@contextmanager
def _temporary_build_constraints(
    group: PyTorchGroupPlan,
    *,
    directory: Path | None = None,
) -> Iterator[Path]:
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".python-build-constraints-",
            suffix=".txt",
            dir=directory,
        )
    except OSError as error:
        raise CustomNodeInstallError(
            "managed build constraints could not be materialized"
        ) from error
    path = Path(name)
    try:
        try:
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            stream.write(managed_build_constraints_bytes(group))
        path.chmod(0o444)
        yield path
    finally:
        path.unlink(missing_ok=True)


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
        raise CustomNodeInstallError(
            "custom-node pyproject.toml has invalid project identity"
        ) from error
    return _ObservedRegistryIdentity(
        name=name,
        normalized_name=normalized_name,
        version=version,
        parsed_version=parsed_version,
    )


def _require_real_directory(path: Path, subject: str) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CustomNodeInstallError(f"{subject} is unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path
    ):
        raise CustomNodeInstallError(f"{subject} must be one real directory")
    return resolved


def _require_absent(path: Path, subject: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise CustomNodeInstallError(f"{subject} could not be inspected") from error
    raise CustomNodeInstallError(f"{subject} already exists")


def _run_git(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: Mapping[str, str],
    description: str,
) -> bytes:
    completed = _run_git_allowing(
        argv,
        cwd=cwd,
        env=env,
        description=description,
        allowed_returncodes=(0,),
    )
    return completed.stdout


def _run_git_allowing(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: Mapping[str, str],
    description: str,
    allowed_returncodes: Sequence[int],
) -> subprocess.CompletedProcess[bytes]:
    command = [os.fspath(item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env),
            check=False,
            stdout=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CustomNodeInstallError(f"{description} failed to start") from error
    if completed.returncode not in allowed_returncodes:
        raise CustomNodeInstallError(
            f"{description} failed with exit code {completed.returncode}"
        )
    return completed
