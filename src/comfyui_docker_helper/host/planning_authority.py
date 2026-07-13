"""Active final-config to canonical desired-resolution integration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import httpx

from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    DirectGitRequestIdentity,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    ManagedPythonRequestIdentity,
    OciLockEntry,
    OciRequestIdentity,
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
    PyPIComfyCliIdentityProvider,
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
            comfy_cli=PyPIComfyCliIdentityProvider(client),
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
    runtime_hook_requests: tuple[LocalExecutableIdentityRequest, ...] = (),
) -> DesiredPlanningInputs:
    platform = TargetPlatform(config.build.platforms[0])
    backend = CudaBackendAdapter().derive(
        CudaVersion.from_validated(config.compute_platform.cuda.version), platform
    )
    repository, tag = backend.base_image.split(":", 1)
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
        ComfyCliRequestIdentity(
            type="comfy-cli",
            package="comfy-cli",
            selector=config.comfyui.cli_version,
            index_url=config.python.index_url,
            python_version=config.python.version,
            platform=platform.value,
        ),
    ]
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
            members=[
                DirectPythonRequestMember(
                    package="torch", extras=[], selector=f"=={config.pytorch.version}"
                ),
                *_members(domains, "pytorch"),
            ],
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
