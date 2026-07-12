"""Opt-in live contract for the exact host uv Python-group resolver."""

import pytest

from comfyui_docker_helper.config.canonical_lock import (
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    PyTorchRequestIdentity,
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

    assert [(item.package, item.version) for item in resolved] == [
        ("packaging", "26.2")
    ]


@pytest.mark.network
@pytest.mark.smoke
def test_exact_host_uv_preserves_real_cu130_distribution_versions() -> None:
    request = PyTorchRequestIdentity(
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
                package="torchvision", extras=[], selector="==0.27.1"
            ),
        ],
    )

    resolved = UvPythonGroupResolver(locate_host_uv()).resolve(request)

    assert [(item.package, item.version) for item in resolved] == [
        ("torch", "2.12.1+cu130"),
        ("torchvision", "0.27.1+cu130"),
    ]
