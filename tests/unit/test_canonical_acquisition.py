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
    ComfyUIRequirementsRequestIdentity,
    DirectGitRequestIdentity,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    PythonGroupRequestIdentity,
    PyTorchRequestIdentity,
    RegistryRequestIdentity,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_resolver import CanonicalAcquisitionError
from comfyui_docker_helper.exact_ledger import COMFYUI_FLOOR_COMMIT
from comfyui_docker_helper.host.canonical_acquisition import (
    ManagedPythonReleaseInputs,
    ProviderIdentityAcquirer,
    ResolvedPythonGroup,
    ResolvedPythonMember,
    UvPythonGroupResolver,
    _setuptools_specifier_from_metadata,
    _target_marker_environment,
)
from comfyui_docker_helper.host.identity_providers import (
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
        python_index_url="https://pypi.org/simple",
        pytorch_index_url="https://download.pytorch.org/whl/cu130",
        members=[
            DirectPythonRequestMember(package="torch", extras=[], selector="==2.12.1"),
            DirectPythonRequestMember(
                package="torchvision", extras=["image"], selector="<0.28,>=0.27"
            ),
        ],
    )


def _pylock(*packages: tuple[str, str]) -> str:
    sections = ['lock-version = "1.0"', 'created-by = "uv"']
    for name, version in packages:
        sections.extend(
            (
                "[[packages]]",
                f'name = "{name}"',
                f'version = "{version}"',
            )
        )
        if name == "torch":
            sections.append(
                'wheels = [{ url = "https://download.pytorch.org/whl/cu130/'
                'torch.whl" }]'
            )
    return "\n".join(sections) + "\n"


_TORCH_METADATA = "\n".join(
    (
        "Metadata-Version: 2.4",
        "Name: torch",
        "Version: 2.12.1+cu130",
        "Requires-Dist: setuptools<82",
        "",
    )
)


# Acquisition adapters preserve exact target/source identity and fail closed on drift.
def test_torch_metadata_markers_use_deterministic_linux_amd64_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    metadata = _TORCH_METADATA.replace(
        "Requires-Dist: setuptools<82",
        'Requires-Dist: setuptools<82; platform_machine == "x86_64" '
        'and sys_platform == "linux" and python_version >= "3.12"',
    )

    assert _target_marker_environment("3.13.14")["platform_machine"] == "x86_64"
    assert (
        _setuptools_specifier_from_metadata(
            metadata,
            python_version="3.13.14",
            expected_torch_version="2.12.1+cu130",
        )
        == "<82"
    )


@pytest.mark.parametrize(
    "metadata",
    [
        _TORCH_METADATA.replace("Name: torch", "Name: torch\nName: other"),
        _TORCH_METADATA.replace(
            "Version: 2.12.1+cu130",
            "Version: 2.12.1+cu130\nVersion: 2.12.1+cu130",
        ),
        _TORCH_METADATA.replace("Metadata-Version: 2.4\n", ""),
        _TORCH_METADATA.replace("setuptools<82", "setuptools=>82"),
        "Metadata-Version: 2.4\n bad-continuation\nName: torch\n"
        "Version: 2.12.1+cu130\n",
    ],
)
def test_torch_metadata_rejects_duplicate_or_defective_identity(
    metadata: str,
) -> None:
    with pytest.raises(CanonicalAcquisitionError, match="metadata is invalid"):
        _setuptools_specifier_from_metadata(
            metadata,
            python_version="3.13.14",
            expected_torch_version="2.12.1+cu130",
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
            stdout=_pylock(
                ("nvidia-cublas-cu13", "13.1.0"),
                ("torch", "2.12.1+cu130"),
                ("torchvision", "0.27.1+cu130"),
            ),
        )

    resolver = UvPythonGroupResolver(
        HostUvRunner(Path("/opt/cdh/bin/uv")),
        runner,
        metadata_reader=lambda _url: _TORCH_METADATA,
    )
    result = resolver.resolve(_group())

    assert result == ResolvedPythonGroup(
        (
            ResolvedPythonMember("torch", "2.12.1+cu130"),
            ResolvedPythonMember("torchvision", "0.27.1+cu130"),
        ),
        "<82",
    )
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:4] == ("/opt/cdh/bin/uv", "--no-config", "pip", "compile")
    assert argv[argv.index("--python-version") + 1] == "3.13.14"
    assert argv[argv.index("--python-platform") + 1] == "x86_64-unknown-linux-gnu"
    assert "--default-index" not in argv
    assert "--no-sources" not in argv
    resolution_root = Path(str(kwargs["cwd"]))
    assert resolution_root.is_absolute()
    assert resolution_root.name.startswith("cdh-pytorch-resolution-")
    assert Path(argv[4]).parent == resolution_root
    assert argv[argv.index("--project") + 1] == str(resolution_root)
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["UV_NO_CONFIG"] == "1"
    assert "UV_INDEX_URL" not in environment
    assert "PIP_CONFIG_FILE" not in environment


def test_non_public_uv_tool_group_uses_exact_isolated_resolution_seam() -> None:
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
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(
        args: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="ruff==0.12.0\n")

    resolved = UvPythonGroupResolver(
        HostUvRunner(Path("/opt/cdh/bin/uv")), runner
    ).resolve(request)

    assert resolved == ResolvedPythonGroup((ResolvedPythonMember("ruff", "0.12.0"),))
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == "/opt/cdh/bin/uv"
    assert argv[argv.index("--python-version") + 1] == "3.13.14"
    assert argv[argv.index("--python-platform") + 1] == "x86_64-unknown-linux-gnu"
    assert argv[argv.index("--default-index") + 1] == "https://pypi.org/simple"
    assert kwargs["input"] == "ruff==0.12.0\n"


def test_comfy_cli_uses_highest_stable_target_resolution_through_python_index() -> None:
    request = ComfyCliRequestIdentity(
        type="comfy-cli",
        package="comfy-cli",
        policy="highest-target-compatible-stable",
        minimum_version="1.7.0",
        environment="uv-tool:comfy-cli",
        python_version="3.14.6",
        platform="linux/amd64",
        index_url="https://packages.example/simple",
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(
        args: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="comfy-cli==2.0.0\n")

    resolved = UvPythonGroupResolver(
        HostUvRunner(Path("/opt/cdh/bin/uv")), runner
    ).resolve(request)

    assert resolved == ResolvedPythonGroup(
        (ResolvedPythonMember("comfy-cli", "2.0.0"),)
    )
    argv, kwargs = calls[0]
    assert argv[argv.index("--python-version") + 1] == "3.14.6"
    assert argv[argv.index("--python-platform") + 1] == "x86_64-unknown-linux-gnu"
    assert argv[argv.index("--default-index") + 1] == request.index_url
    assert argv[argv.index("--resolution") + 1] == "highest"
    assert argv[argv.index("--prerelease") + 1] == "disallow"
    assert kwargs["input"] == "comfy-cli>=1.7.0\n"


@pytest.mark.parametrize(
    ("stdout", "returncode", "message"),
    [
        ("not toml", 0, "invalid PyTorch metadata"),
        (
            _pylock(("torch", "2.12.1+cu130")),
            0,
            "incompatible direct group",
        ),
        (
            _pylock(
                ("torch", "2.12.1+cpu"),
                ("torchvision", "0.27.1+cu130"),
            ),
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

    resolver = UvPythonGroupResolver(
        HostUvRunner(Path("/opt/cdh/bin/uv")),
        runner,
        metadata_reader=lambda _url: _TORCH_METADATA,
    )

    with pytest.raises(CanonicalAcquisitionError, match=message):
        resolver.resolve(_group())


# Adapter projection preserves cohesive provider results without local-label inference.
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
            stdout=_pylock(
                ("custom-extra", "1.0+vendor.1"),
                ("torch", "2.12.1+cu130"),
                ("torchvision", "0.27.1+cu130"),
            ),
        )

    resolved = UvPythonGroupResolver(
        HostUvRunner(Path("/opt/cdh/bin/uv")),
        runner,
        metadata_reader=lambda _url: _TORCH_METADATA,
    ).resolve(request)

    assert resolved.members[0] == ResolvedPythonMember("custom-extra", "1.0+vendor.1")


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
    ancestry_calls: list[tuple[str, str, str]] = field(default_factory=list)
    ancestry_result: bool = True

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
            OfficialComfyUIIdentity(repository, COMMIT_A, "0.11.0"),
            OfficialComfyUIIdentity(repository, COMMIT_B, "0.12.0"),
        )

    def is_ancestor(self, repository: str, ancestor: str, descendant: str) -> bool:
        self.ancestry_calls.append((repository, ancestor, descendant))
        return self.ancestry_result


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
    calls: list[PythonGroupRequestIdentity | ComfyCliRequestIdentity] = field(
        default_factory=list
    )

    def resolve(
        self, request: PythonGroupRequestIdentity | ComfyCliRequestIdentity
    ) -> ResolvedPythonGroup:
        self.calls.append(request)
        if isinstance(request, ComfyCliRequestIdentity):
            return ResolvedPythonGroup((ResolvedPythonMember("comfy-cli", "2.0.0"),))
        return ResolvedPythonGroup(
            (
                ResolvedPythonMember("torch", "2.12.1+cu130"),
                ResolvedPythonMember("torchvision", "0.27.1+cu130"),
            ),
            "<82",
        )


@dataclass
class ProviderFakes:
    oci: FakeOci = field(default_factory=FakeOci)
    python: FakeManagedPython = field(default_factory=FakeManagedPython)
    comfyui: FakeComfyUI = field(default_factory=FakeComfyUI)
    registry: FakeRegistry = field(default_factory=FakeRegistry)
    git: FakeGit = field(default_factory=FakeGit)
    group: FakePythonGroup = field(default_factory=FakePythonGroup)
    requirements: bytes = b"torch\ntorchvision\ntorchaudio\nnumpy>=1.25\n"
    requirements_reads: int = 0

    def read_requirements(self, _request) -> bytes:
        self.requirements_reads += 1
        return self.requirements

    def acquirer(self) -> ProviderIdentityAcquirer:
        return ProviderIdentityAcquirer(
            oci=self.oci,
            managed_python=self.python,
            comfyui=self.comfyui,
            registry=self.registry,
            git=self.git,
            python_group=self.group,
            release=ManagedPythonReleaseInputs(
                "26.1.2",
                "0.5.0",
                DIGEST_A,
                "0.11.28",
            ),
            requirements_reader=self.read_requirements,
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
    assert [entry.package for entry in group_entries[:-1]] == [
        "torch",
        "torchvision",
    ]
    assert group_entries[-1].setuptools_specifier == "<82"
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


def test_exact_git_registry_and_comfy_cli_provider_boundaries() -> None:
    fakes = ProviderFakes()
    git = DirectGitRequestIdentity(
        type="git", url="https://example.test/node.git", ref=COMMIT_A
    )
    cli = ComfyCliRequestIdentity(
        type="comfy-cli",
        package="comfy-cli",
        policy="highest-target-compatible-stable",
        minimum_version="1.7.0",
        environment="uv-tool:comfy-cli",
        index_url="https://pypi.org/simple",
        python_version="3.13.14",
        platform="linux/amd64",
    )
    registry = RegistryRequestIdentity(
        type="registry", id="example-node", selector="1.2.3"
    )

    assert _acquire(git, fakes)[0].commit == COMMIT_A
    assert _acquire(cli, fakes)[0].version == "2.0.0"
    assert _acquire(registry, fakes)[0].version == "1.2.3"
    assert fakes.git.calls == []
    assert fakes.group.calls == [cli]
    assert len(fakes.registry.resolve_calls) == 1


# ComfyUI requirement bytes and formal-release identity are acquired as one
# source proof.
def test_comfyui_exact_release_maps_to_canonical_tag_ref() -> None:
    fakes = ProviderFakes()
    request = ComfyUIRequestIdentity(
        type="comfyui", repository=OFFICIAL_REPOSITORY, selector="0.11.0"
    )

    entry = _acquire(request, fakes)[0]

    assert entry.formal_release == "0.11.0"
    assert fakes.comfyui.resolve_calls[0].ref == "refs/tags/v0.11.0"


def test_exact_comfyui_requirements_acquisition_binds_bytes_target_and_projection() -> (
    None
):
    fakes = ProviderFakes()
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository=OFFICIAL_REPOSITORY,
        commit=COMMIT_A,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
        python_version="3.13.14",
        platform="linux/amd64",
        protected_names=["torch", "torchaudio", "torchvision"],
        protected_policy_digest=DIGEST_B,
    )

    entry = _acquire(request, fakes)[0]

    assert entry.commit == COMMIT_A
    assert entry.floor_commit == COMFYUI_FLOOR_COMMIT
    assert fakes.comfyui.ancestry_calls == [
        (OFFICIAL_REPOSITORY, COMFYUI_FLOOR_COMMIT, COMMIT_A)
    ]
    assert fakes.requirements_reads == 1
    assert entry.requirements_digest.startswith("sha256:")
    assert [item.package for item in entry.protected] == [
        "torch",
        "torchaudio",
        "torchvision",
    ]
    assert not hasattr(entry, "ordinary")


def test_comfyui_requirements_proves_floor_before_reading_bytes() -> None:
    fakes = ProviderFakes(comfyui=FakeComfyUI(ancestry_result=False))
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository=OFFICIAL_REPOSITORY,
        commit=COMMIT_A,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
        python_version="3.13.14",
        platform="linux/amd64",
        protected_names=["torch", "torchaudio", "torchvision"],
        protected_policy_digest=DIGEST_B,
    )

    with pytest.raises(CanonicalAcquisitionError, match=r"supported v0\.11\.0 floor"):
        _acquire(request, fakes)

    assert fakes.requirements_reads == 0


def test_exact_comfyui_requirements_rejects_source_changing_input() -> None:
    fakes = ProviderFakes(
        requirements=b"--extra-index-url https://poison.test/simple\ntorch\n"
    )
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository=OFFICIAL_REPOSITORY,
        commit=COMMIT_A,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
        python_version="3.13.14",
        platform="linux/amd64",
        protected_names=["torch", "torchaudio", "torchvision"],
        protected_policy_digest=DIGEST_B,
    )

    with pytest.raises(CanonicalAcquisitionError, match="changes package sources"):
        _acquire(request, fakes)


def test_comfyui_exact_release_rejects_mismatched_provider_release() -> None:
    class MismatchedComfyUI(FakeComfyUI):
        def resolve(
            self, request: OfficialComfyUIIdentityRequest
        ) -> OfficialComfyUIIdentity:
            return OfficialComfyUIIdentity(OFFICIAL_REPOSITORY, COMMIT_A, "0.12.0")

    fakes = ProviderFakes(comfyui=MismatchedComfyUI())
    request = ComfyUIRequestIdentity(
        type="comfyui", repository=OFFICIAL_REPOSITORY, selector="0.11.0"
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


# Selector acquisition accepts only compatible exact results and refreshes
# moving inputs once.
def test_python_group_rejects_version_outside_member_selector() -> None:
    class MismatchedPythonGroup(FakePythonGroup):
        def resolve(self, request: PythonGroupRequestIdentity) -> ResolvedPythonGroup:
            return ResolvedPythonGroup(
                (
                    ResolvedPythonMember("torch", "2.11.0+cu130"),
                    ResolvedPythonMember("torchvision", "0.27.1+cu130"),
                ),
                "<82",
            )

    fakes = ProviderFakes(group=MismatchedPythonGroup())

    with pytest.raises(CanonicalAcquisitionError, match="incompatible data"):
        _acquire(_group(), fakes)


def test_moving_catalog_selectors_choose_highest_stable_match() -> None:
    fakes = ProviderFakes()
    comfyui = ComfyUIRequestIdentity(
        type="comfyui", repository=OFFICIAL_REPOSITORY, selector="<1,>=0.11"
    )
    cli = ComfyCliRequestIdentity(
        type="comfy-cli",
        package="comfy-cli",
        policy="highest-target-compatible-stable",
        minimum_version="1.7.0",
        environment="uv-tool:comfy-cli",
        index_url="https://pypi.org/simple",
        python_version="3.13.14",
        platform="linux/amd64",
    )
    registry = RegistryRequestIdentity(
        type="registry", id="example-node", selector="<3,>=1"
    )

    assert _acquire(comfyui, fakes)[0].formal_release == "0.12.0"
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
