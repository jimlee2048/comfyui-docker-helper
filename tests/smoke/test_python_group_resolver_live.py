"""Opt-in live contract for the exact host uv Python-group resolver."""

import pytest

from comfyui_docker_helper.config.canonical_lock import (
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    PyTorchRequestIdentity,
)
from comfyui_docker_helper.config.canonical_resolver import CanonicalAcquisitionError
from comfyui_docker_helper.exact_ledger import (
    DEFAULT_MANAGED_PYTHON_VERSION,
    FALLBACK_MANAGED_PYTHON_VERSION,
)
from comfyui_docker_helper.host.canonical_acquisition import UvPythonGroupResolver
from comfyui_docker_helper.host.uv_runner import locate_host_uv


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
    [DEFAULT_MANAGED_PYTHON_VERSION, FALLBACK_MANAGED_PYTHON_VERSION],
)
def test_exact_host_uv_preserves_real_cu130_distribution_versions(
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
        ],
    )

    resolved = UvPythonGroupResolver(locate_host_uv()).resolve(request)

    assert [(item.package, item.version) for item in resolved.members] == [
        ("torch", "2.12.1+cu130"),
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

    with pytest.raises(CanonicalAcquisitionError, match="resolution failed"):
        UvPythonGroupResolver(locate_host_uv()).resolve(request)
