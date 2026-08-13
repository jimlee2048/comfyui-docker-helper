"""Source-aware PyTorch resolution manifest contracts."""

from __future__ import annotations

import tomllib

import pytest

from comfyui_docker_helper.pytorch_resolution import (
    pytorch_resolution_manifest_bytes,
)


def test_manifest_routes_only_index_owned_members_to_pytorch() -> None:
    source = "https://example.test/sageattention.whl#sha256=abc"
    document = tomllib.loads(
        pytorch_resolution_manifest_bytes(
            requirements=(
                "torch==2.12.1",
                f"sageattention[torch] @ {source}",
            ),
            pytorch_index_packages=("torch",),
            python_version="3.13.14",
            python_index_url="https://pypi.org/simple",
            pytorch_index_url="https://download.pytorch.org/whl/cu130",
        ).decode()
    )

    assert document["project"]["dependencies"] == [
        "torch==2.12.1",
        f"sageattention[torch] @ {source}",
    ]
    assert document["tool"]["uv"]["sources"] == {"torch": {"index": "pytorch"}}
    assert document["tool"]["uv"]["index"] == [
        {
            "name": "python",
            "url": "https://pypi.org/simple",
            "default": True,
        },
        {
            "name": "pytorch",
            "url": "https://download.pytorch.org/whl/cu130",
            "explicit": True,
        },
    ]


def test_manifest_rejects_a_protected_direct_source() -> None:
    with pytest.raises(
        ValueError,
        match="protected PyTorch packages must use the PyTorch index",
    ):
        pytorch_resolution_manifest_bytes(
            requirements=("torch @ https://example.test/torch.whl",),
            pytorch_index_packages=(),
            python_version="3.13.14",
            python_index_url="https://pypi.org/simple",
            pytorch_index_url="https://download.pytorch.org/whl/cu130",
        )


def test_manifest_rejects_routing_a_direct_member_to_the_index() -> None:
    with pytest.raises(
        ValueError,
        match="PyTorch index packages must match index-backed members",
    ):
        pytorch_resolution_manifest_bytes(
            requirements=(
                "torch==2.12.1",
                "sageattention @ https://example.test/sageattention.whl",
            ),
            pytorch_index_packages=("torch", "sageattention"),
            python_version="3.13.14",
            python_index_url="https://pypi.org/simple",
            pytorch_index_url="https://download.pytorch.org/whl/cu130",
        )
