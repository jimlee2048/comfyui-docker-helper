"""Opt-in live contract for the exact host uv Python-group resolver."""

import pytest

from comfyui_docker_helper.config.canonical_lock import (
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
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
