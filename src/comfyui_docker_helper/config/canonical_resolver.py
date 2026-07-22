"""Canonical-lock reconciliation and provider-acquisition boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from comfyui_docker_helper.config.canonical_lock import (
    ApplicationExtrasLockEntry,
    CanonicalLock,
    CanonicalLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    CudaImageLockEntry,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    PythonGroupRequestIdentity,
    PyTorchLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    RegistryRequestIdentity,
    ResolverRequestIdentity,
    UvImageLockEntry,
    UvToolLockEntry,
    canonical_entry_key,
    canonical_lock_from_entries,
    pytorch_core_version_matches_channel,
    uv_image_version_matches_tag,
)
from comfyui_docker_helper.config.canonical_request import (
    DesiredResolution,
    SelectorStability,
    request_keys,
)
from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticError
from comfyui_docker_helper.exact_ledger import COMFYUI_MINIMUM_VERSION
from comfyui_docker_helper.host.identity_providers import (
    LocalExecutableIdentityRequest,
)

type LockEntryKey = tuple[str, ...]


class LockPolicy(StrEnum):
    """Resolution policy selected independently from output side effects."""

    DEFAULT = "default"
    LOCKED = "locked"
    UPGRADE = "upgrade"


class ReconcilePurpose(StrEnum):
    """Whether a caller may later persist the accepted lock."""

    APPLY = "apply"
    CHECK = "check"
    DRY_RUN = "dry-run"


class DeltaKind(StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class LockDeltaItem:
    key: LockEntryKey
    kind: DeltaKind


@dataclass(frozen=True, slots=True)
class AcceptedCanonicalLock:
    """Accepted lock result with provider, local-read, and write intent."""

    lock: CanonicalLock
    delta: tuple[LockDeltaItem, ...]
    write_intent: bool
    provider_calls: tuple[LockEntryKey, ...]
    local_reads: tuple[LockEntryKey, ...]

    @property
    def changed(self) -> bool:
        return bool(self.delta)


@dataclass(frozen=True, slots=True)
class AcquiredCanonicalEntries:
    entries: tuple[CanonicalLockEntry, ...]
    provider_called: bool


class CanonicalEntryAcquirer(Protocol):
    """Typed provider bundle seam used only for requests selected by policy."""

    def acquire(
        self, request: ResolverRequestIdentity, request_digest: str
    ) -> AcquiredCanonicalEntries: ...


class LocalExecutableEntryAcquirer(Protocol):
    """Local content seam; reads are tracked separately from provider calls."""

    def acquire(
        self, request: LocalExecutableIdentityRequest
    ) -> LocalExecutableLockEntry: ...


class CanonicalResolutionError(DiagnosticError):
    """Deterministically aggregated reconciliation/acquisition failures."""


class CanonicalAcquisitionError(Exception):
    """Expected short provider/group acquisition failure."""


def reconcile_canonical_lock(
    desired: tuple[DesiredResolution, ...],
    *,
    local_requests: tuple[LocalExecutableIdentityRequest, ...] = (),
    local_acquirer: LocalExecutableEntryAcquirer | None = None,
    existing: CanonicalLock | None,
    acquirer: CanonicalEntryAcquirer,
    policy: LockPolicy = LockPolicy.DEFAULT,
    purpose: ReconcilePurpose = ReconcilePurpose.APPLY,
) -> AcceptedCanonicalLock:
    """Accept one deterministic lock result without performing writes."""
    if purpose is ReconcilePurpose.CHECK and policy is not LockPolicy.DEFAULT:
        raise ValueError("check uses default reconciliation policy")
    ordered = tuple(sorted(desired, key=lambda item: item.keys))
    fixed, local_reads = _acquire_local_entries(local_requests, local_acquirer)
    _validate_desired_keys(ordered, fixed)
    existing_by_key = _entry_map(existing.entries if existing is not None else ())

    if policy is LockPolicy.LOCKED:
        return _accept_locked(ordered, fixed, local_reads, existing, existing_by_key)

    accepted: dict[LockEntryKey, CanonicalLockEntry] = {}
    provider_calls: list[LockEntryKey] = []
    diagnostics: list[Diagnostic] = []
    for item in ordered:
        current = tuple(existing_by_key.get(key) for key in item.keys)
        compatible = all(
            entry is not None
            and getattr(entry, "request_digest", None) == item.request_digest
            for entry in current
        ) and entries_satisfy_request(
            item.request,
            tuple(entry for entry in current if entry is not None),
            item.request_digest,
        )
        refresh = policy is LockPolicy.UPGRADE and (
            item.stability is SelectorStability.MOVING
        )
        if compatible and not refresh:
            accepted.update(
                (key, entry)
                for key, entry in zip(item.keys, current, strict=True)
                if entry is not None
            )
            continue

        try:
            acquisition = acquirer.acquire(item.request, item.request_digest)
        except CanonicalAcquisitionError as error:
            diagnostics.append(
                Diagnostic(
                    path=("config.lock.toml", *item.keys[0]),
                    code="lock.resolve_failed",
                    message=str(error),
                )
            )
            continue
        if acquisition.provider_called:
            provider_calls.append(item.keys[0])
        resolved = acquisition.entries
        resolved_by_key = _entry_map(resolved)
        if not entries_satisfy_request(
            item.request,
            resolved,
            item.request_digest,
        ):
            raise ValueError("provider returned an incompatible identity set")
        accepted.update(resolved_by_key)

    accepted.update((canonical_entry_key(entry), entry) for entry in fixed)
    if diagnostics:
        raise CanonicalResolutionError(tuple(diagnostics))

    lock = canonical_lock_from_entries([accepted[key] for key in sorted(accepted)])
    delta = _lock_delta(existing_by_key, accepted)
    write_intent = (
        purpose is ReconcilePurpose.APPLY
        and policy is not LockPolicy.LOCKED
        and bool(delta)
    )
    return AcceptedCanonicalLock(
        lock, delta, write_intent, tuple(provider_calls), local_reads
    )


def _accept_locked(
    desired: tuple[DesiredResolution, ...],
    fixed: tuple[LocalExecutableLockEntry, ...],
    local_reads: tuple[LockEntryKey, ...],
    existing: CanonicalLock | None,
    existing_by_key: dict[LockEntryKey, CanonicalLockEntry],
) -> AcceptedCanonicalLock:
    diagnostics: list[Diagnostic] = []
    if existing is None:
        diagnostics.append(
            Diagnostic(
                path=("config.lock.toml",),
                code="lock.required",
                message="locked mode requires an existing canonical lock",
            )
        )
    expected_keys = {key for item in desired for key in item.keys} | {
        canonical_entry_key(entry) for entry in fixed
    }
    actual_keys = set(existing_by_key)
    for key in sorted(expected_keys - actual_keys):
        diagnostics.append(_locked_diagnostic(key, "missing"))
    for key in sorted(actual_keys - expected_keys):
        diagnostics.append(_locked_diagnostic(key, "extra"))
    for item in desired:
        entries = tuple(
            existing_by_key[key] for key in item.keys if key in existing_by_key
        )
        if len(entries) == len(item.keys) and not entries_satisfy_request(
            item.request,
            entries,
            item.request_digest,
        ):
            diagnostics.extend(
                _locked_diagnostic(key, "request or result changed")
                for key in item.keys
            )
    for expected in fixed:
        key = canonical_entry_key(expected)
        actual = existing_by_key.get(key)
        if actual is not None and actual != expected:
            diagnostics.append(_locked_diagnostic(key, "content changed"))
    if diagnostics:
        raise CanonicalResolutionError(tuple(diagnostics))
    if existing is None:  # pragma: no cover - guarded by diagnostics above
        raise AssertionError("existing lock must be present")
    lock = canonical_lock_from_entries(
        [existing_by_key[key] for key in sorted(existing_by_key)]
    )
    return AcceptedCanonicalLock(lock, (), False, (), local_reads)


def _acquire_local_entries(
    requests: tuple[LocalExecutableIdentityRequest, ...],
    acquirer: LocalExecutableEntryAcquirer | None,
) -> tuple[tuple[LocalExecutableLockEntry, ...], tuple[LockEntryKey, ...]]:
    if requests and acquirer is None:
        raise ValueError("local executable requests require a local acquirer")
    ordered = tuple(sorted(requests, key=lambda item: item.canonical_path.as_posix()))
    entries: list[LocalExecutableLockEntry] = []
    diagnostics: list[Diagnostic] = []
    reads: list[LockEntryKey] = []
    for request in ordered:
        identity = request.canonical_path
        if identity.parts[0] == "build-hooks":
            relative = PurePosixPath(*identity.parts[1:]).as_posix()
            key = ("hooks", "build", relative)
        elif identity.parts[0] == "runtime-hooks":
            key = ("hooks", "runtime", PurePosixPath(*identity.parts[1:]).as_posix())
        else:
            raise ValueError("local executable request has no supported hook domain")
        reads.append(key)
        try:
            entry = acquirer.acquire(request) if acquirer is not None else None
        except CanonicalAcquisitionError as error:
            diagnostics.append(
                Diagnostic(
                    path=("config.lock.toml", *key),
                    code="lock.local_read_failed",
                    message=str(error),
                )
            )
            continue
        if entry is None or canonical_entry_key(entry) != key:
            raise ValueError("local acquirer returned an incompatible identity")
        entries.append(entry)
    if diagnostics:
        raise CanonicalResolutionError(tuple(diagnostics))
    return tuple(entries), tuple(reads)


def entries_satisfy_request(
    request: ResolverRequestIdentity,
    entries: tuple[CanonicalLockEntry, ...],
    request_digest: str,
) -> bool:
    """Prove that locally valid resolved entries satisfy one canonical request."""
    expected_keys = set(request_keys(request))
    by_key = _entry_map(entries)
    if (
        len(entries) != len(by_key)
        or len(by_key) != len(expected_keys)
        or set(by_key) != expected_keys
        or any(
            getattr(entry, "request_digest", None) != request_digest
            for entry in entries
        )
    ):
        return False
    if isinstance(request, OciRequestIdentity):
        entry = entries[0]
        expected_type = (
            CudaImageLockEntry if request.role == "cuda-base" else UvImageLockEntry
        )
        return isinstance(entry, expected_type) and (
            entry.repository == request.repository
            and entry.tag == request.tag
            and entry.platform == request.platform
            and (
                request.role != "uv-tool"
                or (
                    isinstance(entry, UvImageLockEntry)
                    and uv_image_version_matches_tag(
                        request.tag, entry.observed_version
                    )
                )
            )
        )
    if isinstance(request, ManagedPythonRequestIdentity):
        entry = entries[0]
        return isinstance(entry, ManagedPythonLockEntry) and (
            entry.version == request.version
            and entry.platform == request.platform
            and entry.libc == request.libc
            and entry.catalog_digest == request.catalog_descriptor_digest
        )
    if isinstance(request, ComfyUIRequestIdentity):
        entry = entries[0]
        return isinstance(entry, OfficialComfyUILockEntry) and (
            entry.repository == request.repository
            and _comfyui_result_matches(request.selector, entry)
        )
    if isinstance(request, ComfyUIRequirementsRequestIdentity):
        entry = entries[0]
        return isinstance(entry, ComfyUIRequirementsLockEntry) and (
            all(item.name in request.protected_names for item in entry.pytorch)
        )
    if isinstance(request, ComfyCliRequestIdentity):
        entry = entries[0]
        return isinstance(entry, UvToolLockEntry) and (
            entry.name == request.package
            and entry.extras == ()
            and Version(entry.version) >= Version(request.minimum_version)
            and _is_stable(Version(entry.version))
        )
    if isinstance(request, RegistryRequestIdentity):
        entry = entries[0]
        return isinstance(entry, RegistryNodeLockEntry) and (
            entry.id == request.id
            and _published_result_matches(request.selector, entry.version)
        )
    if isinstance(request, DirectGitRequestIdentity):
        entry = entries[0]
        return isinstance(entry, DirectGitLockEntry) and (
            entry.url == request.url
            and (not _is_commit(request.ref) or entry.commit == request.ref)
        )
    if not isinstance(request, PythonGroupRequestIdentity):
        return False
    if not isinstance(request, PyTorchRequestIdentity) and request.group == "uv-tool":
        entry = entries[0]
        member = request.members[0]
        return (
            isinstance(entry, UvToolLockEntry)
            and entry.name == member.package
            and entry.extras == member.extras
            and _direct_result_matches(member.selector, entry.version)
        )
    entry = entries[0]
    if isinstance(request, PyTorchRequestIdentity):
        if not isinstance(entry, PyTorchLockEntry):
            return False
        packages = entry.packages
    elif request.group == "application-extra":
        if not isinstance(entry, ApplicationExtrasLockEntry):
            return False
        packages = entry.packages
    else:  # pragma: no cover - uv-tool groups returned above
        return False
    members = {member.package: member for member in request.members}
    if {package.name for package in packages} != set(members):
        return False
    for package in packages:
        member = members[package.name]
        if package.extras != member.extras or not _direct_result_matches(
            member.selector, package.version
        ):
            return False
        if isinstance(request, PyTorchRequestIdentity) and not (
            pytorch_core_version_matches_channel(
                package.name, package.version, request.channel
            )
        ):
            return False
    return True


def rebuild_canonical_entries(
    entries: tuple[CanonicalLockEntry, ...] | list[CanonicalLockEntry],
) -> tuple[CanonicalLockEntry, ...]:
    """Strictly rebuild discriminated entries after crossing an object seam."""
    return tuple(
        type(entry).model_validate(entry.model_dump(mode="python"), strict=True)
        for entry in entries
    )


def _validate_unique_entries(entries: tuple[CanonicalLockEntry, ...]) -> None:
    keys = [canonical_entry_key(entry) for entry in entries]
    if len(keys) != len(set(keys)):
        raise ValueError("canonical lock entries must have unique logical identities")


def _locked_diagnostic(key: LockEntryKey, reason: str) -> Diagnostic:
    return Diagnostic(
        path=("config.lock.toml", *key),
        code="lock.locked_mismatch",
        message=f"locked identity is {reason}; regenerate config.lock.toml",
    )


def _validate_desired_keys(
    desired: tuple[DesiredResolution, ...],
    fixed: tuple[LocalExecutableLockEntry, ...],
) -> None:
    keys = [key for item in desired for key in item.keys]
    keys.extend(canonical_entry_key(entry) for entry in fixed)
    if len(keys) != len(set(keys)):
        raise ValueError("desired canonical lock identities must be unique")


def _entry_map(
    entries: tuple[CanonicalLockEntry, ...] | list[CanonicalLockEntry],
) -> dict[LockEntryKey, CanonicalLockEntry]:
    _validate_unique_entries(tuple(entries))
    return {canonical_entry_key(entry): entry for entry in entries}


def _lock_delta(
    before: dict[LockEntryKey, CanonicalLockEntry],
    after: dict[LockEntryKey, CanonicalLockEntry],
) -> tuple[LockDeltaItem, ...]:
    items = [
        LockDeltaItem(key, DeltaKind.REMOVED) for key in before.keys() - after.keys()
    ]
    items.extend(
        LockDeltaItem(key, DeltaKind.ADDED) for key in after.keys() - before.keys()
    )
    items.extend(
        LockDeltaItem(key, DeltaKind.UPDATED)
        for key in before.keys() & after.keys()
        if before[key] != after[key]
    )
    return tuple(sorted(items, key=lambda item: item.key))


def _is_commit(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )


def _comfyui_result_matches(selector: str, entry: OfficialComfyUILockEntry) -> bool:
    if _is_commit(selector):
        return entry.commit == selector and entry.formal_release is None
    if selector == "nightly":
        return entry.formal_release is None
    if entry.formal_release is None:
        return False
    version = Version(entry.formal_release)
    if version < Version(COMFYUI_MINIMUM_VERSION) or not _is_stable(version):
        return False
    if selector == "latest":
        return True
    if selector[0].isdigit():
        return entry.formal_release == selector
    return SpecifierSet(selector).contains(version, prereleases=False)


def _published_result_matches(selector: str, value: str) -> bool:
    version = Version(value)
    if selector == "latest":
        return _is_stable(version)
    if not any(character in selector for character in "<>=!,"):
        return value == selector
    return _is_stable(version) and SpecifierSet(selector).contains(
        version, prereleases=False
    )


def _direct_result_matches(selector: str, value: str) -> bool:
    version = Version(value)
    if version.is_prerelease or version.is_devrelease:
        return False
    return not selector or SpecifierSet(selector).contains(version, prereleases=False)


def _is_stable(version: Version) -> bool:
    return not (
        version.is_prerelease or version.is_devrelease or version.local is not None
    )
