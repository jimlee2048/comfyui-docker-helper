"""Active final-config to canonical desired-resolution integration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import httpx

from comfyui_docker_helper.comfyui_requirements import (
    COMFYUI_REQUIREMENTS_PATH,
    CUDA_PROTECTED_REQUIREMENTS,
    ComfyUIRequirementsError,
    merge_pytorch_requirements,
    protected_policy_digest,
)
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectGitRequestIdentity,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    ManagedPythonRequestIdentity,
    OciLockEntry,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    ProtectedRequirementProjection,
    PyTorchRequestIdentity,
    RegistryRequestIdentity,
    ResolverRequestIdentity,
    canonical_entry_key,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_resolver import (
    AcquiredCanonicalEntries,
    CanonicalAcquisitionError,
    CanonicalEntryAcquirer,
    CanonicalResolutionError,
    DesiredResolution,
    LockPolicy,
    ManagedPythonReleaseInputs,
    entries_satisfy_request,
)
from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_planning import (
    CudaBackendAdapter,
    CudaVersion,
    TargetPlatform,
)
from comfyui_docker_helper.config.final_validation import FinalConfigDomainResult
from comfyui_docker_helper.config.runtime_hooks import CUSTOM_NODE_HOOK_LOCK_PREFIX
from comfyui_docker_helper.exact_ledger import (
    CDH_VERSION,
    COMFY_CLI_MINIMUM_VERSION,
    COMFYUI_FLOOR_COMMIT,
    COMFYUI_REPOSITORY,
    PIP_VERSION,
    UV_IMAGE_REPOSITORY,
    UV_VERSION,
)
from comfyui_docker_helper.host.canonical_acquisition import (
    LocalExecutableEntryAcquirer,
    ProviderIdentityAcquirer,
    UvPythonGroupResolver,
)
from comfyui_docker_helper.host.identity_providers import (
    FilesystemLocalExecutableIdentityProvider,
    GitDirectIdentityProvider,
    GitOfficialComfyUIIdentityProvider,
    HttpOciIdentityProvider,
    HttpRegistryNodeIdentityProvider,
    LocalExecutableIdentityRequest,
    UvManagedPythonIdentityProvider,
)
from comfyui_docker_helper.host.uv_runner import locate_host_uv
from comfyui_docker_helper.release_artifacts import release_source_digest


@dataclass(frozen=True, slots=True)
class DesiredPlanningInputs:
    desired: tuple[DesiredResolution, ...]
    local_requests: tuple[LocalExecutableIdentityRequest, ...]


@dataclass(slots=True)
class CachingCanonicalAcquirer:
    """Avoid duplicate acquisition when uv catalog identity is needed first."""

    delegate: CanonicalEntryAcquirer
    cache: dict[tuple[str, str], AcquiredCanonicalEntries] = field(default_factory=dict)

    def acquire(
        self, request: ResolverRequestIdentity, request_digest: str
    ) -> AcquiredCanonicalEntries:
        key = (request.model_dump_json(), request_digest)
        if key not in self.cache:
            self.cache[key] = self.delegate.acquire(request, request_digest)
        return self.cache[key]


@dataclass(frozen=True, slots=True)
class DefaultPlanningProviders:
    acquirer: CachingCanonicalAcquirer
    local_acquirer: LocalExecutableEntryAcquirer


@contextmanager
def default_planning_providers() -> Iterator[DefaultPlanningProviders]:
    """Create concrete final providers only for render/build, never validate."""
    uv = locate_host_uv()
    release = managed_python_release_inputs()
    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        provider = ProviderIdentityAcquirer(
            oci=HttpOciIdentityProvider(client),
            managed_python=UvManagedPythonIdentityProvider(uv),
            comfyui=GitOfficialComfyUIIdentityProvider(),
            registry=HttpRegistryNodeIdentityProvider(client),
            git=GitDirectIdentityProvider(),
            python_group=UvPythonGroupResolver(uv),
            release=release,
        )
        yield DefaultPlanningProviders(
            CachingCanonicalAcquirer(provider),
            LocalExecutableEntryAcquirer(FilesystemLocalExecutableIdentityProvider()),
        )


def uv_catalog_descriptor_digest(
    config: FinalConfig,
    existing: CanonicalLock | None,
    policy: LockPolicy,
    acquirer: CachingCanonicalAcquirer,
) -> str:
    """Obtain the uv descriptor needed to bind the managed-Python catalog."""
    request = uv_oci_request(config)
    digest = compute_request_digest(request)
    if policy is not LockPolicy.UPGRADE and existing is not None:
        for entry in existing.entries:
            if (
                isinstance(entry, OciLockEntry)
                and canonical_entry_key(entry) == ("oci", "uv-tool")
                and entries_satisfy_request(request, (entry,), digest)
            ):
                return entry.descriptor_digest
    if policy is LockPolicy.LOCKED:
        return f"sha256:{'0' * 64}"
    try:
        acquired = acquirer.acquire(request, digest)
    except CanonicalAcquisitionError as error:
        raise CanonicalResolutionError(
            (
                Diagnostic(
                    path=("config.lock.toml", "oci", "uv-tool"),
                    code="lock.resolve_failed",
                    message=str(error),
                ),
            )
        ) from error
    if not entries_satisfy_request(request, acquired.entries, digest):
        raise ValueError("uv provider returned an incompatible descriptor")
    entry = acquired.entries[0]
    if not isinstance(entry, OciLockEntry):  # pragma: no cover - proven above
        raise AssertionError("compatible uv OCI result must be an OCI entry")
    return entry.descriptor_digest


def build_desired_planning_inputs(
    config: FinalConfig,
    domains: FinalConfigDomainResult,
    *,
    scripts_dir: str | Path,
    uv_descriptor_digest: str,
    comfyui_entry: OfficialComfyUILockEntry,
    requirements_entry: ComfyUIRequirementsLockEntry,
    runtime_hook_requests: tuple[LocalExecutableIdentityRequest, ...] = (),
) -> DesiredPlanningInputs:
    platform = TargetPlatform(config.build.platforms[0])
    backend = CudaBackendAdapter().derive(
        CudaVersion.from_validated(config.compute_platform.cuda.version), platform
    )
    repository, tag = backend.base_image.split(":", 1)
    requirements_request = comfyui_requirements_request(config, comfyui_entry)
    if not entries_satisfy_request(
        requirements_request,
        (requirements_entry,),
        compute_request_digest(requirements_request),
    ):
        raise ValueError("ComfyUI requirements identity does not match final config")
    requests: list[ResolverRequestIdentity] = [
        OciRequestIdentity(
            type="oci",
            role="cuda-base",
            repository=repository,
            tag=tag,
            platform=platform.value,
        ),
        uv_oci_request(config),
        ManagedPythonRequestIdentity(
            type="managed-python",
            version=config.python.version,
            implementation="cpython",
            platform=platform.value,
            libc="gnu",
            catalog_descriptor_digest=uv_descriptor_digest,
        ),
        ComfyUIRequestIdentity(
            type="comfyui",
            repository=COMFYUI_REPOSITORY,
            selector=config.comfyui.version,
        ),
        requirements_request,
    ]
    if config.comfyui.install_cli:
        requests.append(
            ComfyCliRequestIdentity(
                type="comfy-cli",
                package="comfy-cli",
                policy="highest-target-compatible-stable",
                minimum_version=COMFY_CLI_MINIMUM_VERSION,
                environment="uv-tool:comfy-cli",
                index_url=config.python.index_url,
                python_version=config.python.version,
                platform=platform.value,
            )
        )
    python_members = _members(domains, "python")
    if python_members:
        requests.append(
            DirectPythonRequestIdentity(
                type="python-group",
                environment="application",
                group="application-extra",
                python_version=config.python.version,
                platform=platform.value,
                index_url=config.python.index_url,
                members=python_members,
            )
        )
    for member in _members(domains, "python", field="uv_tools"):
        requests.append(
            DirectPythonRequestIdentity(
                type="python-group",
                environment=f"uv-tool:{member.package}",
                group="uv-tool",
                python_version=config.python.version,
                platform=platform.value,
                index_url=config.python.index_url,
                members=[member],
            )
        )
    upstream = tuple(
        DirectPythonRequestMember(
            package=item.package,
            extras=item.extras,
            selector=item.selector,
        )
        for item in requirements_entry.protected
    )
    try:
        pytorch_members = merge_pytorch_requirements(
            DirectPythonRequestMember(
                package="torch", extras=[], selector=f"=={config.pytorch.version}"
            ),
            upstream,
            tuple(_members(domains, "pytorch")),
        )
    except ComfyUIRequirementsError as error:
        raise CanonicalResolutionError(
            (
                Diagnostic(
                    path=("config.lock.toml", "pytorch-group"),
                    code="lock.protected_requirement_conflict",
                    message=str(error),
                ),
            )
        ) from error
    requests.append(
        PyTorchRequestIdentity(
            type="pytorch-group",
            environment="application",
            group="pytorch",
            backend="cuda",
            channel=backend.package_channel,
            python_version=config.python.version,
            platform=platform.value,
            python_index_url=config.python.index_url,
            pytorch_index_url=(
                f"{config.pytorch.index_base_url.rstrip('/')}/{backend.package_channel}"
            ),
            upstream_protected=[
                ProtectedRequirementProjection(
                    package=item.package,
                    extras=item.extras,
                    selector=item.selector,
                )
                for item in upstream
            ],
            members=list(pytorch_members),
        )
    )
    for node in config.comfyui.custom_nodes:
        if node.type == "registry":
            requests.append(
                RegistryRequestIdentity(
                    type="registry", id=node.id, selector=node.version or "latest"
                )
            )
        else:
            requests.append(
                DirectGitRequestIdentity(
                    type="git", url=node.url, ref=node.ref or "HEAD"
                )
            )
    release = managed_python_release_inputs()
    desired = tuple(
        DesiredResolution.from_request(
            request,
            managed_python_release=(
                release if isinstance(request, ManagedPythonRequestIdentity) else None
            ),
        )
        for request in requests
    )
    root = Path(scripts_dir).resolve()
    relative_hooks = sorted(
        {
            hook
            for node in config.comfyui.custom_nodes
            for hook in (*node.pre_install_scripts, *node.post_install_scripts)
        }
    )
    local = (
        tuple(
            LocalExecutableIdentityRequest(
                root,
                PurePosixPath(path),
                PurePosixPath(CUSTOM_NODE_HOOK_LOCK_PREFIX) / path,
            )
            for path in relative_hooks
        )
        + runtime_hook_requests
    )
    return DesiredPlanningInputs(desired, local)


def stable_comfyui_entry(
    config: FinalConfig,
    existing: CanonicalLock | None,
    policy: LockPolicy,
    acquirer: CachingCanonicalAcquirer,
) -> OfficialComfyUILockEntry:
    """Stabilize the exact source identity needed by downstream requests."""
    request = ComfyUIRequestIdentity(
        type="comfyui", repository=COMFYUI_REPOSITORY, selector=config.comfyui.version
    )
    digest = compute_request_digest(request)
    current = _existing_entry(existing, ("comfyui", COMFYUI_REPOSITORY))
    moving = (
        not (
            len(request.selector) == 40
            and all(character in "0123456789abcdef" for character in request.selector)
        )
        and not request.selector[0].isdigit()
    )
    if (
        (policy is not LockPolicy.UPGRADE or not moving)
        and isinstance(current, OfficialComfyUILockEntry)
        and entries_satisfy_request(request, (current,), digest)
    ):
        return current
    return _acquire_stable_entry(
        request,
        digest,
        policy,
        acquirer,
        OfficialComfyUILockEntry,
        ("comfyui", COMFYUI_REPOSITORY),
    )


def stable_comfyui_requirements_entry(
    config: FinalConfig,
    comfyui: OfficialComfyUILockEntry,
    existing: CanonicalLock | None,
    policy: LockPolicy,
    acquirer: CachingCanonicalAcquirer,
) -> ComfyUIRequirementsLockEntry:
    """Stabilize the exact protected projection needed by the PyTorch request."""
    request = comfyui_requirements_request(config, comfyui)
    digest = compute_request_digest(request)
    key = ("comfyui-requirements", COMFYUI_REPOSITORY)
    current = _existing_entry(existing, key)
    if (
        policy is not LockPolicy.UPGRADE
        and isinstance(current, ComfyUIRequirementsLockEntry)
        and entries_satisfy_request(request, (current,), digest)
    ):
        return current
    return _acquire_stable_entry(
        request,
        digest,
        policy,
        acquirer,
        ComfyUIRequirementsLockEntry,
        key,
    )


def comfyui_requirements_request(
    config: FinalConfig,
    comfyui: OfficialComfyUILockEntry,
) -> ComfyUIRequirementsRequestIdentity:
    names = tuple(sorted(CUDA_PROTECTED_REQUIREMENTS))
    return ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository=comfyui.repository,
        commit=comfyui.commit,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path=COMFYUI_REQUIREMENTS_PATH,
        python_version=config.python.version,
        platform=config.build.platforms[0],
        protected_names=list(names),
        protected_policy_digest=protected_policy_digest(names),
    )


def _existing_entry(lock: CanonicalLock | None, key: tuple[str, ...]):
    if lock is None:
        return None
    return next(
        (entry for entry in lock.entries if canonical_entry_key(entry) == key), None
    )


def _acquire_stable_entry(
    request: ResolverRequestIdentity,
    digest: str,
    policy: LockPolicy,
    acquirer: CachingCanonicalAcquirer,
    expected_type,
    key: tuple[str, ...],
):
    if policy is LockPolicy.LOCKED:
        raise CanonicalResolutionError(
            (
                Diagnostic(
                    path=("config.lock.toml", *key),
                    code="lock.locked_mismatch",
                    message=(
                        "locked identity is missing or changed; "
                        "regenerate config.lock.toml"
                    ),
                ),
            )
        )
    try:
        acquired = acquirer.acquire(request, digest)
    except CanonicalAcquisitionError as error:
        raise CanonicalResolutionError(
            (
                Diagnostic(
                    path=("config.lock.toml", *key),
                    code="lock.resolve_failed",
                    message=str(error),
                ),
            )
        ) from error
    if len(acquired.entries) != 1 or not isinstance(acquired.entries[0], expected_type):
        raise ValueError("provider returned an incompatible staged identity")
    if not entries_satisfy_request(request, acquired.entries, digest):
        raise ValueError("provider returned an incompatible staged identity")
    return acquired.entries[0]


def uv_oci_request(config: FinalConfig) -> OciRequestIdentity:
    return OciRequestIdentity(
        type="oci",
        role="uv-tool",
        repository=UV_IMAGE_REPOSITORY,
        tag=config.python.uv_version,
        platform="linux/amd64",
    )


def managed_python_release_inputs() -> ManagedPythonReleaseInputs:
    return ManagedPythonReleaseInputs(
        pip_version=PIP_VERSION,
        cdh_version=CDH_VERSION,
        cdh_source_digest=release_source_digest(),
        uv_build_version=UV_VERSION,
    )


def _members(
    domains: FinalConfigDomainResult,
    group: str,
    *,
    field: str = "extra_packages",
) -> list[DirectPythonRequestMember]:
    return [
        DirectPythonRequestMember(
            package=item.name,
            extras=list(item.extras),
            selector=item.specifier,
        )
        for item in domains.package_requirements
        if item.path[:2] == (group, field)
    ]
