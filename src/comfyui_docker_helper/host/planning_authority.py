"""Validated configuration to canonical request integration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import httpx

from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    ComfyUIRequirementsLockEntry,
    OfficialComfyUILockEntry,
    ResolverRequestIdentity,
    UvImageLockEntry,
    canonical_entry_key,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_request import (
    CanonicalRequestGraph,
    PlanningReleaseInputs,
    SelectorStability,
    comfyui_request,
    comfyui_requirements_request,
    request_stability,
    uv_oci_request,
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
    PIP_VERSION,
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
from comfyui_docker_helper.host.release_wheel import build_canonical_wheel
from comfyui_docker_helper.host.uv_runner import locate_host_uv
from comfyui_docker_helper.release_artifacts import CanonicalWheel


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
    canonical_wheel: CanonicalWheel


@contextmanager
def default_planning_providers() -> Iterator[DefaultPlanningProviders]:
    """Create concrete final providers only for render/build, never validate."""
    uv = locate_host_uv()
    canonical_wheel = build_canonical_wheel(uv)
    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        provider = ProviderIdentityAcquirer(
            oci=HttpOciIdentityProvider(client),
            managed_python=UvManagedPythonIdentityProvider(uv),
            comfyui=GitOfficialComfyUIIdentityProvider(),
            registry=HttpRegistryNodeIdentityProvider(client),
            git=GitDirectIdentityProvider(),
            python_group=UvPythonGroupResolver(uv),
        )
        yield DefaultPlanningProviders(
            CachingCanonicalAcquirer(provider),
            LocalExecutableEntryAcquirer(FilesystemLocalExecutableIdentityProvider()),
            canonical_wheel,
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
                isinstance(entry, UvImageLockEntry)
                and canonical_entry_key(entry) == ("images", "uv")
                and entries_satisfy_request(request, (entry,), digest)
            ):
                return entry.digest
    if policy is LockPolicy.LOCKED:
        return f"sha256:{'0' * 64}"
    try:
        acquired = acquirer.acquire(request, digest)
    except CanonicalAcquisitionError as error:
        raise CanonicalResolutionError(
            (
                Diagnostic(
                    path=("config.lock.toml", "images", "uv"),
                    code="lock.resolve_failed",
                    message=str(error),
                ),
            )
        ) from error
    if not entries_satisfy_request(request, acquired.entries, digest):
        raise ValueError("uv provider returned an incompatible descriptor")
    entry = acquired.entries[0]
    if not isinstance(entry, UvImageLockEntry):  # pragma: no cover - proven above
        raise AssertionError("compatible uv OCI result must be an OCI entry")
    return entry.digest


def build_local_executable_requests(
    graph: CanonicalRequestGraph,
    *,
    build_hooks_dir: str | Path | None,
    runtime_hook_requests: tuple[LocalExecutableIdentityRequest, ...] = (),
) -> tuple[LocalExecutableIdentityRequest, ...]:
    relative_hooks = sorted(
        {
            hook
            for node in graph.custom_nodes
            for hook in (*node.pre_install_hooks, *node.post_install_hooks)
        }
    )
    if relative_hooks and build_hooks_dir is None:
        raise ValueError("build-hook requests require an admitted source root")
    root = Path(build_hooks_dir).resolve() if build_hooks_dir is not None else None
    return (
        tuple(
            LocalExecutableIdentityRequest(
                root,
                PurePosixPath(path),
                PurePosixPath(hook_lock_identity("build", path)),
            )
            for path in relative_hooks
            if root is not None
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
    request = comfyui_request(config)
    digest = compute_request_digest(request)
    current = _existing_entry(existing, ("comfyui",))
    if (
        (
            policy is not LockPolicy.UPGRADE
            or request_stability(request) is SelectorStability.EXACT
        )
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
        ("comfyui",),
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
    key = ("comfyui", "requirements")
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


def planning_release_inputs(canonical_wheel: CanonicalWheel) -> PlanningReleaseInputs:
    """Collect exact release-owned request and toolchain artifacts once."""
    return PlanningReleaseInputs(
        pip_version=PIP_VERSION,
        cdh_version=CDH_VERSION,
        cdh_wheel_digest=canonical_wheel.digest,
    )
