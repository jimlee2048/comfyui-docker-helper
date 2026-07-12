"""Focused provider adapter and exact uv group resolver contracts."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from comfyui_docker_helper.config.canonical_lock import (
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    DirectGitRequestIdentity,
    DirectPythonRequestMember,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    PythonGroupRequestIdentity,
    PyTorchRequestIdentity,
    RegistryRequestIdentity,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_resolver import CanonicalAcquisitionError
from comfyui_docker_helper.host.canonical_acquisition import (
    ManagedPythonReleaseInputs,
    ProviderIdentityAcquirer,
    ResolvedPythonMember,
    UvPythonGroupResolver,
)
from comfyui_docker_helper.host.identity_providers import (
    ComfyCliIdentity,
    DirectGitIdentity,
    DirectGitIdentityRequest,
    ManagedPythonIdentity,
    OciIdentity,
    OfficialComfyUIIdentity,
    OfficialComfyUIIdentityRequest,
    RegistryNodeIdentity,
)
from comfyui_docker_helper.host.uv_runner import HostUvRunner

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40
OFFICIAL_REPOSITORY = "https://github.com/comfyanonymous/ComfyUI.git"


def _group() -> PyTorchRequestIdentity:
    return PyTorchRequestIdentity(
        type="pytorch-group",
        environment="application",
        group="pytorch",
        backend="cuda",
        channel="cu130",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://download.pytorch.org/whl/cu130",
        members=[
            DirectPythonRequestMember(package="torch", extras=[], selector="==2.12.1"),
            DirectPythonRequestMember(
                package="torchvision", extras=["image"], selector="<0.28,>=0.27"
            ),
        ],
    )


def test_uv_group_resolver_uses_one_absolute_isolated_explicit_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setenv("UV_INDEX_URL", "https://must-not-leak.test")
    monkeypatch.setenv("PIP_CONFIG_FILE", "/must/not/leak")

    def runner(
        args: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "nvidia-cublas-cu13==13.1.0\n"
                "torch==2.12.1+cu130\n"
                "torchvision[image]==0.27.1+cu130\n"
            ),
        )

    resolver = UvPythonGroupResolver(HostUvRunner(Path("/opt/cdh/bin/uv")), runner)
    result = resolver.resolve(_group())

    assert result == (
        ResolvedPythonMember("torch", "2.12.1+cu130"),
        ResolvedPythonMember("torchvision", "0.27.1+cu130"),
    )
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:4] == ("/opt/cdh/bin/uv", "--no-config", "pip", "compile")
    assert argv[argv.index("--python-version") + 1] == "3.13.14"
    assert argv[argv.index("--python-platform") + 1] == "x86_64-unknown-linux-gnu"
    assert argv[argv.index("--default-index") + 1].endswith("/cu130")
    assert kwargs["input"] == ("torch==2.12.1\ntorchvision[image]<0.28,>=0.27\n")
    assert kwargs["cwd"] == "/"
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["UV_NO_CONFIG"] == "1"
    assert "UV_INDEX_URL" not in environment
    assert "PIP_CONFIG_FILE" not in environment


@pytest.mark.parametrize(
    ("stdout", "returncode", "message"),
    [
        ("torch==2.12.1\n", 0, "omitted"),
        ("torch>=2\ntorchvision==0.27.1\n", 0, "invalid data"),
        ("torch===\n", 0, "invalid data"),
        ("torch==2.12.1rc1+cu130\ntorchvision==0.27.1\n", 0, "invalid data"),
        (
            "torch==2.12.1\ntorchvision==0.27.1+cu130\n",
            0,
            "incompatible PyTorch channel",
        ),
        (
            "torch==2.12.1+cpu\ntorchvision==0.27.1+cu130\n",
            0,
            "incompatible PyTorch channel",
        ),
        (
            "torch==2.12.1+cu129\ntorchvision==0.27.1+cu130\n",
            0,
            "incompatible PyTorch channel",
        ),
        (
            "torch==2.12.1+cu130\ntorchvision==0.27.1\n",
            0,
            "incompatible PyTorch channel",
        ),
        (
            "torch==2.12.1+cu130\ntorchvision==0.27.1+cu129\n",
            0,
            "incompatible PyTorch channel",
        ),
        ("", 2, "resolution failed"),
    ],
)
def test_uv_group_resolver_rejects_failed_or_incomplete_output(
    stdout: str, returncode: int, message: str
) -> None:
    def runner(
        args: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode, stdout=stdout)

    resolver = UvPythonGroupResolver(HostUvRunner(Path("/opt/cdh/bin/uv")), runner)

    with pytest.raises(CanonicalAcquisitionError, match=message):
        resolver.resolve(_group())


def test_pytorch_group_does_not_interpret_arbitrary_extra_local_label() -> None:
    request = PyTorchRequestIdentity.model_validate(
        {
            **_group().model_dump(),
            "members": [
                *_group().members,
                DirectPythonRequestMember(
                    package="custom-extra", extras=[], selector="==1.0"
                ),
            ],
        }
    )

    def runner(
        args: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "custom-extra==1.0+vendor.1\n"
                "torch==2.12.1+cu130\n"
                "torchvision==0.27.1+cu130\n"
            ),
        )

    resolved = UvPythonGroupResolver(
        HostUvRunner(Path("/opt/cdh/bin/uv")), runner
    ).resolve(request)

    assert resolved[0] == ResolvedPythonMember("custom-extra", "1.0+vendor.1")


@dataclass
class FakeOci:
    calls: list[object] = field(default_factory=list)

    def resolve(self, request: object) -> OciIdentity:
        self.calls.append(request)
        return OciIdentity(
            "cuda-base",
            "nvidia/cuda",
            "13.0.3-cudnn-devel-ubuntu24.04",
            DIGEST_A,
            "index",
            "linux/amd64",
        )


@dataclass
class FakeManagedPython:
    calls: list[object] = field(default_factory=list)

    def resolve(self, request: object) -> ManagedPythonIdentity:
        self.calls.append(request)
        return ManagedPythonIdentity(
            "3.13.14",
            "cpython",
            "linux/amd64",
            "gnu",
            "uv-managed",
            DIGEST_B,
            "cpython-3.13.14-linux-x86_64-gnu",
            "https://example.test/python.tar.zst",
        )


@dataclass
class FakeComfyUI:
    resolve_calls: list[OfficialComfyUIIdentityRequest] = field(default_factory=list)
    list_calls: list[str] = field(default_factory=list)

    def resolve(
        self, request: OfficialComfyUIIdentityRequest
    ) -> OfficialComfyUIIdentity:
        self.resolve_calls.append(request)
        ref = request.ref
        return OfficialComfyUIIdentity(
            OFFICIAL_REPOSITORY,
            COMMIT_A,
            ref.removeprefix("refs/tags/v") if ref.startswith("refs/tags/v") else None,
        )

    def list_releases(self, repository: str) -> tuple[OfficialComfyUIIdentity, ...]:
        self.list_calls.append(repository)
        return (
            OfficialComfyUIIdentity(repository, COMMIT_A, "0.4.0"),
            OfficialComfyUIIdentity(repository, COMMIT_B, "0.5.0"),
        )


@dataclass
class FakeComfyCli:
    list_calls: int = 0

    def list_versions(self) -> tuple[ComfyCliIdentity, ...]:
        self.list_calls += 1
        return (
            ComfyCliIdentity("comfy-cli", "1.0.0"),
            ComfyCliIdentity("comfy-cli", "2.0.0"),
        )

    def resolve(self, request: object) -> ComfyCliIdentity:
        raise AssertionError("exact comfy-cli must be a direct result")


@dataclass
class FakeRegistry:
    resolve_calls: list[object] = field(default_factory=list)
    list_calls: list[str] = field(default_factory=list)

    def resolve(self, request: object) -> RegistryNodeIdentity:
        self.resolve_calls.append(request)
        return RegistryNodeIdentity("registry", "example-node", "1.2.3")

    def list_versions(self, node_id: str) -> tuple[RegistryNodeIdentity, ...]:
        self.list_calls.append(node_id)
        return (
            RegistryNodeIdentity("registry", node_id, "1.0.0"),
            RegistryNodeIdentity("registry", node_id, "2.0.0"),
        )


@dataclass
class FakeGit:
    calls: list[DirectGitIdentityRequest] = field(default_factory=list)

    def resolve(self, request: DirectGitIdentityRequest) -> DirectGitIdentity:
        self.calls.append(request)
        return DirectGitIdentity("git", request.url, COMMIT_B)


@dataclass
class FakePythonGroup:
    calls: list[PythonGroupRequestIdentity] = field(default_factory=list)

    def resolve(
        self, request: PythonGroupRequestIdentity
    ) -> tuple[ResolvedPythonMember, ...]:
        self.calls.append(request)
        return (
            ResolvedPythonMember("torch", "2.12.1+cu130"),
            ResolvedPythonMember("torchvision", "0.27.1+cu130"),
        )


@dataclass
class ProviderFakes:
    oci: FakeOci = field(default_factory=FakeOci)
    python: FakeManagedPython = field(default_factory=FakeManagedPython)
    comfyui: FakeComfyUI = field(default_factory=FakeComfyUI)
    cli: FakeComfyCli = field(default_factory=FakeComfyCli)
    registry: FakeRegistry = field(default_factory=FakeRegistry)
    git: FakeGit = field(default_factory=FakeGit)
    group: FakePythonGroup = field(default_factory=FakePythonGroup)

    def acquirer(self) -> ProviderIdentityAcquirer:
        return ProviderIdentityAcquirer(
            self.oci,
            self.python,
            self.comfyui,
            self.cli,
            self.registry,
            self.git,
            self.group,
            ManagedPythonReleaseInputs(
                "26.1.2",
                "83.0.0",
                "0.47.0",
                "0.5.0",
                DIGEST_A,
                "0.11.28",
            ),
        )


def _acquire(request: Any, fakes: ProviderFakes) -> tuple[object, ...]:
    return fakes.acquirer().acquire(request, compute_request_digest(request)).entries


def test_adapter_converts_oci_managed_python_and_complete_group() -> None:
    fakes = ProviderFakes()
    oci = OciRequestIdentity(
        type="oci",
        role="cuda-base",
        repository="nvidia/cuda",
        tag="13.0.3-cudnn-devel-ubuntu24.04",
        platform="linux/amd64",
    )
    python = ManagedPythonRequestIdentity(
        type="managed-python",
        version="3.13.14",
        implementation="cpython",
        platform="linux/amd64",
        libc="gnu",
        catalog_descriptor_digest=DIGEST_B,
    )

    oci_entry = _acquire(oci, fakes)[0]
    python_entry = _acquire(python, fakes)[0]
    group_entries = _acquire(_group(), fakes)

    assert oci_entry.descriptor_digest == DIGEST_A
    assert python_entry.pip_version == "26.1.2"
    assert python_entry.cdh_source_digest == DIGEST_A
    assert [entry.package for entry in group_entries] == ["torch", "torchvision"]
    assert len(fakes.group.calls) == 1


def test_adapter_rejects_provider_identity_that_does_not_match_request() -> None:
    class MismatchedOci(FakeOci):
        def resolve(self, request: object) -> OciIdentity:
            return OciIdentity(
                "cuda-base",
                "other/image",
                "13.0.3-cudnn-devel-ubuntu24.04",
                DIGEST_A,
                "index",
                "linux/amd64",
            )

    fakes = ProviderFakes(oci=MismatchedOci())
    request = OciRequestIdentity(
        type="oci",
        role="cuda-base",
        repository="nvidia/cuda",
        tag="13.0.3-cudnn-devel-ubuntu24.04",
        platform="linux/amd64",
    )

    with pytest.raises(CanonicalAcquisitionError, match="incompatible data"):
        _acquire(request, fakes)


def test_exact_git_and_comfy_cli_are_direct_while_registry_is_verified() -> None:
    fakes = ProviderFakes()
    git = DirectGitRequestIdentity(
        type="git", url="https://example.test/node.git", ref=COMMIT_A
    )
    cli = ComfyCliRequestIdentity(
        type="comfy-cli",
        package="comfy-cli",
        selector="1.5.3",
        index_url="https://pypi.org/simple",
        python_version="3.13.14",
        platform="linux/amd64",
    )
    registry = RegistryRequestIdentity(
        type="registry", id="example-node", selector="1.2.3"
    )

    assert _acquire(git, fakes)[0].commit == COMMIT_A
    assert _acquire(cli, fakes)[0].version == "1.5.3"
    assert _acquire(registry, fakes)[0].version == "1.2.3"
    assert fakes.git.calls == []
    assert fakes.cli.list_calls == 0
    assert len(fakes.registry.resolve_calls) == 1


def test_comfyui_exact_release_maps_to_canonical_tag_ref() -> None:
    fakes = ProviderFakes()
    request = ComfyUIRequestIdentity(
        type="comfyui", repository=OFFICIAL_REPOSITORY, selector="0.4.0"
    )

    entry = _acquire(request, fakes)[0]

    assert entry.formal_release == "0.4.0"
    assert fakes.comfyui.resolve_calls[0].ref == "refs/tags/v0.4.0"


def test_comfyui_exact_release_rejects_mismatched_provider_release() -> None:
    class MismatchedComfyUI(FakeComfyUI):
        def resolve(
            self, request: OfficialComfyUIIdentityRequest
        ) -> OfficialComfyUIIdentity:
            return OfficialComfyUIIdentity(OFFICIAL_REPOSITORY, COMMIT_A, "0.5.0")

    fakes = ProviderFakes(comfyui=MismatchedComfyUI())
    request = ComfyUIRequestIdentity(
        type="comfyui", repository=OFFICIAL_REPOSITORY, selector="0.4.0"
    )

    with pytest.raises(CanonicalAcquisitionError, match="incompatible data"):
        _acquire(request, fakes)


def test_registry_exact_rejects_mismatched_provider_version() -> None:
    class MismatchedRegistry(FakeRegistry):
        def resolve(self, request: object) -> RegistryNodeIdentity:
            return RegistryNodeIdentity("registry", "example-node", "2.0.0")

    fakes = ProviderFakes(registry=MismatchedRegistry())
    request = RegistryRequestIdentity(
        type="registry", id="example-node", selector="1.2.3"
    )

    with pytest.raises(CanonicalAcquisitionError, match="incompatible data"):
        _acquire(request, fakes)


def test_python_group_rejects_version_outside_member_selector() -> None:
    class MismatchedPythonGroup(FakePythonGroup):
        def resolve(
            self, request: PythonGroupRequestIdentity
        ) -> tuple[ResolvedPythonMember, ...]:
            return (
                ResolvedPythonMember("torch", "2.11.0+cu130"),
                ResolvedPythonMember("torchvision", "0.27.1+cu130"),
            )

    fakes = ProviderFakes(group=MismatchedPythonGroup())

    with pytest.raises(CanonicalAcquisitionError, match="incompatible data"):
        _acquire(_group(), fakes)


def test_moving_catalog_selectors_choose_highest_stable_match() -> None:
    fakes = ProviderFakes()
    comfyui = ComfyUIRequestIdentity(
        type="comfyui", repository=OFFICIAL_REPOSITORY, selector="<0.6,>=0.4"
    )
    cli = ComfyCliRequestIdentity(
        type="comfy-cli",
        package="comfy-cli",
        selector="<3,>=1",
        index_url="https://pypi.org/simple",
        python_version="3.13.14",
        platform="linux/amd64",
    )
    registry = RegistryRequestIdentity(
        type="registry", id="example-node", selector="<3,>=1"
    )

    assert _acquire(comfyui, fakes)[0].formal_release == "0.5.0"
    assert _acquire(cli, fakes)[0].version == "2.0.0"
    assert _acquire(registry, fakes)[0].version == "2.0.0"


def test_symbolic_git_and_nightly_use_provider_once() -> None:
    fakes = ProviderFakes()
    git = DirectGitRequestIdentity(
        type="git", url="https://example.test/node.git", ref="main"
    )
    nightly = ComfyUIRequestIdentity(
        type="comfyui", repository=OFFICIAL_REPOSITORY, selector="nightly"
    )

    assert _acquire(git, fakes)[0].commit == COMMIT_B
    assert _acquire(nightly, fakes)[0].formal_release is None
    assert len(fakes.git.calls) == 1
    assert fakes.comfyui.resolve_calls[0].ref == "HEAD"
