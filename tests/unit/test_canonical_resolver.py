"""Atomic grouped canonical-lock reconciliation contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest

from comfyui_docker_helper.config.canonical_lock import (
    ApplicationExtrasLockEntry,
    BuildHookLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    CudaImageLockEntry,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    PyTorchLockEntry,
    PyTorchRequestIdentity,
    ResolvedPythonPackage,
    UvImageLockEntry,
    UvToolLockEntry,
)
from comfyui_docker_helper.config.canonical_request import DesiredResolution
from comfyui_docker_helper.config.canonical_resolver import (
    AcquiredCanonicalEntries,
    CanonicalResolutionError,
    DeltaKind,
    LockPolicy,
    ReconcilePurpose,
    reconcile_canonical_lock,
)
from comfyui_docker_helper.exact_ledger import COMFYUI_FLOOR_COMMIT
from comfyui_docker_helper.host.identity_providers import (
    LocalExecutableIdentityRequest,
)

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
COMMIT = "1" * 40


def _requests(*, application_extras: bool = False):
    requests = [
        OciRequestIdentity(
            type="oci",
            role="cuda-base",
            repository="nvidia/cuda",
            tag="13.0.3-cudnn-devel-ubuntu24.04",
            platform="linux/amd64",
        ),
        OciRequestIdentity(
            type="oci",
            role="uv-tool",
            repository="ghcr.io/astral-sh/uv",
            tag="0.11.28-debian-slim",
            platform="linux/amd64",
        ),
        ManagedPythonRequestIdentity(
            type="managed-python",
            version="3.13.14",
            implementation="cpython",
            platform="linux/amd64",
            libc="gnu",
            catalog_descriptor_digest=DIGEST_A,
        ),
        ComfyUIRequestIdentity(
            type="comfyui",
            repository="https://github.com/Comfy-Org/ComfyUI.git",
            selector="latest",
        ),
        ComfyUIRequirementsRequestIdentity(
            type="comfyui-requirements",
            repository="https://github.com/Comfy-Org/ComfyUI.git",
            commit=COMMIT,
            floor_commit=COMFYUI_FLOOR_COMMIT,
            path="requirements.txt",
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
            resolver_descriptor_digest=DIGEST_A,
            members=(
                DirectPythonRequestMember(
                    package="torch", extras=(), selector="==2.12.1"
                ),
            ),
        ),
    ]
    if application_extras:
        requests.append(
            DirectPythonRequestIdentity(
                type="python-group",
                environment="application",
                group="application-extra",
                python_version="3.13.14",
                platform="linux/amd64",
                index_url="https://pypi.org/simple",
                resolver_descriptor_digest=DIGEST_A,
                members=(
                    DirectPythonRequestMember(
                        package="numpy", extras=(), selector="<3,>=2"
                    ),
                    DirectPythonRequestMember(
                        package="pillow", extras=(), selector="<12,>=11"
                    ),
                ),
            )
        )
    return tuple(requests)


def _desired(*, application_extras: bool = False):
    return tuple(
        DesiredResolution(request)
        for request in _requests(application_extras=application_extras)
    )


@dataclass
class FakeAcquirer:
    generation: int = 0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def acquire(self, request, request_digest):
        desired = DesiredResolution(request)
        self.calls.append(desired.keys[0])
        if isinstance(request, OciRequestIdentity):
            common = dict(
                request_digest=request_digest,
                repository=request.repository,
                tag=request.tag,
                digest=DIGEST_B,
                kind="index",
                platform=request.platform,
            )
            entry = (
                CudaImageLockEntry(**common)
                if request.role == "cuda-base"
                else UvImageLockEntry(**common, observed_version="0.11.28")
            )
        elif isinstance(request, ManagedPythonRequestIdentity):
            entry = ManagedPythonLockEntry(
                request_digest=request_digest,
                version=request.version,
                platform=request.platform,
                libc=request.libc,
                catalog_digest=request.catalog_descriptor_digest,
                artifact_key="cpython-3.13.14-linux-x86_64-gnu",
                artifact_url="https://example.test/python.tar.zst",
            )
        elif isinstance(request, ComfyUIRequestIdentity):
            entry = OfficialComfyUILockEntry(
                request_digest=request_digest,
                repository=request.repository,
                commit=COMMIT,
                formal_release="0.11.0",
            )
        elif isinstance(request, ComfyUIRequirementsRequestIdentity):
            content = "torch\n"
            entry = ComfyUIRequirementsLockEntry(
                request_digest=request_digest,
                digest=(f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"),
                content=content,
            )
        elif isinstance(request, PyTorchRequestIdentity):
            entry = PyTorchLockEntry(
                request_digest=request_digest,
                setuptools_specifier="<82",
                packages=(
                    ResolvedPythonPackage(
                        name="torch", extras=(), version="2.12.1+cu130"
                    ),
                ),
            )
        elif isinstance(request, DirectPythonRequestIdentity):
            version = "2.4.1" if self.generation == 0 else "2.5.0"
            entry = ApplicationExtrasLockEntry(
                request_digest=request_digest,
                packages=(
                    ResolvedPythonPackage(name="numpy", extras=(), version=version),
                    ResolvedPythonPackage(name="pillow", extras=(), version="11.3.0"),
                ),
            )
        elif isinstance(request, ComfyCliRequestIdentity):
            entry = UvToolLockEntry(
                request_digest=request_digest,
                name="comfy-cli",
                extras=(),
                version="1.8.0",
            )
        else:  # pragma: no cover - exhaustive request set above
            raise AssertionError(type(request))
        return AcquiredCanonicalEntries((entry,), True)


@dataclass
class FakeLocalAcquirer:
    digest: str = DIGEST_A

    def acquire(self, request):
        return BuildHookLockEntry(
            relative_path=PurePosixPath(*request.canonical_path.parts[1:]).as_posix(),
            digest=self.digest,
        )


def _initial_lock(*, application_extras: bool = False, local: bool = False):
    acquirer = FakeAcquirer()
    kwargs = {}
    if local:
        kwargs = {
            "local_requests": (
                LocalExecutableIdentityRequest(
                    Path("/tmp/hooks"),
                    PurePosixPath("setup.sh"),
                    PurePosixPath("build-hooks/setup.sh"),
                ),
            ),
            "local_acquirer": FakeLocalAcquirer(),
        }
    accepted = reconcile_canonical_lock(
        _desired(application_extras=application_extras),
        existing=None,
        acquirer=acquirer,
        **kwargs,
    )
    return accepted.lock


# Default and upgrade reconciliation operate once per compatible atomic group
# and accurately report provider calls and deltas.
def test_default_reuses_every_compatible_atomic_group_without_provider_calls() -> None:
    existing = _initial_lock(application_extras=True)
    acquirer = FakeAcquirer()

    accepted = reconcile_canonical_lock(
        _desired(application_extras=True), existing=existing, acquirer=acquirer
    )

    assert accepted.lock == existing
    assert accepted.delta == ()
    assert accepted.provider_calls == ()
    assert acquirer.calls == []
    assert accepted.write_intent is False


def test_upgrade_refreshes_complete_application_extras_as_one_delta() -> None:
    existing = _initial_lock(application_extras=True)
    acquirer = FakeAcquirer(generation=1)

    accepted = reconcile_canonical_lock(
        _desired(application_extras=True),
        existing=existing,
        acquirer=acquirer,
        policy=LockPolicy.UPGRADE,
    )

    key = ("python", "package_groups", "application_extras")
    assert any(
        item.key == key and item.kind is DeltaKind.UPDATED for item in accepted.delta
    )
    assert accepted.provider_calls.count(key) == 1
    group = accepted.lock.python.package_groups.application_extras
    assert group is not None
    assert tuple(package.version for package in group.packages) == ("2.5.0", "11.3.0")


# Locked and non-apply modes never silently refresh identities or request a
# write; local drift fails before external provider work.
def test_locked_matching_lock_has_zero_provider_calls_and_no_write() -> None:
    existing = _initial_lock(local=True)
    acquirer = FakeAcquirer()

    accepted = reconcile_canonical_lock(
        _desired(),
        local_requests=(
            LocalExecutableIdentityRequest(
                Path("/tmp/hooks"),
                PurePosixPath("setup.sh"),
                PurePosixPath("build-hooks/setup.sh"),
            ),
        ),
        local_acquirer=FakeLocalAcquirer(),
        existing=existing,
        acquirer=acquirer,
        policy=LockPolicy.LOCKED,
    )

    assert accepted.lock == existing
    assert accepted.provider_calls == ()
    assert acquirer.calls == []
    assert accepted.write_intent is False
    assert accepted.local_reads == (("hooks", "build", "setup.sh"),)


def test_locked_hook_drift_fails_without_external_provider_calls() -> None:
    existing = _initial_lock(local=True)
    acquirer = FakeAcquirer()

    with pytest.raises(CanonicalResolutionError) as raised:
        reconcile_canonical_lock(
            _desired(),
            local_requests=(
                LocalExecutableIdentityRequest(
                    Path("/tmp/hooks"),
                    PurePosixPath("setup.sh"),
                    PurePosixPath("build-hooks/setup.sh"),
                ),
            ),
            local_acquirer=FakeLocalAcquirer(DIGEST_B),
            existing=existing,
            acquirer=acquirer,
            policy=LockPolicy.LOCKED,
        )

    assert acquirer.calls == []
    assert raised.value.diagnostics[0].message == (
        "locked identity is content changed; regenerate config.lock.toml"
    )


@pytest.mark.parametrize("purpose", [ReconcilePurpose.CHECK, ReconcilePurpose.DRY_RUN])
def test_non_apply_purposes_never_request_a_lock_write(purpose) -> None:
    accepted = reconcile_canonical_lock(
        _desired(), existing=None, acquirer=FakeAcquirer(), purpose=purpose
    )

    assert accepted.changed is True
    assert accepted.write_intent is False
