"""Provider adaptation into atomic grouped lock results."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import pytest

from comfyui_docker_helper.config.canonical_lock import (
    MAX_COMFYUI_REQUIREMENTS_BYTES,
    BuildHookLockEntry,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectPythonRequestMember,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    PyTorchLockEntry,
    PyTorchRequestIdentity,
    UvImageLockEntry,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_request import DesiredResolution
from comfyui_docker_helper.config.canonical_resolver import (
    CanonicalAcquisitionError,
    CanonicalResolutionError,
    reconcile_canonical_lock,
)
from comfyui_docker_helper.exact_ledger import COMFYUI_FLOOR_COMMIT
from comfyui_docker_helper.host.canonical_acquisition import (
    LocalExecutableEntryAcquirer,
    ProviderIdentityAcquirer,
    ResolvedPythonGroup,
    ResolvedPythonMember,
)
from comfyui_docker_helper.host.identity_providers import (
    LocalExecutableIdentity,
    LocalExecutableIdentityRequest,
    ManagedPythonIdentity,
    OciIdentity,
)

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
COMMIT = "1" * 40


class _Unused:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected provider call: {name}")


class _Oci:
    def resolve(self, request):
        return OciIdentity(
            role=request.role,
            repository=request.repository,
            tag=request.tag,
            descriptor_digest=DIGEST_B,
            descriptor_kind="index",
            platform=request.platform,
            resolved_version="0.11.28",
        )


class _ManagedPython:
    def resolve(self, request):
        return ManagedPythonIdentity(
            version=request.version,
            implementation=request.implementation,
            platform=request.platform,
            libc=request.libc,
            provider="uv-managed",
            catalog_descriptor_digest=request.catalog_descriptor_digest,
            catalog_key="cpython-3.13.14-linux-x86_64-gnu",
            catalog_url="https://example.test/python.tar.zst",
        )


class _ComfyUI:
    def is_ancestor(self, repository, ancestor, descendant):
        return True


class _PythonGroup:
    def resolve(self, request):
        return ResolvedPythonGroup(
            tuple(
                ResolvedPythonMember(
                    member.package,
                    "2.12.1+cu130" if member.package == "torch" else "0.27.1+cu130",
                )
                for member in request.members
            ),
            "<82",
        )


def _acquirer(**changes):
    values = {
        "oci": _Oci(),
        "managed_python": _ManagedPython(),
        "comfyui": _ComfyUI(),
        "registry": _Unused(),
        "git": _Unused(),
        "python_group": _PythonGroup(),
    }
    values.update(changes)
    return ProviderIdentityAcquirer(**values)


# Fixed image and managed-Python providers return only their owned external
# artifact identities.
def test_uv_provider_returns_one_fixed_image_domain_result() -> None:
    request = OciRequestIdentity(
        type="oci",
        role="uv-tool",
        repository="ghcr.io/astral-sh/uv",
        tag="0.11.28",
        platform="linux/amd64",
    )

    acquired = _acquirer().acquire(request, compute_request_digest(request))

    assert len(acquired.entries) == 1
    entry = acquired.entries[0]
    assert isinstance(entry, UvImageLockEntry)
    assert entry.digest == DIGEST_B
    assert entry.kind == "index"
    assert entry.observed_version == "0.11.28"


def test_managed_python_result_has_only_provider_owned_artifact_identity() -> None:
    request = ManagedPythonRequestIdentity(
        type="managed-python",
        version="3.13.14",
        implementation="cpython",
        platform="linux/amd64",
        libc="gnu",
        catalog_descriptor_digest=DIGEST_A,
    )

    acquired = _acquirer().acquire(request, compute_request_digest(request))

    entry = acquired.entries[0]
    assert isinstance(entry, ManagedPythonLockEntry)
    assert entry.catalog_digest == DIGEST_A
    assert entry.artifact_key == "cpython-3.13.14-linux-x86_64-gnu"


# Requirements acquisition preserves the exact upstream source bytes; local
# planning owns target projection separately.
def test_requirements_provider_returns_exact_source_snapshot() -> None:
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository="https://github.com/Comfy-Org/ComfyUI.git",
        commit=COMMIT,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
    )
    content = b"torch\r\ntorchaudio>=2  \r\nnumpy>=2"
    acquirer = _acquirer(requirements_reader=lambda _request: content)

    acquired = acquirer.acquire(request, compute_request_digest(request))

    entry = acquired.entries[0]
    assert isinstance(entry, ComfyUIRequirementsLockEntry)
    assert entry.digest == f"sha256:{hashlib.sha256(content).hexdigest()}"
    assert entry.content.encode("utf-8") == content


@pytest.mark.parametrize(
    "content",
    [b"\xff", b"x" * (MAX_COMFYUI_REQUIREMENTS_BYTES + 1)],
)
def test_requirements_provider_rejects_invalid_source_as_acquisition_failure(
    content: bytes,
) -> None:
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository="https://github.com/Comfy-Org/ComfyUI.git",
        commit=COMMIT,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
    )

    with pytest.raises(CanonicalAcquisitionError):
        _acquirer(requirements_reader=lambda _request: content).acquire(
            request, compute_request_digest(request)
        )


def test_invalid_provider_source_surfaces_as_lock_resolution_failure() -> None:
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository="https://github.com/Comfy-Org/ComfyUI.git",
        commit=COMMIT,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
    )

    with pytest.raises(CanonicalResolutionError) as raised:
        reconcile_canonical_lock(
            (DesiredResolution(request),),
            existing=None,
            acquirer=_acquirer(requirements_reader=lambda _request: b"\xff"),
        )

    assert raised.value.diagnostics[0].code == "lock.resolve_failed"


def test_requirements_provider_rejects_non_bytes_without_leaking_type_errors() -> None:
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository="https://github.com/Comfy-Org/ComfyUI.git",
        commit=COMMIT,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
    )

    with pytest.raises(
        CanonicalAcquisitionError,
        match="provider returned invalid content",
    ):
        _acquirer(requirements_reader=lambda _request: "torch\n").acquire(
            request, compute_request_digest(request)
        )


def test_pytorch_provider_returns_one_complete_atomic_group() -> None:
    request = PyTorchRequestIdentity(
        type="pytorch-group",
        environment="application",
        group="pytorch",
        backend="cuda",
        channel="cu130",
        python_version="3.13.14",
        platform="linux/amd64",
        python_index_url="https://pypi.org/simple",
        pytorch_index_url="https://download.pytorch.org/whl/cu130",
        members=(
            DirectPythonRequestMember(package="torch", extras=(), selector="==2.12.1"),
            DirectPythonRequestMember(
                package="torchvision", extras=("image",), selector="==0.27.1"
            ),
        ),
    )

    acquired = _acquirer().acquire(request, compute_request_digest(request))

    assert len(acquired.entries) == 1
    entry = acquired.entries[0]
    assert isinstance(entry, PyTorchLockEntry)
    assert tuple(package.name for package in entry.packages) == (
        "torch",
        "torchvision",
    )
    assert entry.setuptools_specifier == "<82"


# Local hook acquisition removes the host-only prefix and retains typed content
# identity for canonical locking.
class _Local:
    def resolve(self, request):
        return LocalExecutableIdentity(request.canonical_path, DIGEST_A)


def test_local_hook_acquisition_returns_typed_tree_row_without_prefix() -> None:
    request = LocalExecutableIdentityRequest(
        Path("/tmp/hooks"),
        PurePosixPath("common/setup.sh"),
        PurePosixPath("build-hooks/common/setup.sh"),
    )

    entry = LocalExecutableEntryAcquirer(_Local()).acquire(request)

    assert isinstance(entry, BuildHookLockEntry)
    assert entry.relative_path == "common/setup.sh"
    assert entry.digest == DIGEST_A
