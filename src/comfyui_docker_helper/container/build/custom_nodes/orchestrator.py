"""Ordered custom-node orchestration and final-state proof."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.cli_output.events import EventSink
from comfyui_docker_helper.config.authored.validation.domains import is_git_source_url
from comfyui_docker_helper.config.credentials.process_policy import (
    GitCredentialPolicyError,
    git_credential_environment,
)
from comfyui_docker_helper.config.evidence.custom_nodes import (
    CustomNodeInventory,
    custom_node_inventory,
)
from comfyui_docker_helper.config.planning.build_plan import (
    ApplicationPhase,
    CustomNodePlan,
    CustomNodesPhase,
    GitNodePlan,
    PyTorchGroupPlan,
    RegistryNodePlan,
    managed_build_constraints_bytes,
)
from comfyui_docker_helper.config.planning.requirements import (
    ParsedComfyUIRequirements,
    ParsedManagerRequirements,
)
from comfyui_docker_helper.config.validation.registry import (
    validate_registry_node_authority,
)
from comfyui_docker_helper.container.build.application import (
    application_build_environment,
)
from comfyui_docker_helper.container.build.comfyui import (
    capture_application_requirements,
    capture_manager_authority,
    observe_application_state,
    observe_manager_absence,
    observe_manager_capability,
    verify_manager_authority,
)
from comfyui_docker_helper.container.build.custom_nodes import contracts, git, registry
from comfyui_docker_helper.container.build.events import (
    ContainerHelperEvent,
    ContainerHelperPhase,
    ContainerHelperPhaseCompleted,
    ContainerHelperPhaseStarted,
    CustomNodeCompleted,
    CustomNodesInstallCompleted,
    GitCustomNodeStarted,
    RegistryCustomNodeStarted,
)
from comfyui_docker_helper.container.build.git_credential_helper import (
    GIT_CREDENTIAL_BUILD_PLAN_DIGEST_ENV,
)
from comfyui_docker_helper.container.process.runners import ContainerRuntime, run_hook

_GIT_PATH = Path("/usr/bin/git")
_UV_PATH = Path("/usr/local/bin/uv")
_BUILD_DIRECTORY = Path("/opt/cdh/build")
_CONSTRAINTS_PATH = _BUILD_DIRECTORY / "python-package-constraints.txt"
_BUILD_HOOKS_DIRECTORY = _BUILD_DIRECTORY / "hooks"


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
    event_sink: EventSink[ContainerHelperEvent] | None = None,
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
            event_sink=event_sink,
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
    event_sink: EventSink[ContainerHelperEvent] | None,
) -> None:
    """Execute one validated custom-node sequence."""

    with _helper_phase(event_sink, ContainerHelperPhase.CUSTOM_NODES_PREPARATION):
        nodes = custom_nodes.nodes
        has_registry = any(isinstance(node, RegistryNodePlan) for node in nodes)
        manager_authority: ParsedManagerRequirements | None = None
        if nodes:
            if application.comfyui.manager is None:
                observe_manager_absence(application, runtime)
            else:
                manager_authority = capture_manager_authority(application, runtime)
        custom_nodes_root = contracts._require_real_directory(
            runtime.comfyui_path / "custom_nodes", "custom-nodes root"
        )
        application_authority = capture_application_requirements(application, runtime)
        custom_node_python_environment = _managed_python_environment(
            application,
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
        observations = _VerificationObservations.initial(
            has_manager_observer=bool(nodes)
        )

    for index, node in enumerate(nodes):
        position = index + 1
        _emit_custom_node_started(
            event_sink,
            node,
            index=position,
            total=len(nodes),
        )
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
        with _optional_helper_phase(
            event_sink,
            ContainerHelperPhase.CUSTOM_NODE_PRE_INSTALL,
            enabled=bool(node.pre_install_hooks),
        ):
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

        with _helper_phase(event_sink, ContainerHelperPhase.CUSTOM_NODE_INSTALLATION):
            observations.invalidate_mutation()
            if isinstance(node, RegistryNodePlan):
                registry._install_registry_node(
                    node,
                    custom_nodes,
                    application,
                    runtime,
                    manager_authority,
                    custom_node_python_environment,
                )
            else:
                git._install_git_node(
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

        with _optional_helper_phase(
            event_sink,
            ContainerHelperPhase.CUSTOM_NODE_POST_INSTALL,
            enabled=bool(node.post_install_hooks),
        ):
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
        _emit_helper_event(
            event_sink,
            CustomNodeCompleted(index=position, total=len(nodes)),
        )

    with _helper_phase(
        event_sink, ContainerHelperPhase.CUSTOM_NODES_FINAL_VERIFICATION
    ):
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
    _emit_helper_event(event_sink, CustomNodesInstallCompleted(node_count=len(nodes)))


@contextmanager
def _helper_phase(
    event_sink: EventSink[ContainerHelperEvent] | None,
    phase: ContainerHelperPhase,
) -> Iterator[None]:
    _emit_helper_event(event_sink, ContainerHelperPhaseStarted(phase))
    yield
    _emit_helper_event(event_sink, ContainerHelperPhaseCompleted(phase))


@contextmanager
def _optional_helper_phase(
    event_sink: EventSink[ContainerHelperEvent] | None,
    phase: ContainerHelperPhase,
    *,
    enabled: bool,
) -> Iterator[None]:
    if not enabled:
        yield
        return
    with _helper_phase(event_sink, phase):
        yield


def _emit_custom_node_started(
    event_sink: EventSink[ContainerHelperEvent] | None,
    node: CustomNodePlan,
    *,
    index: int,
    total: int,
) -> None:
    pre_hook_count = len(node.pre_install_hooks)
    post_hook_count = len(node.post_install_hooks)
    if isinstance(node, RegistryNodePlan):
        event: ContainerHelperEvent = RegistryCustomNodeStarted(
            index=index,
            total=total,
            id=node.id,
            version=node.version,
            pre_hook_count=pre_hook_count,
            post_hook_count=post_hook_count,
        )
    else:
        event = GitCustomNodeStarted(
            index=index,
            total=total,
            target_name=Path(node.target).name,
            pre_hook_count=pre_hook_count,
            post_hook_count=post_hook_count,
        )
    _emit_helper_event(event_sink, event)


def _emit_helper_event(
    event_sink: EventSink[ContainerHelperEvent] | None,
    event: ContainerHelperEvent,
) -> None:
    if event_sink is not None:
        event_sink.emit(event)


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
        raise contracts.CustomNodeInstallError(
            "Git credential BuildPlan identity is unavailable"
        )
    helper = (
        f"!exec {shlex.quote(sys.executable)} "
        "-m comfyui_docker_helper.container.build.git_credential_helper"
    )
    try:
        return git_credential_environment(
            environment,
            helper=helper,
            overlay={GIT_CREDENTIAL_BUILD_PLAN_DIGEST_ENV: build_plan_digest},
        )
    except GitCredentialPolicyError:
        raise contracts.CustomNodeInstallError(
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

    custom_nodes_root = contracts._require_real_directory(
        runtime.comfyui_path / "custom_nodes", "custom-nodes root"
    )
    git_environment = runtime.env(environ)
    git_targets: list[Path] = []
    for node in custom_nodes.nodes:
        if not isinstance(node, GitNodePlan):
            continue
        target = git._planned_git_target(node, custom_nodes_root)
        git._verify_git_provenance(
            node,
            target,
            custom_nodes_root,
            git_path,
            git_environment,
        )
        git_targets.append(target)
    registry._verify_registry_set(
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
        raise contracts.CustomNodeInstallError(
            "custom-node Manager state does not match application phase"
        )
    if runtime.workspace != Path(application.paths.workspace):
        raise contracts.CustomNodeInstallError(
            "custom-node workspace does not match BuildPlan"
        )
    if runtime.comfyui_path != Path(application.paths.comfyui):
        raise contracts.CustomNodeInstallError(
            "custom-node ComfyUI path does not match BuildPlan"
        )
    if runtime.virtual_env != Path(application.paths.venv):
        raise contracts.CustomNodeInstallError(
            "custom-node venv does not match BuildPlan"
        )
    if Path(custom_nodes.user_directory) != runtime.comfyui_path / "user":
        raise contracts.CustomNodeInstallError(
            "Registry user directory does not match BuildPlan"
        )
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
        raise contracts.CustomNodeInstallError(message) from error
    for node in custom_nodes.nodes:
        if isinstance(node, RegistryNodePlan):
            try:
                Version(node.version)
            except InvalidVersion as error:
                raise contracts.CustomNodeInstallError(
                    f"Registry node {node.id} has an invalid locked version"
                ) from error
        else:
            if git._COMMIT_PATTERN.fullmatch(node.commit) is None:
                raise contracts.CustomNodeInstallError(
                    "Git node commit must be exact 40-hex"
                )
            if not is_git_source_url(node.url):
                raise contracts.CustomNodeInstallError("Git node URL is invalid")
            target = git._planned_git_target(node, custom_root)
            if target in git_targets:
                raise contracts.CustomNodeInstallError(
                    f"Git target {target.name} is duplicated in BuildPlan"
                )
            git_targets.add(target)


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
            target = git._planned_git_target(node, custom_nodes_root)
            git._verify_git_provenance(
                node, target, custom_nodes_root, git_path, git_environment
            )
            admitted_git_targets.append(target)
    for node in future:
        if isinstance(node, GitNodePlan):
            _require_absent(
                git._planned_git_target(node, custom_nodes_root),
                f"future Git target {Path(node.target).name}",
            )
    if has_registry:
        expected_registry = tuple(
            node for node in admitted if isinstance(node, RegistryNodePlan)
        )
        registry._verify_registry_set(
            custom_nodes_root,
            expected_registry,
            excluded_git_targets=admitted_git_targets,
        )


def _managed_python_environment(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    python_index_url: str,
    pytorch_index_url: str,
    constraints_path: Path,
    build_constraints_path: Path,
    environ: Mapping[str, str] | None,
) -> dict[str, str]:
    environment = application_build_environment(
        application,
        environ,
        constraints_path=constraints_path,
        comfyui_path=runtime.comfyui_path,
        virtual_env=runtime.virtual_env,
    )
    build_path = environment["PATH"]
    environment.update(
        {
            "PIP_BUILD_CONSTRAINT": os.fspath(build_constraints_path),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_EXTRA_INDEX_URL": pytorch_index_url,
            "PIP_INDEX_URL": python_index_url,
            "UV_BUILD_CONSTRAINT": os.fspath(build_constraints_path),
            "UV_DEFAULT_INDEX": python_index_url,
            "UV_INDEX": pytorch_index_url,
            "UV_INDEX_STRATEGY": "unsafe-best-match",
            "UV_NO_CONFIG": "1",
            "WORKSPACE": os.fspath(runtime.workspace),
            "PATH": f"{runtime.virtual_env}/bin:/usr/local/bin:{build_path}",
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
        raise contracts.CustomNodeInstallError(
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


def _require_absent(path: Path, subject: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise contracts.CustomNodeInstallError(
            f"{subject} could not be inspected"
        ) from error
    raise contracts.CustomNodeInstallError(f"{subject} already exists")
