"""Focused canonical reconciliation mode and delta contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, field, replace
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    CanonicalLockEntry,
    ComfyCliLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    DirectPythonLockEntry,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciLockEntry,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    ProtectedRequirementProjection,
    PyTorchCompatibilityLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    RegistryRequestIdentity,
    ResolverRequestIdentity,
    canonical_entry_key,
)
from comfyui_docker_helper.config.canonical_resolver import (
    AcceptedCanonicalLock,
    AcquiredCanonicalEntries,
    CanonicalAcquisitionError,
    CanonicalResolutionError,
    DeltaKind,
    DesiredResolution,
    LockPolicy,
    ManagedPythonReleaseInputs,
    ReconcilePurpose,
    reconcile_canonical_lock,
)
from comfyui_docker_helper.exact_ledger import COMFYUI_FLOOR_COMMIT
from comfyui_docker_helper.host.canonical_acquisition import (
    LocalExecutableEntryAcquirer,
)
from comfyui_docker_helper.host.identity_providers import (
    FilesystemLocalExecutableIdentityProvider,
    LocalExecutableIdentityRequest,
    SelectorStability,
)

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40

RELEASE = ManagedPythonReleaseInputs(
    pip_version="26.1.2",
    cdh_version="0.5.0",
    cdh_source_digest=DIGEST_A,
    uv_build_version="0.11.28",
)


def _requests() -> tuple[ResolverRequestIdentity, ...]:
    return (
        OciRequestIdentity(
            type="oci",
            role="cuda-base",
            repository="nvidia/cuda",
            tag="13.0.3-cudnn-devel-ubuntu24.04",
            platform="linux/amd64",
        ),
        ManagedPythonRequestIdentity(
            type="managed-python",
            version="3.13.14",
            implementation="cpython",
            platform="linux/amd64",
            libc="gnu",
            catalog_descriptor_digest=DIGEST_B,
        ),
        ComfyUIRequestIdentity(
            type="comfyui",
            repository="https://github.com/Comfy-Org/ComfyUI.git",
            selector="latest",
        ),
        ComfyCliRequestIdentity(
            type="comfy-cli",
            package="comfy-cli",
            policy="highest-target-compatible-stable",
            minimum_version="1.7.0",
            environment="uv-tool:comfy-cli",
            index_url="https://pypi.org/simple",
            python_version="3.13.14",
            platform="linux/amd64",
        ),
        RegistryRequestIdentity(type="registry", id="example-node", selector="latest"),
        DirectGitRequestIdentity(
            type="git", url="https://example.test/node.git", ref=COMMIT_A
        ),
        PyTorchRequestIdentity(
            type="pytorch-group",
            environment="application",
            group="pytorch",
            backend="cuda",
            channel="cu130",
            python_version="3.13.14",
            platform="linux/amd64",
            python_index_url="https://pypi.org/simple",
            pytorch_index_url="https://download.pytorch.org/whl/cu130",
            members=[
                DirectPythonRequestMember(
                    package="torch", extras=[], selector="==2.12.1"
                ),
                DirectPythonRequestMember(
                    package="torchvision", extras=[], selector="<0.28,>=0.27"
                ),
            ],
        ),
    )


def _desired(
    requests: tuple[ResolverRequestIdentity, ...] | None = None,
) -> tuple[DesiredResolution, ...]:
    return tuple(
        DesiredResolution.from_request(
            item,
            managed_python_release=(
                RELEASE if isinstance(item, ManagedPythonRequestIdentity) else None
            ),
        )
        for item in requests or _requests()
    )


def _local_request() -> LocalExecutableIdentityRequest:
    return LocalExecutableIdentityRequest(
        root=Path("/scripts"), relative_path=PurePosixPath("hooks/pre.py")
    )


# Reconciliation reuses compatible identities and invokes providers only by mode policy.
def test_requirements_sidecar_reuses_refreshes_and_locked_never_calls_provider() -> (
    None
):
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository="https://github.com/Comfy-Org/ComfyUI.git",
        commit=COMMIT_A,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
        python_version="3.13.14",
        platform="linux/amd64",
        protected_names=["torch", "torchaudio", "torchvision"],
        protected_policy_digest=DIGEST_B,
    )

    @dataclass
    class SidecarAcquirer:
        calls: int = 0

        def acquire(self, requested, request_digest: str):
            self.calls += 1
            return AcquiredCanonicalEntries(
                (
                    ComfyUIRequirementsLockEntry(
                        type="comfyui-requirements",
                        request_digest=request_digest,
                        repository=requested.repository,
                        commit=requested.commit,
                        floor_commit=requested.floor_commit,
                        path=requested.path,
                        python_version=requested.python_version,
                        platform=requested.platform,
                        protected_names=requested.protected_names,
                        protected_policy_digest=requested.protected_policy_digest,
                        requirements_digest=DIGEST_A,
                        protected=[
                            ProtectedRequirementProjection(
                                package="torchaudio", extras=[], selector=""
                            )
                        ],
                    ),
                ),
                True,
            )

    desired = _desired((request,))
    first_acquirer = SidecarAcquirer()
    first = reconcile_canonical_lock(desired, existing=None, acquirer=first_acquirer)
    assert first_acquirer.calls == 1
    assert first.provider_calls == (("comfyui-requirements", request.repository),)

    default_acquirer = SidecarAcquirer()
    reused = reconcile_canonical_lock(
        desired, existing=first.lock, acquirer=default_acquirer
    )
    assert default_acquirer.calls == 0
    assert not reused.changed

    upgrade_acquirer = SidecarAcquirer()
    upgraded = reconcile_canonical_lock(
        desired,
        existing=first.lock,
        acquirer=upgrade_acquirer,
        policy=LockPolicy.UPGRADE,
    )
    assert upgrade_acquirer.calls == 1
    assert not upgraded.changed

    locked_acquirer = SidecarAcquirer()
    locked = reconcile_canonical_lock(
        desired,
        existing=first.lock,
        acquirer=locked_acquirer,
        policy=LockPolicy.LOCKED,
    )
    assert locked_acquirer.calls == 0
    assert not locked.changed


@dataclass
class FakeLocalAcquirer:
    digest: str = DIGEST_A
    calls: list[LocalExecutableIdentityRequest] = field(default_factory=list)

    def acquire(
        self, request: LocalExecutableIdentityRequest
    ) -> LocalExecutableLockEntry:
        self.calls.append(request)
        return LocalExecutableLockEntry(
            type="local-executable",
            relative_path=request.relative_path.as_posix(),
            digest=self.digest,
        )


@dataclass
class FakeAcquirer:
    calls: list[str] = field(default_factory=list)
    provider_calls: list[str] = field(default_factory=list)
    generations: dict[str, int] = field(default_factory=dict)
    failures: set[str] = field(default_factory=set)
    incompatible: set[str] = field(default_factory=set)
    release: ManagedPythonReleaseInputs = RELEASE

    def acquire(
        self, request: ResolverRequestIdentity, request_digest: str
    ) -> AcquiredCanonicalEntries:
        kind = request.type
        self.calls.append(kind)
        provider_called = _fake_uses_provider(request)
        if provider_called:
            self.provider_calls.append(kind)
        if kind in self.failures:
            raise CanonicalAcquisitionError(f"{kind} identity is unavailable")
        generation = self.generations.get(kind, 0) + 1
        self.generations[kind] = generation
        digest = f"sha256:{generation:064x}"
        effective_digest = DIGEST_A if kind in self.incompatible else request_digest
        if isinstance(request, OciRequestIdentity):
            entries: tuple[CanonicalLockEntry, ...] = (
                OciLockEntry(
                    type="oci",
                    request_digest=effective_digest,
                    role=request.role,
                    repository=request.repository,
                    tag=request.tag,
                    descriptor_digest=digest,
                    descriptor_kind="index",
                    platform=request.platform,
                    resolved_version=("0.11.28" if request.role == "uv-tool" else None),
                ),
            )
        elif isinstance(request, ManagedPythonRequestIdentity):
            entries = (
                ManagedPythonLockEntry(
                    type="managed-python",
                    request_digest=effective_digest,
                    version=request.version,
                    implementation=request.implementation,
                    platform=request.platform,
                    libc=request.libc,
                    provider="uv-managed",
                    catalog_descriptor_digest=request.catalog_descriptor_digest,
                    catalog_key=f"cpython-{request.version}-linux-x86_64-gnu",
                    catalog_url="https://example.test/python.tar.zst",
                    pip_version=self.release.pip_version,
                    cdh_version=self.release.cdh_version,
                    cdh_source_digest=self.release.cdh_source_digest,
                    uv_build_version=self.release.uv_build_version,
                ),
            )
        elif isinstance(request, ComfyUIRequestIdentity):
            entries = (
                OfficialComfyUILockEntry(
                    type="comfyui",
                    request_digest=effective_digest,
                    repository=request.repository,
                    commit=f"{generation:x}" * 40,
                    formal_release="0.11.0",
                ),
            )
        elif isinstance(request, ComfyCliRequestIdentity):
            entries = (
                ComfyCliLockEntry(
                    type="comfy-cli",
                    request_digest=effective_digest,
                    package="comfy-cli",
                    version="2.0.0",
                    environment=request.environment,
                ),
            )
        elif isinstance(request, RegistryRequestIdentity):
            entries = (
                RegistryNodeLockEntry(
                    type="registry",
                    request_digest=effective_digest,
                    id=request.id,
                    version="1.2.3",
                ),
            )
        elif isinstance(request, DirectGitRequestIdentity):
            entries = (
                DirectGitLockEntry(
                    type="git",
                    request_digest=effective_digest,
                    url=request.url,
                    commit=request.ref if len(request.ref) == 40 else COMMIT_B,
                ),
            )
        else:
            versions = {
                "torch": "2.12.1+cu130",
                "torchvision": "0.27.1+cu130",
            }
            entries = tuple(
                DirectPythonLockEntry(
                    type="python-package",
                    request_digest=effective_digest,
                    package=member.package,
                    extras=member.extras,
                    version=versions[member.package],
                    environment=request.environment,
                )
                for member in request.members
            )
            if isinstance(request, PyTorchRequestIdentity):
                entries = (
                    *entries,
                    PyTorchCompatibilityLockEntry(
                        type="pytorch-compatibility",
                        request_digest=effective_digest,
                        environment="application",
                        setuptools_specifier="<82",
                    ),
                )
        return AcquiredCanonicalEntries(entries, provider_called)


def _fake_uses_provider(request: ResolverRequestIdentity) -> bool:
    if isinstance(request, (OciRequestIdentity, ManagedPythonRequestIdentity)):
        return True
    if isinstance(request, ComfyUIRequestIdentity):
        return len(request.selector) != 40
    if isinstance(request, ComfyCliRequestIdentity):
        return True
    if isinstance(request, RegistryRequestIdentity):
        return True
    if isinstance(request, DirectGitRequestIdentity):
        return len(request.ref) != 40
    return True


def _initial_lock(
    desired: tuple[DesiredResolution, ...] | None = None,
    local_digest: str | None = None,
) -> CanonicalLock:
    local_requests = (_local_request(),) if local_digest is not None else ()
    result = reconcile_canonical_lock(
        desired or _desired(),
        local_requests=local_requests,
        local_acquirer=FakeLocalAcquirer(local_digest or DIGEST_A),
        existing=None,
        acquirer=FakeAcquirer(),
    )
    return result.lock


def test_request_table_has_exact_keys_and_stability_for_every_domain() -> None:
    desired = _desired()

    assert [item.keys for item in desired] == [
        (("oci", "cuda-base"),),
        (("managed-python", "cpython", "linux/amd64"),),
        (("comfyui", "https://github.com/Comfy-Org/ComfyUI.git"),),
        (("comfy-cli", "comfy-cli", "uv-tool:comfy-cli"),),
        (("registry", "example-node"),),
        (("git", "https://example.test/node.git"),),
        (
            ("python-package", "application", "torch"),
            ("python-package", "application", "torchvision"),
            ("pytorch-compatibility", "application"),
        ),
    ]
    assert [item.stability for item in desired] == [
        SelectorStability.MOVING,
        SelectorStability.EXACT,
        SelectorStability.MOVING,
        SelectorStability.MOVING,
        SelectorStability.MOVING,
        SelectorStability.EXACT,
        SelectorStability.MOVING,
    ]


def test_desired_resolution_derivations_cannot_be_supplied_or_mutated() -> None:
    request = _requests()[0]

    with pytest.raises(TypeError):
        DesiredResolution(
            request=request,
            keys=(("oci", "forged"),),
            request_digest=DIGEST_A,
            stability=SelectorStability.EXACT,
        )

    desired = DesiredResolution.from_request(request)
    with pytest.raises(AttributeError):
        desired.stability = SelectorStability.EXACT


def test_managed_python_requires_typed_release_owned_compatibility() -> None:
    request = _requests()[1]
    assert isinstance(request, ManagedPythonRequestIdentity)

    with pytest.raises(ValueError, match="release-owned inputs"):
        DesiredResolution.from_request(request)

    with pytest.raises(ValueError, match="only to managed Python"):
        DesiredResolution.from_request(_requests()[0], managed_python_release=RELEASE)


@pytest.mark.parametrize(
    ("field_name", "new_value"),
    [
        ("pip_version", "26.1.3"),
        ("cdh_version", "0.5.1"),
        ("cdh_source_digest", DIGEST_B),
        ("uv_build_version", "0.11.29"),
    ],
)
@pytest.mark.parametrize(
    "policy", [LockPolicy.DEFAULT, LockPolicy.LOCKED, LockPolicy.UPGRADE]
)
def test_release_owned_input_change_invalidates_managed_python_without_digest_abuse(
    field_name: str,
    new_value: str,
    policy: LockPolicy,
) -> None:
    request = _requests()[1]
    assert isinstance(request, ManagedPythonRequestIdentity)
    previous = DesiredResolution.from_request(request, managed_python_release=RELEASE)
    existing = _initial_lock((previous,))
    current_release = replace(RELEASE, **{field_name: new_value})
    current = DesiredResolution.from_request(
        request, managed_python_release=current_release
    )
    acquirer = FakeAcquirer(release=current_release)

    assert current.request_digest == previous.request_digest
    if policy is LockPolicy.LOCKED:
        with pytest.raises(CanonicalResolutionError, match="operation failed"):
            reconcile_canonical_lock(
                (current,),
                existing=existing,
                acquirer=acquirer,
                policy=policy,
            )
        assert acquirer.calls == []
        return

    result = reconcile_canonical_lock(
        (current,), existing=existing, acquirer=acquirer, policy=policy
    )

    assert acquirer.calls == ["managed-python"]
    assert len(result.delta) == 1
    assert result.delta[0].kind is DeltaKind.UPDATED


@pytest.mark.parametrize("field_name", ["catalog_key", "catalog_url"])
def test_managed_python_result_only_fields_do_not_enter_desired_compatibility(
    field_name: str,
) -> None:
    desired = (_desired()[1],)
    existing = _initial_lock(desired)
    entry = existing.entries[0]
    assert isinstance(entry, ManagedPythonLockEntry)
    result_only_value = (
        "alternate-catalog-key"
        if field_name == "catalog_key"
        else "https://example.test/alternate-python.tar.zst"
    )
    changed = entry.model_copy(update={field_name: result_only_value})
    existing = CanonicalLock(schema_version=1, entries=[changed])
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(desired, existing=existing, acquirer=acquirer)

    assert acquirer.calls == []
    assert result.delta == ()
    assert result.lock.entries == (changed,)


# Exact upstream movement is refreshed only in writable modes and rejected when locked.
@pytest.mark.parametrize("policy", [LockPolicy.DEFAULT, LockPolicy.UPGRADE])
def test_uv_exact_tag_mismatch_reacquires_in_mutating_modes(
    policy: LockPolicy,
) -> None:
    request = OciRequestIdentity(
        type="oci",
        role="uv-tool",
        repository="ghcr.io/astral-sh/uv",
        tag="0.11.28",
        platform="linux/amd64",
    )
    desired = (DesiredResolution.from_request(request),)
    existing = CanonicalLock(
        schema_version=1,
        entries=[
            OciLockEntry(
                type="oci",
                request_digest=desired[0].request_digest,
                role="uv-tool",
                repository=request.repository,
                tag=request.tag,
                descriptor_digest=DIGEST_A,
                descriptor_kind="index",
                platform=request.platform,
                resolved_version="0.11.29",
            )
        ],
    )
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(
        desired, existing=existing, acquirer=acquirer, policy=policy
    )

    assert acquirer.calls == ["oci"]
    entry = result.lock.entries[0]
    assert isinstance(entry, OciLockEntry)
    assert entry.resolved_version == "0.11.28"


def test_uv_exact_tag_mismatch_fails_locked_without_provider_calls() -> None:
    request = OciRequestIdentity(
        type="oci",
        role="uv-tool",
        repository="ghcr.io/astral-sh/uv",
        tag="0.11.28",
        platform="linux/amd64",
    )
    desired = (DesiredResolution.from_request(request),)
    existing = CanonicalLock(
        schema_version=1,
        entries=[
            OciLockEntry(
                type="oci",
                request_digest=desired[0].request_digest,
                role="uv-tool",
                repository=request.repository,
                tag=request.tag,
                descriptor_digest=DIGEST_A,
                descriptor_kind="index",
                platform=request.platform,
                resolved_version="0.11.29",
            )
        ],
    )
    acquirer = FakeAcquirer()

    with pytest.raises(CanonicalResolutionError) as raised:
        reconcile_canonical_lock(
            desired,
            existing=existing,
            acquirer=acquirer,
            policy=LockPolicy.LOCKED,
        )

    assert acquirer.calls == []
    assert [item.code for item in raised.value.diagnostics] == ["lock.locked_mismatch"]


def test_upgrade_refreshes_the_internal_moving_comfy_cli_request() -> None:
    request = _requests()[3]
    assert isinstance(request, ComfyCliRequestIdentity)
    desired = DesiredResolution.from_request(request)
    existing = _initial_lock((desired,))
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(
        (desired,),
        existing=existing,
        acquirer=acquirer,
        policy=LockPolicy.UPGRADE,
    )

    assert desired.stability is SelectorStability.MOVING
    assert acquirer.calls == ["comfy-cli"]
    assert acquirer.provider_calls == ["comfy-cli"]
    assert result.delta == ()


def test_admitted_request_lock_and_result_are_deeply_immutable() -> None:
    request = _requests()[-1]
    desired = DesiredResolution.from_request(request)
    lock = _initial_lock((desired,))
    accepted = AcceptedCanonicalLock(lock, (), False, (), ())

    assert isinstance(request.members, tuple)
    assert isinstance(request.members[0].extras, tuple)
    assert isinstance(lock.entries, tuple)
    with pytest.raises(ValidationError, match="frozen"):
        request.members[0].selector = "==9.9.9"
    with pytest.raises(ValidationError, match="frozen"):
        lock.entries[0].request_digest = DIGEST_B
    with pytest.raises(FrozenInstanceError):
        accepted.lock = CanonicalLock(schema_version=1, entries=())


# Default mode acquires, reuses, adds, and removes only the affected canonical
# identities.
def test_default_initial_acquisition_is_sorted_and_returns_write_intent() -> None:
    acquirer = FakeAcquirer()
    local = FakeLocalAcquirer()
    writes = 0

    result = reconcile_canonical_lock(
        tuple(reversed(_desired())),
        local_requests=(_local_request(),),
        local_acquirer=local,
        existing=None,
        acquirer=acquirer,
    )

    assert acquirer.calls == [
        "comfy-cli",
        "comfyui",
        "git",
        "managed-python",
        "oci",
        "pytorch-group",
        "registry",
    ]
    assert acquirer.provider_calls == [
        "comfy-cli",
        "comfyui",
        "managed-python",
        "oci",
        "pytorch-group",
        "registry",
    ]
    assert all(item.kind is DeltaKind.ADDED for item in result.delta)
    assert result.write_intent is True
    assert len(local.calls) == 1
    assert result.local_reads == (("local-executable", "hooks/pre.py"),)
    assert writes == 0


def test_default_reuses_matching_entries_and_removes_only_deleted_keys() -> None:
    full = _desired()
    existing = _initial_lock(full, DIGEST_A)
    retained = tuple(item for item in full if item.request.type != "registry")
    acquirer = FakeAcquirer()
    local = FakeLocalAcquirer()

    result = reconcile_canonical_lock(
        retained,
        local_requests=(_local_request(),),
        local_acquirer=local,
        existing=existing,
        acquirer=acquirer,
    )

    assert acquirer.calls == []
    assert acquirer.provider_calls == []
    assert len(local.calls) == 1
    assert len(result.delta) == 1
    assert result.delta[0].key == ("registry", "example-node")
    assert result.delta[0].kind is DeltaKind.REMOVED


@pytest.mark.parametrize(
    ("purpose", "write_intent"),
    [
        (ReconcilePurpose.APPLY, True),
        (ReconcilePurpose.CHECK, False),
        (ReconcilePurpose.DRY_RUN, False),
    ],
)
def test_disabling_comfy_cli_removes_or_reports_only_its_entry(
    purpose: ReconcilePurpose, write_intent: bool
) -> None:
    cli = _desired((_requests()[3],))
    existing = _initial_lock(cli)
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(
        (),
        existing=existing,
        acquirer=acquirer,
        purpose=purpose,
    )

    assert acquirer.calls == []
    assert [(item.key, item.kind) for item in result.delta] == [
        (("comfy-cli", "comfy-cli", "uv-tool:comfy-cli"), DeltaKind.REMOVED)
    ]
    assert result.write_intent is write_intent


def test_locked_disabled_mode_rejects_an_extra_comfy_cli_without_provider_calls() -> (
    None
):
    cli = _desired((_requests()[3],))
    existing = _initial_lock(cli)
    acquirer = FakeAcquirer()

    with pytest.raises(CanonicalResolutionError) as raised:
        reconcile_canonical_lock(
            (), existing=existing, acquirer=acquirer, policy=LockPolicy.LOCKED
        )

    assert acquirer.calls == []
    assert raised.value.diagnostics[0].path[-3:] == (
        "comfy-cli",
        "comfy-cli",
        "uv-tool:comfy-cli",
    )


@pytest.mark.parametrize(
    ("purpose", "write_intent"),
    [
        (ReconcilePurpose.APPLY, True),
        (ReconcilePurpose.CHECK, False),
        (ReconcilePurpose.DRY_RUN, False),
    ],
)
def test_enabling_comfy_cli_acquires_exact_entry_in_non_locked_modes(
    purpose: ReconcilePurpose, write_intent: bool
) -> None:
    cli = _desired((_requests()[3],))
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(
        cli, existing=None, acquirer=acquirer, purpose=purpose
    )

    assert acquirer.calls == ["comfy-cli"]
    assert result.lock.entries[0].environment == "uv-tool:comfy-cli"
    assert result.lock.entries[0].version == "2.0.0"
    assert result.write_intent is write_intent


def test_locked_enabled_mode_reports_missing_without_provider_calls() -> None:
    cli = _desired((_requests()[3],))
    acquirer = FakeAcquirer()

    with pytest.raises(CanonicalResolutionError):
        reconcile_canonical_lock(
            cli, existing=None, acquirer=acquirer, policy=LockPolicy.LOCKED
        )

    assert acquirer.calls == []


# Cohesive groups refresh as a unit while unrelated exact results remain reusable.
def test_one_changed_group_member_reacquires_the_complete_group_only() -> None:
    desired = _desired()
    existing = _initial_lock(desired)
    requests = list(_requests())
    group = requests[-1]
    assert isinstance(group, PyTorchRequestIdentity)
    members = [
        group.members[0],
        group.members[1].model_copy(update={"selector": "<0.29,>=0.27"}),
    ]
    requests[-1] = PyTorchRequestIdentity.model_validate(
        {**group.model_dump(), "members": members}
    )
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(
        _desired(tuple(requests)), existing=existing, acquirer=acquirer
    )

    assert acquirer.calls == ["pytorch-group"]
    assert acquirer.provider_calls == ["pytorch-group"]
    package_delta = [item for item in result.delta if item.key[0] == "python-package"]
    assert {item.key[-1] for item in package_delta} == {"torch", "torchvision"}
    assert all(item.kind is DeltaKind.UPDATED for item in package_delta)


@pytest.mark.parametrize(
    ("entry_type", "expected_call"),
    [
        ("oci", "oci"),
        ("managed-python", "managed-python"),
        ("comfyui", "comfyui"),
        ("registry", "registry"),
        ("git", "git"),
        ("python-package", "pytorch-group"),
    ],
)
def test_matching_digest_but_incompatible_result_is_reacquired_by_domain(
    entry_type: str, expected_call: str
) -> None:
    desired = _desired()
    existing = _corrupt_lock_result(_initial_lock(desired), entry_type)
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(desired, existing=existing, acquirer=acquirer)

    assert acquirer.calls == [expected_call]
    assert any(item.kind is DeltaKind.UPDATED for item in result.delta)


@pytest.mark.parametrize(
    "entry_type",
    [
        "oci",
        "managed-python",
        "comfyui",
        "registry",
        "git",
        "python-package",
    ],
)
def test_locked_rejects_matching_digest_with_incompatible_resolved_identity(
    entry_type: str,
) -> None:
    desired = _desired()
    existing = _corrupt_lock_result(_initial_lock(desired), entry_type)
    acquirer = FakeAcquirer()

    with pytest.raises(CanonicalResolutionError) as raised:
        reconcile_canonical_lock(
            desired,
            existing=existing,
            acquirer=acquirer,
            policy=LockPolicy.LOCKED,
        )

    assert acquirer.calls == []
    assert all(item.code == "lock.locked_mismatch" for item in raised.value.diagnostics)


def _corrupt_lock_result(lock: CanonicalLock, entry_type: str) -> CanonicalLock:
    entries: list[CanonicalLockEntry] = []
    changed = False
    for entry in lock.entries:
        if changed or entry.type != entry_type:
            entries.append(entry)
            continue
        data = entry.model_dump(mode="python")
        if isinstance(entry, OciLockEntry):
            data["tag"] = "12.9.2-cudnn-devel-ubuntu24.04"
        elif isinstance(entry, ManagedPythonLockEntry):
            data["version"] = "3.12.13"
        elif isinstance(entry, OfficialComfyUILockEntry):
            data["formal_release"] = None
        elif isinstance(entry, RegistryNodeLockEntry):
            data["version"] = "1.2.3-rc.1"
        elif isinstance(entry, DirectGitLockEntry):
            data["commit"] = COMMIT_B
        else:
            data["version"] = "2.11.0+cu130"
        entries.append(type(entry).model_validate(data))
        changed = True
    assert changed
    return CanonicalLock(schema_version=1, entries=entries)


@pytest.mark.parametrize("version", ["2.12.1", "2.12.1+cpu", "2.12.1+cu129"])
@pytest.mark.parametrize("policy", [LockPolicy.DEFAULT, LockPolicy.LOCKED])
def test_pytorch_core_channel_mismatch_is_reacquired_or_rejected_without_calls(
    version: str, policy: LockPolicy
) -> None:
    desired = _desired()
    existing = _initial_lock(desired)
    entries = list(existing.entries)
    index = next(
        index
        for index, entry in enumerate(entries)
        if isinstance(entry, DirectPythonLockEntry) and entry.package == "torch"
    )
    entries[index] = entries[index].model_copy(update={"version": version})
    existing = CanonicalLock(schema_version=1, entries=entries)
    acquirer = FakeAcquirer()

    if policy is LockPolicy.LOCKED:
        with pytest.raises(CanonicalResolutionError) as raised:
            reconcile_canonical_lock(
                desired, existing=existing, acquirer=acquirer, policy=policy
            )
        assert acquirer.calls == []
        assert any(
            item.code == "lock.locked_mismatch" for item in raised.value.diagnostics
        )
        return

    result = reconcile_canonical_lock(
        desired, existing=existing, acquirer=acquirer, policy=policy
    )

    assert acquirer.calls == ["pytorch-group"]
    torch = next(
        entry
        for entry in result.lock.entries
        if isinstance(entry, DirectPythonLockEntry) and entry.package == "torch"
    )
    assert torch.version == "2.12.1+cu130"


# Local content is always rehashed, while upgrade refreshes only moving external inputs.
def test_locked_aggregates_entry_set_digest_and_local_content_without_calls() -> None:
    desired = _desired()
    existing = _initial_lock(desired, DIGEST_A)
    changed = list(desired)
    git = changed[5].request
    assert isinstance(git, DirectGitRequestIdentity)
    changed[5] = DesiredResolution.from_request(
        DirectGitRequestIdentity(type="git", url=git.url, ref=COMMIT_B)
    )
    acquirer = FakeAcquirer()
    local = FakeLocalAcquirer(DIGEST_B)

    with pytest.raises(CanonicalResolutionError) as raised:
        reconcile_canonical_lock(
            tuple(changed[:-1]),
            local_requests=(_local_request(),),
            local_acquirer=local,
            existing=existing,
            acquirer=acquirer,
            policy=LockPolicy.LOCKED,
        )

    assert acquirer.calls == []
    assert len(local.calls) == 1
    assert [item.code for item in raised.value.diagnostics] == [
        "lock.locked_mismatch",
        "lock.locked_mismatch",
        "lock.locked_mismatch",
        "lock.locked_mismatch",
        "lock.locked_mismatch",
    ]


@pytest.mark.parametrize(
    ("policy", "purpose", "write_intent"),
    [
        (LockPolicy.DEFAULT, ReconcilePurpose.APPLY, True),
        (LockPolicy.UPGRADE, ReconcilePurpose.APPLY, True),
        (LockPolicy.DEFAULT, ReconcilePurpose.CHECK, False),
        (LockPolicy.DEFAULT, ReconcilePurpose.DRY_RUN, False),
        (LockPolicy.UPGRADE, ReconcilePurpose.DRY_RUN, False),
    ],
)
def test_every_writable_policy_rehashes_local_content_without_external_calls(
    tmp_path: Path,
    policy: LockPolicy,
    purpose: ReconcilePurpose,
    write_intent: bool,
) -> None:
    script = tmp_path / "hooks" / "pre.py"
    script.parent.mkdir()
    script.write_text("first", encoding="utf-8")
    script.chmod(0o755)
    request = LocalExecutableIdentityRequest(
        root=tmp_path, relative_path=PurePosixPath("hooks/pre.py")
    )
    local = LocalExecutableEntryAcquirer(FilesystemLocalExecutableIdentityProvider())
    existing = reconcile_canonical_lock(
        (),
        local_requests=(request,),
        local_acquirer=local,
        existing=None,
        acquirer=FakeAcquirer(),
    ).lock
    script.write_text("second", encoding="utf-8")
    external = FakeAcquirer()

    result = reconcile_canonical_lock(
        (),
        local_requests=(request,),
        local_acquirer=local,
        existing=existing,
        acquirer=external,
        policy=policy,
        purpose=purpose,
    )

    assert external.calls == []
    assert result.provider_calls == ()
    assert result.local_reads == (("local-executable", "hooks/pre.py"),)
    assert result.delta[0].kind is DeltaKind.UPDATED
    assert result.write_intent is write_intent


def test_locked_rehashes_local_content_and_rejects_change_without_provider_call(
    tmp_path: Path,
) -> None:
    script = tmp_path / "hook.py"
    script.write_text("first", encoding="utf-8")
    script.chmod(0o755)
    request = LocalExecutableIdentityRequest(
        root=tmp_path, relative_path=PurePosixPath("hook.py")
    )
    local = LocalExecutableEntryAcquirer(FilesystemLocalExecutableIdentityProvider())
    existing = reconcile_canonical_lock(
        (),
        local_requests=(request,),
        local_acquirer=local,
        existing=None,
        acquirer=FakeAcquirer(),
    ).lock
    script.write_text("second", encoding="utf-8")
    external = FakeAcquirer()

    with pytest.raises(CanonicalResolutionError, match="operation failed"):
        reconcile_canonical_lock(
            (),
            local_requests=(request,),
            local_acquirer=local,
            existing=existing,
            acquirer=external,
            policy=LockPolicy.LOCKED,
        )

    assert external.calls == []


def test_upgrade_refreshes_all_moving_and_retains_every_exact_entry() -> None:
    desired = _desired()
    existing = _initial_lock(desired)
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(
        desired,
        existing=existing,
        acquirer=acquirer,
        policy=LockPolicy.UPGRADE,
    )

    assert acquirer.calls == [
        "comfy-cli",
        "comfyui",
        "oci",
        "pytorch-group",
        "registry",
    ]
    assert result.provider_calls == (
        ("comfy-cli", "comfy-cli", "uv-tool:comfy-cli"),
        ("comfyui", "https://github.com/Comfy-Org/ComfyUI.git"),
        ("oci", "cuda-base"),
        ("python-package", "application", "torch"),
        ("registry", "example-node"),
    )
    exact_keys = {
        key
        for item in desired
        if item.stability is SelectorStability.EXACT
        for key in item.keys
    }
    old = {canonical_entry_key(entry): entry for entry in existing.entries}
    new = {canonical_entry_key(entry): entry for entry in result.lock.entries}
    assert all(old[key] == new[key] for key in exact_keys)


def test_upgrade_retains_an_unchanged_all_exact_python_group_without_uv_call() -> None:
    group = _requests()[-1]
    assert isinstance(group, PyTorchRequestIdentity)
    exact_group = PyTorchRequestIdentity.model_validate(
        {
            **group.model_dump(),
            "members": [
                group.members[0],
                group.members[1].model_copy(update={"selector": "==0.27.1"}),
            ],
        }
    )
    desired = (DesiredResolution.from_request(exact_group),)
    existing = _initial_lock(desired)
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(
        desired,
        existing=existing,
        acquirer=acquirer,
        policy=LockPolicy.UPGRADE,
    )

    assert desired[0].stability is SelectorStability.EXACT
    assert acquirer.calls == []
    assert acquirer.provider_calls == []
    assert result.delta == ()


def test_non_public_uv_tool_group_acquires_then_reuses_and_locks_without_calls() -> (
    None
):
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="uv-tool:ruff",
        group="uv-tool",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        members=[
            DirectPythonRequestMember(package="ruff", extras=[], selector="==0.12.0")
        ],
    )
    desired = (DesiredResolution.from_request(request),)

    @dataclass
    class UvToolAcquirer:
        calls: int = 0

        def acquire(
            self, requested: ResolverRequestIdentity, request_digest: str
        ) -> AcquiredCanonicalEntries:
            self.calls += 1
            assert requested == request
            return AcquiredCanonicalEntries(
                (
                    DirectPythonLockEntry(
                        type="python-package",
                        request_digest=request_digest,
                        package="ruff",
                        extras=[],
                        version="0.12.0",
                        environment="uv-tool:ruff",
                    ),
                ),
                True,
            )

    first_acquirer = UvToolAcquirer()
    first = reconcile_canonical_lock(desired, existing=None, acquirer=first_acquirer)

    assert first_acquirer.calls == 1
    assert first.provider_calls == (("python-package", "uv-tool:ruff", "ruff"),)
    assert first.lock.entries[0].environment == "uv-tool:ruff"

    for policy in (LockPolicy.DEFAULT, LockPolicy.LOCKED):
        replay_acquirer = UvToolAcquirer()
        replay = reconcile_canonical_lock(
            desired,
            existing=first.lock,
            acquirer=replay_acquirer,
            policy=policy,
        )

        assert replay_acquirer.calls == 0
        assert replay.provider_calls == ()
        assert replay.delta == ()
        assert replay.write_intent is False


@pytest.mark.parametrize(
    ("policy", "purpose", "expected_calls", "write_intent"),
    [
        (LockPolicy.DEFAULT, ReconcilePurpose.CHECK, [], False),
        (LockPolicy.DEFAULT, ReconcilePurpose.DRY_RUN, [], False),
        (
            LockPolicy.UPGRADE,
            ReconcilePurpose.DRY_RUN,
            ["comfy-cli", "comfyui", "oci", "pytorch-group", "registry"],
            False,
        ),
        (LockPolicy.LOCKED, ReconcilePurpose.DRY_RUN, [], False),
    ],
)
# Check and dry-run preserve policy semantics without publishing lock changes.
def test_check_and_dry_run_apply_policy_without_write(
    policy: LockPolicy,
    purpose: ReconcilePurpose,
    expected_calls: list[str],
    write_intent: bool,
) -> None:
    desired = _desired()
    existing = _initial_lock(desired)
    acquirer = FakeAcquirer()

    result = reconcile_canonical_lock(
        desired,
        existing=existing,
        acquirer=acquirer,
        policy=policy,
        purpose=purpose,
    )

    assert acquirer.calls == expected_calls
    assert result.write_intent is write_intent


def test_provider_failures_are_aggregated_in_deterministic_order() -> None:
    acquirer = FakeAcquirer(failures={"oci", "comfyui", "registry"})

    with pytest.raises(CanonicalResolutionError) as raised:
        reconcile_canonical_lock(_desired(), existing=None, acquirer=acquirer)

    assert [item.path[1] for item in raised.value.diagnostics] == [
        "comfyui",
        "oci",
        "registry",
    ]
    assert acquirer.calls == [
        "comfy-cli",
        "comfyui",
        "git",
        "managed-python",
        "oci",
        "pytorch-group",
        "registry",
    ]


def test_incompatible_provider_result_is_a_programmer_error() -> None:
    acquirer = FakeAcquirer(incompatible={"oci"})

    with pytest.raises(ValueError, match="incompatible identity set"):
        reconcile_canonical_lock(_desired(), existing=None, acquirer=acquirer)


def test_duplicate_provider_logical_keys_are_not_folded_or_accepted() -> None:
    class DuplicateAcquirer(FakeAcquirer):
        def acquire(
            self, request: ResolverRequestIdentity, request_digest: str
        ) -> AcquiredCanonicalEntries:
            acquired = super().acquire(request, request_digest)
            return AcquiredCanonicalEntries(
                entries=(*acquired.entries, acquired.entries[0]),
                provider_called=acquired.provider_called,
            )

    request = _requests()[0]

    with pytest.raises(ValueError, match="unique logical identities"):
        reconcile_canonical_lock(
            (DesiredResolution.from_request(request),),
            existing=None,
            acquirer=DuplicateAcquirer(),
        )


def test_check_rejects_nondefault_policy() -> None:
    with pytest.raises(ValueError, match="check uses default"):
        reconcile_canonical_lock(
            _desired(),
            existing=None,
            acquirer=FakeAcquirer(),
            policy=LockPolicy.UPGRADE,
            purpose=ReconcilePurpose.CHECK,
        )
