"""Provider adaptation into atomic grouped lock results."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from comfyui_docker_helper.config.canonical_lock import (
    BuildHookLockEntry,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectPythonRequestMember,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    PyTorchLockEntry,
    PyTorchRequestIdentity,
    RequirementsRoutingPolicy,
    UvImageLockEntry,
    compute_request_digest,
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


def _routing_policy() -> RequirementsRoutingPolicy:
    return RequirementsRoutingPolicy(
        revision=1,
        routed_names=("torch", "torchaudio", "torchvision"),
        syntax="pep508",
        markers="packaging-target-environment",
        normalization="pep503-names-pep508-extras",
        merge="specifier-intersection-extra-union",
        sources="reject-options-and-direct-urls",
    )


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


def test_requirements_provider_returns_nested_digest_and_pytorch_projection() -> None:
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository="https://github.com/Comfy-Org/ComfyUI.git",
        commit=COMMIT,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
        python_version="3.13.14",
        platform="linux/amd64",
        routing_policy=_routing_policy(),
    )
    acquirer = _acquirer(
        requirements_reader=lambda _request: b"torch\ntorchaudio>=2\nnumpy>=2\n"
    )

    acquired = acquirer.acquire(request, compute_request_digest(request))

    entry = acquired.entries[0]
    assert isinstance(entry, ComfyUIRequirementsLockEntry)
    assert entry.digest.startswith("sha256:")
    assert tuple(item.name for item in entry.pytorch) == ("torch", "torchaudio")
    assert entry.pytorch[1].specifier == ">=2"


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
