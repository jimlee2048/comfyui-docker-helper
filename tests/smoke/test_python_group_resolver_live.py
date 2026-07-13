"""Opt-in live contracts for exact ComfyUI and Python-group acquisition."""

import httpx
import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from comfyui_docker_helper.comfyui_requirements import (
    CUDA_PROTECTED_REQUIREMENTS,
    parse_comfyui_requirements,
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
    FALLBACK_MANAGED_PYTHON_VERSION,
)
from comfyui_docker_helper.host.canonical_acquisition import UvPythonGroupResolver
from comfyui_docker_helper.host.identity_providers import (
    GitOfficialComfyUIIdentityProvider,
    OfficialComfyUIIdentityRequest,
)
from comfyui_docker_helper.host.uv_runner import locate_host_uv


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

    for python_version in (
        DEFAULT_MANAGED_PYTHON_VERSION,
        FALLBACK_MANAGED_PYTHON_VERSION,
        "3.14.6",
    ):
        parsed = parse_comfyui_requirements(
            requirements_response.content,
            python_version=python_version,
            platform="linux/amd64",
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

    manager_rows = {
        line.strip()
        for line in manager_response.text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "comfyui_manager==4.0.5" in manager_rows


@pytest.mark.network
@pytest.mark.smoke
def test_exact_host_uv_resolves_one_real_complete_group() -> None:
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        members=[
            DirectPythonRequestMember(package="packaging", extras=[], selector="==26.2")
        ],
    )

    resolved = UvPythonGroupResolver(locate_host_uv()).resolve(request)

    assert [(item.package, item.version) for item in resolved.members] == [
        ("packaging", "26.2")
    ]


@pytest.mark.network
@pytest.mark.smoke
@pytest.mark.parametrize(
    "python_version",
    [
        DEFAULT_MANAGED_PYTHON_VERSION,
        FALLBACK_MANAGED_PYTHON_VERSION,
        "3.14.6",
    ],
)
def test_exact_host_uv_resolves_optional_comfy_cli_for_every_target_profile(
    python_version: str,
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
    )

    resolved = UvPythonGroupResolver(locate_host_uv()).resolve(request)

    assert len(resolved.members) == 1
    member = resolved.members[0]
    version = Version(member.version)
    assert member.package == "comfy-cli"
    assert version >= Version(COMFY_CLI_MINIMUM_VERSION)
    assert not version.is_prerelease
    assert not version.is_devrelease
    assert version.local is None


@pytest.mark.network
@pytest.mark.smoke
@pytest.mark.parametrize(
    "python_version",
    [
        DEFAULT_MANAGED_PYTHON_VERSION,
        FALLBACK_MANAGED_PYTHON_VERSION,
        "3.14.6",
    ],
)
def test_exact_host_uv_resolves_d073_cu130_group_for_every_target_profile(
    python_version: str,
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
        members=[
            DirectPythonRequestMember(package="torch", extras=[], selector="==2.12.1"),
            DirectPythonRequestMember(
                package="torchvision", extras=[], selector="==0.27.1"
            ),
            DirectPythonRequestMember(package="torchaudio", extras=[], selector=""),
        ],
    )

    resolved = UvPythonGroupResolver(locate_host_uv()).resolve(request)

    assert [(item.package, item.version) for item in resolved.members] == [
        ("torch", "2.12.1+cu130"),
        ("torchaudio", "2.11.0+cu130"),
        ("torchvision", "0.27.1+cu130"),
    ]
    assert resolved.setuptools_specifier == "<82"


@pytest.mark.network
@pytest.mark.smoke
def test_pytorch_direct_extra_does_not_fall_back_to_python_index() -> None:
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
        members=[
            DirectPythonRequestMember(package="torch", extras=[], selector="==2.12.1"),
            DirectPythonRequestMember(
                package="pydantic-settings", extras=[], selector="==2.12.0"
            ),
        ],
    )

    with pytest.raises(CanonicalAcquisitionError, match="resolution failed") as raised:
        UvPythonGroupResolver(locate_host_uv()).resolve(request)

    message = str(raised.value)
    for expected in (
        "pydantic-settings",
        "cu130",
        DEFAULT_MANAGED_PYTHON_VERSION,
        "linux/amd64",
        "https://download.pytorch.org/whl/cu130",
    ):
        assert expected in message
