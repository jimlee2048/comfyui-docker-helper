"""Opt-in live contracts for exact ComfyUI and Python-group acquisition."""

import httpx
import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from tests.acceptance_scenarios import RELEASE_PYTHON_PROFILES

from comfyui_docker_helper.comfyui_requirements import (
    CUDA_PROTECTED_REQUIREMENTS,
    parse_comfyui_requirements,
    parse_manager_requirements,
)
from comfyui_docker_helper.config.canonical_lock import (
    ComfyCliRequestIdentity,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    PyTorchRequestIdentity,
)
from comfyui_docker_helper.config.canonical_resolver import CanonicalAcquisitionError
from comfyui_docker_helper.exact_ledger import (
    COMFY_CLI_MINIMUM_VERSION,
    COMFYUI_FLOOR_COMMIT,
    COMFYUI_REPOSITORY,
    DEFAULT_MANAGED_PYTHON_VERSION,
    UV_IMAGE_REPOSITORY,
)
from comfyui_docker_helper.host.canonical_acquisition import DockerPythonGroupResolver
from comfyui_docker_helper.host.identity_providers import (
    GitOfficialComfyUIIdentityProvider,
    HttpOciIdentityProvider,
    OciIdentityRequest,
    OfficialComfyUIIdentityRequest,
)

QUALIFIED_TEST_UV_VERSION = "0.11.28"


@pytest.fixture(scope="module")
def uv_descriptor_digest() -> str:
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        return (
            HttpOciIdentityProvider(client)
            .resolve(
                OciIdentityRequest(
                    "uv-tool",
                    UV_IMAGE_REPOSITORY,
                    f"{QUALIFIED_TEST_UV_VERSION}-debian-slim",
                )
            )
            .descriptor_digest
        )


# Live resolution proves supported profiles and strict Python/PyTorch source routing.
@pytest.mark.network
@pytest.mark.smoke
def test_exact_v011_source_requirements_and_manager_ownership_are_live() -> None:
    provider = GitOfficialComfyUIIdentityProvider()
    identity = provider.resolve(
        OfficialComfyUIIdentityRequest(COMFYUI_REPOSITORY, "refs/tags/v0.11.0")
    )

    assert identity.commit == COMFYUI_FLOOR_COMMIT
    assert identity.formal_release == "0.11.0"
    assert provider.is_ancestor(
        COMFYUI_REPOSITORY, COMFYUI_FLOOR_COMMIT, identity.commit
    )

    base = f"https://raw.githubusercontent.com/Comfy-Org/ComfyUI/{COMFYUI_FLOOR_COMMIT}"
    requirements_response = httpx.get(
        f"{base}/requirements.txt", follow_redirects=True, timeout=30.0
    )
    requirements_response.raise_for_status()
    manager_response = httpx.get(
        f"{base}/manager_requirements.txt", follow_redirects=True, timeout=30.0
    )
    manager_response.raise_for_status()

    for python_version in RELEASE_PYTHON_PROFILES:
        parsed = parse_comfyui_requirements(
            requirements_response.content,
            python_version=python_version,
            platform="linux/amd64",
            machine="x86_64",
            protected_names=CUDA_PROTECTED_REQUIREMENTS,
        )
        assert [item.package for item in parsed.protected] == [
            "torch",
            "torchaudio",
            "torchvision",
        ]
        ordinary_names = {
            canonicalize_name(Requirement(item).name) for item in parsed.ordinary
        }
        assert {"comfy-kitchen", "requests"} <= ordinary_names

    manager = parse_manager_requirements(
        manager_response.content,
        python_version=DEFAULT_MANAGED_PYTHON_VERSION,
        platform="linux/amd64",
        machine="x86_64",
    )
    assert manager.rows == ("comfyui_manager==4.0.5",)
    assert manager.manager_version == "4.0.5"
    assert manager.digest == (
        "sha256:20c24949777265225ea5dc4ceb44a45c6dc6ec46d206d40b7871ebf80054e33c"
    )
    assert manager_response.content == b"comfyui_manager==4.0.5\n"


@pytest.mark.network
@pytest.mark.docker
@pytest.mark.smoke
def test_exact_oci_uv_resolves_one_real_complete_group(
    uv_descriptor_digest: str,
) -> None:
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version=DEFAULT_MANAGED_PYTHON_VERSION,
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=uv_descriptor_digest,
        members=[
            DirectPythonRequestMember(package="packaging", extras=[], selector="==26.2")
        ],
    )

    resolved = DockerPythonGroupResolver().resolve(request)

    assert [(item.package, item.version) for item in resolved.members] == [
        ("packaging", "26.2")
    ]


@pytest.mark.network
@pytest.mark.docker
@pytest.mark.smoke
def test_exact_oci_uv_resolves_one_isolated_uv_tool(
    uv_descriptor_digest: str,
) -> None:
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="uv-tool:ruff",
        group="uv-tool",
        python_version=DEFAULT_MANAGED_PYTHON_VERSION,
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=uv_descriptor_digest,
        members=[
            DirectPythonRequestMember(package="ruff", extras=[], selector="==0.15.18")
        ],
    )

    resolved = DockerPythonGroupResolver().resolve(request)

    assert [(item.package, item.version) for item in resolved.members] == [
        ("ruff", "0.15.18")
    ]


@pytest.mark.network
@pytest.mark.docker
@pytest.mark.smoke
@pytest.mark.parametrize(
    "python_version",
    RELEASE_PYTHON_PROFILES,
)
def test_exact_oci_uv_resolves_optional_comfy_cli_for_every_target_profile(
    python_version: str,
    uv_descriptor_digest: str,
) -> None:
    request = ComfyCliRequestIdentity(
        type="comfy-cli",
        package="comfy-cli",
        policy="highest-target-compatible-stable",
        minimum_version=COMFY_CLI_MINIMUM_VERSION,
        environment="uv-tool:comfy-cli",
        python_version=python_version,
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=uv_descriptor_digest,
    )

    resolved = DockerPythonGroupResolver().resolve(request)

    assert len(resolved.members) == 1
    member = resolved.members[0]
    version = Version(member.version)
    assert member.package == "comfy-cli"
    assert version >= Version(COMFY_CLI_MINIMUM_VERSION)
    assert not version.is_prerelease
    assert not version.is_devrelease
    assert version.local is None


@pytest.mark.network
@pytest.mark.docker
@pytest.mark.smoke
@pytest.mark.parametrize(
    "python_version",
    RELEASE_PYTHON_PROFILES,
)
def test_exact_oci_uv_resolves_cu130_pytorch_group_for_every_release_profile(
    python_version: str,
    uv_descriptor_digest: str,
) -> None:
    request = PyTorchRequestIdentity(
        type="pytorch-group",
        environment="application",
        group="pytorch",
        backend="cuda",
        channel="cu130",
        python_version=python_version,
        platform="linux/amd64",
        python_index_url="https://pypi.org/simple",
        pytorch_index_url="https://download.pytorch.org/whl/cu130",
        resolver_descriptor_digest=uv_descriptor_digest,
        members=[
            DirectPythonRequestMember(package="torch", extras=[], selector="==2.12.1"),
            DirectPythonRequestMember(
                package="torchvision", extras=[], selector="==0.27.1"
            ),
            DirectPythonRequestMember(package="torchaudio", extras=[], selector=""),
        ],
    )

    resolved = DockerPythonGroupResolver().resolve(request)

    assert [(item.package, item.version) for item in resolved.members] == [
        ("torch", "2.12.1+cu130"),
        ("torchaudio", "2.11.0+cu130"),
        ("torchvision", "0.27.1+cu130"),
    ]
    assert resolved.setuptools_specifier == "<82"


@pytest.mark.network
@pytest.mark.docker
@pytest.mark.smoke
def test_pytorch_direct_extra_does_not_fall_back_to_python_index(
    uv_descriptor_digest: str,
) -> None:
    request = PyTorchRequestIdentity(
        type="pytorch-group",
        environment="application",
        group="pytorch",
        backend="cuda",
        channel="cu130",
        python_version=DEFAULT_MANAGED_PYTHON_VERSION,
        platform="linux/amd64",
        python_index_url="https://pypi.org/simple",
        pytorch_index_url="https://download.pytorch.org/whl/cu130",
        resolver_descriptor_digest=uv_descriptor_digest,
        members=[
            DirectPythonRequestMember(package="torch", extras=[], selector="==2.12.1"),
            DirectPythonRequestMember(
                package="pydantic-settings", extras=[], selector="==2.12.0"
            ),
        ],
    )

    with pytest.raises(CanonicalAcquisitionError, match="resolution failed") as raised:
        DockerPythonGroupResolver().resolve(request)

    message = str(raised.value)
    for expected in (
        "pydantic-settings",
        "cu130",
        DEFAULT_MANAGED_PYTHON_VERSION,
        "linux/amd64",
        "https://download.pytorch.org/whl/cu130",
    ):
        assert expected in message
