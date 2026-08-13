"""Provider adaptation into atomic grouped lock results."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path, PurePosixPath

import pytest

from comfyui_docker_helper.config.canonical_lock import (
    MAX_COMFYUI_REQUIREMENTS_BYTES,
    BuildHookLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectPythonRequestIdentity,
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
    DockerPythonGroupResolver,
    LocalExecutableEntryAcquirer,
    LocalFileEntryAcquirer,
    ProviderIdentityAcquirer,
    ResolvedPythonGroup,
    ResolvedPythonMember,
)
from comfyui_docker_helper.host.identity_providers import (
    LocalExecutableIdentity,
    ManagedPythonIdentity,
    OciIdentity,
)
from comfyui_docker_helper.host.uv_docker_executor import (
    UvDockerExecutorError,
    UvResolverResult,
)
from comfyui_docker_helper.local_executable import (
    LocalExecutableIdentityRequest,
)
from comfyui_docker_helper.local_file_identity import LocalFileIdentityRequest

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
COMMIT = "1" * 40
CONTROLLED_CLEANUP_ERROR = (
    "uv resolver cancelled; cleanup_incomplete: "
    "name=cdh-uv-resolver-test, "
    "label=comfyui-docker-helper.uv-operation=test"
)


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


class _UvExecutor:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.calls: list[tuple[object, object]] = []

    def execute(self, descriptor, operation):
        self.calls.append((descriptor, operation))
        return UvResolverResult(self.stdout, b"")


def _pylock(*packages: tuple[str, str]) -> bytes:
    rows = "".join(
        f'[[packages]]\nname = "{name}"\nversion = "{version}"\n'
        for name, version in packages
    )
    return rows.encode()


class _FailingUvExecutor:
    def execute(self, descriptor, operation):
        del descriptor, operation
        raise UvDockerExecutorError(CONTROLLED_CLEANUP_ERROR)


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
        repository="astral/uv",
        tag="0.11.28-debian-slim",
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
    ("content", "oversized_size"),
    [
        pytest.param(b"\xff", None, id="invalid-utf8"),
        pytest.param(
            None,
            MAX_COMFYUI_REQUIREMENTS_BYTES + 1,
            id="oversized",
        ),
    ],
)
def test_requirements_provider_rejects_invalid_source_as_acquisition_failure(
    content: bytes | None,
    oversized_size: int | None,
) -> None:
    if oversized_size is not None:
        content = b"x" * oversized_size
    assert content is not None
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
        resolver_descriptor_digest=DIGEST_A,
        members=(
            DirectPythonRequestMember(package="torch", extras=(), specifier="==2.12.1"),
            DirectPythonRequestMember(
                package="torchvision", extras=("image",), specifier="==0.27.1"
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


# The OCI adapter preserves each request's exact descriptor and existing result parser.
def test_docker_python_group_resolver_compiles_ordinary_and_comfy_cli_requests() -> (
    None
):
    ordinary_executor = _UvExecutor(_pylock(("packaging", "26.2")))
    ordinary = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST_A,
        members=(
            DirectPythonRequestMember(
                package="packaging", extras=(), specifier="==26.2"
            ),
        ),
    )

    resolved = DockerPythonGroupResolver(ordinary_executor).resolve(ordinary)

    assert [(item.package, item.version) for item in resolved.members] == [
        ("packaging", "26.2")
    ]
    assert ordinary_executor.calls[0][0].digest == DIGEST_A
    assert ordinary_executor.calls[0][1].index_url == "https://pypi.org/simple"

    cli_executor = _UvExecutor(_pylock(("comfy-cli", "1.8.0")))
    cli = ComfyCliRequestIdentity(
        type="comfy-cli",
        package="comfy-cli",
        policy="highest-target-compatible-stable",
        minimum_version="1.7.0",
        environment="uv-tool:comfy-cli",
        index_url="https://pypi.org/simple",
        python_version="3.13.14",
        platform="linux/amd64",
        resolver_descriptor_digest=DIGEST_B,
    )

    cli_result = DockerPythonGroupResolver(cli_executor).resolve(cli)

    assert [(item.package, item.version) for item in cli_result.members] == [
        ("comfy-cli", "1.8.0")
    ]
    assert cli_executor.calls[0][0].digest == DIGEST_B


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("", b"demo\n"),
        (">=1", b"demo>=1\n"),
        ("~=1.2", b"demo~=1.2\n"),
    ],
)
def test_direct_python_resolver_preserves_name_based_requirement_text(
    selector: str,
    expected: bytes,
) -> None:
    executor = _UvExecutor(_pylock(("demo", "1.5.0")))
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST_A,
        members=(
            DirectPythonRequestMember(
                package="demo",
                extras=(),
                specifier=selector,
            ),
        ),
    )

    result = DockerPythonGroupResolver(executor).resolve(request)

    assert [(item.package, item.version) for item in result.members] == [
        ("demo", "1.5.0")
    ]
    assert executor.calls[0][1].requirements == expected


@pytest.mark.parametrize(
    "source",
    [
        "https://example.test/opaque-artifact.whl#sha256=abc",
        "git+https://example.test/demo.git@main#subdirectory=package",
    ],
)
def test_direct_python_resolver_renders_complete_direct_reference(
    source: str,
) -> None:
    executor = _UvExecutor(
        b"""
[[packages]]
name = "demo"
version = "1.0rc1"
source = { vcs = "unconsumed", precise = "unconsumed" }
"""
    )
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST_A,
        members=(
            DirectPythonRequestMember(
                package="demo",
                extras=("cli",),
                specifier="",
                direct_reference=source,
            ),
        ),
    )

    result = DockerPythonGroupResolver(executor).resolve(request)

    assert result.members == (ResolvedPythonMember("demo", "1.0rc1"),)
    assert executor.calls[0][1].requirements == f"demo[cli] @ {source}\n".encode()


def test_generic_pylock_ignores_transitive_and_unconsumed_fields() -> None:
    executor = _UvExecutor(
        b"""
[[packages]]
name = "demo"
version = "1.0+cu130"
wheels = "unconsumed"
source = { arbitrary = true }

[[packages]]
name = "transitive"
unconsumed = true
"""
    )
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST_A,
        members=(DirectPythonRequestMember(package="demo", extras=(), specifier=""),),
    )

    result = DockerPythonGroupResolver(executor).resolve(request)

    assert result.members == (ResolvedPythonMember("demo", "1.0+cu130"),)


@pytest.mark.parametrize(
    "output",
    [
        b"invalid = [",
        _pylock(("transitive", "1.0")),
        _pylock(("demo", "1.0"), ("Demo", "1.0")),
        _pylock(("demo", "1.0RC1")),
    ],
)
def test_generic_pylock_rejects_malformed_requested_results(output: bytes) -> None:
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST_A,
        members=(DirectPythonRequestMember(package="demo", extras=(), specifier=""),),
    )

    with pytest.raises(
        CanonicalAcquisitionError,
        match="Python resolver returned invalid package metadata",
    ):
        DockerPythonGroupResolver(_UvExecutor(output)).resolve(request)


@pytest.mark.parametrize(
    ("environment", "group", "package", "selector"),
    [
        ("application", "application-extra", "packaging", "==26.2"),
        ("uv-tool:ruff", "uv-tool", "ruff", "==0.15.18"),
    ],
)
def test_direct_python_groups_preserve_controlled_executor_cleanup_identity(
    environment: str,
    group: str,
    package: str,
    selector: str,
) -> None:
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment=environment,
        group=group,
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST_A,
        members=(
            DirectPythonRequestMember(package=package, extras=(), specifier=selector),
        ),
    )

    with pytest.raises(CanonicalAcquisitionError) as raised:
        DockerPythonGroupResolver(_FailingUvExecutor()).resolve(request)

    assert str(raised.value) == (
        f"Python group resolution failed: {CONTROLLED_CLEANUP_ERROR}"
    )


def test_comfy_cli_preserves_controlled_executor_cleanup_identity() -> None:
    request = ComfyCliRequestIdentity(
        type="comfy-cli",
        package="comfy-cli",
        policy="highest-target-compatible-stable",
        minimum_version="1.7.0",
        environment="uv-tool:comfy-cli",
        index_url="https://pypi.org/simple",
        python_version="3.13.14",
        platform="linux/amd64",
        resolver_descriptor_digest=DIGEST_A,
    )

    with pytest.raises(CanonicalAcquisitionError) as raised:
        DockerPythonGroupResolver(_FailingUvExecutor()).resolve(request)

    assert str(raised.value) == (
        f"Python group resolution failed: {CONTROLLED_CLEANUP_ERROR}"
    )


def test_docker_python_group_resolver_preserves_pytorch_routing_and_metadata() -> None:
    executor = _UvExecutor(
        b"""
[[packages]]
name = "torch"
version = "2.12.1+cu130"
wheels = [{ url = "https://download.pytorch.org/whl/cu130/torch.whl" }]

[[packages]]
name = "torchvision"
version = "0.27.1+cu130"
wheels = [{ url = "https://download.pytorch.org/whl/cu130/torchvision.whl" }]

[[packages]]
name = "sageattention"
version = "2.2.0+cu130"
source = { url = "unconsumed" }
"""
    )
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
        resolver_descriptor_digest=DIGEST_A,
        members=(
            DirectPythonRequestMember(package="torch", extras=(), specifier="==2.12.1"),
            DirectPythonRequestMember(
                package="torchvision", extras=(), specifier="==0.27.1"
            ),
            DirectPythonRequestMember(
                package="sageattention",
                extras=(),
                specifier="",
                direct_reference="https://example.test/sageattention.whl",
            ),
        ),
    )
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: torch\n"
        "Version: 2.12.1+cu130\n"
        "Requires-Dist: setuptools<82\n"
    )

    metadata_urls = []

    def read_metadata(url: str) -> str:
        metadata_urls.append(url)
        return metadata

    resolved = DockerPythonGroupResolver(
        executor, metadata_reader=read_metadata
    ).resolve(request)

    assert {item.package: item.version for item in resolved.members} == {
        "sageattention": "2.2.0+cu130",
        "torch": "2.12.1+cu130",
        "torchvision": "0.27.1+cu130",
    }
    assert resolved.setuptools_specifier == "<82"
    assert executor.calls[0][0].digest == DIGEST_A
    manifest = tomllib.loads(executor.calls[0][1].pyproject.decode())
    assert (
        "sageattention @ https://example.test/sageattention.whl"
        in manifest["project"]["dependencies"]
    )
    assert "sageattention" not in manifest["tool"]["uv"]["sources"]
    assert metadata_urls == [
        "https://download.pytorch.org/whl/cu130/torch.whl.metadata"
    ]


def test_pytorch_pylock_rejects_non_table_consumed_wheel() -> None:
    executor = _UvExecutor(
        b"""
[[packages]]
name = "torch"
version = "2.12.1+cu130"
wheels = ["not-a-table"]
"""
    )
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
        resolver_descriptor_digest=DIGEST_A,
        members=(
            DirectPythonRequestMember(package="torch", extras=(), specifier="==2.12.1"),
        ),
    )

    with pytest.raises(
        CanonicalAcquisitionError,
        match="resolver did not select one exact torch wheel",
    ):
        DockerPythonGroupResolver(executor).resolve(request)


def test_pytorch_preserves_controlled_executor_cleanup_identity() -> None:
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
        resolver_descriptor_digest=DIGEST_A,
        members=(
            DirectPythonRequestMember(package="torch", extras=(), specifier="==2.12.1"),
        ),
    )

    with pytest.raises(CanonicalAcquisitionError) as raised:
        DockerPythonGroupResolver(_FailingUvExecutor()).resolve(request)

    message = str(raised.value)
    assert f"PyTorch resolution failed: {CONTROLLED_CLEANUP_ERROR}" in message
    assert "packages [torch]" in message
    assert "channel cu130" in message


def test_controlled_cleanup_identity_reaches_final_reconciliation_diagnostic() -> None:
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST_A,
        members=(
            DirectPythonRequestMember(
                package="packaging", extras=(), specifier="==26.2"
            ),
        ),
    )
    acquirer = _acquirer(python_group=DockerPythonGroupResolver(_FailingUvExecutor()))

    with pytest.raises(CanonicalResolutionError) as raised:
        reconcile_canonical_lock(
            (DesiredResolution(request),),
            existing=None,
            acquirer=acquirer,
        )

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "lock.resolve_failed"
    assert diagnostic.message == (
        f"Python group resolution failed: {CONTROLLED_CLEANUP_ERROR}"
    )


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


def test_local_file_acquisition_streams_current_content_into_target_key(
    tmp_path: Path,
) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"model-content")
    request = LocalFileIdentityRequest(
        source,
        PurePosixPath("models/model.bin"),
    )

    entry = LocalFileEntryAcquirer().acquire(request)

    assert entry.relative_target == "models/model.bin"
    assert entry.digest == (f"sha256:{hashlib.sha256(b'model-content').hexdigest()}")
