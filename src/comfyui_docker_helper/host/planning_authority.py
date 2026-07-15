"""Validated configuration to canonical request integration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import httpx

from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    OciLockEntry,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    ResolverRequestIdentity,
    canonical_entry_key,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_request import (
    CanonicalRequestGraph,
    ManagedPythonReleaseInputs,
    PlanningReleaseInputs,
    comfyui_requirements_request,
)
from comfyui_docker_helper.config.canonical_resolver import (
    AcquiredCanonicalEntries,
    CanonicalAcquisitionError,
    CanonicalEntryAcquirer,
    CanonicalResolutionError,
    LockPolicy,
    entries_satisfy_request,
)
from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.hook_validation import hook_lock_identity
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
    UvManagedPythonIdentityProvider,
)
from comfyui_docker_helper.host.uv_runner import locate_host_uv
from comfyui_docker_helper.release_artifacts import (
    production_inventory,
    production_requirements_digest,
    release_source_digest,
)


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


def build_local_executable_requests(
    graph: CanonicalRequestGraph,
    *,
    scripts_dir: str | Path,
    runtime_hook_requests: tuple[LocalExecutableIdentityRequest, ...] = (),
) -> tuple[LocalExecutableIdentityRequest, ...]:
    root = Path(scripts_dir).resolve()
    relative_hooks = sorted(
        {
            hook
            for node in graph.custom_nodes
            for hook in (*node.pre_install, *node.post_install)
        }
    )
    return (
        tuple(
            LocalExecutableIdentityRequest(
                root,
                PurePosixPath(path),
                PurePosixPath(hook_lock_identity("custom", path)),
            )
            for path in relative_hooks
        )
        + runtime_hook_requests
    )


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


def planning_release_inputs(python_version: str) -> PlanningReleaseInputs:
    """Collect exact release-owned request and toolchain artifacts once."""
    managed = managed_python_release_inputs()
    return PlanningReleaseInputs(
        managed_python=managed,
        requirements_digest=production_requirements_digest(),
        cdh_closure=production_inventory(python_version),
    )
