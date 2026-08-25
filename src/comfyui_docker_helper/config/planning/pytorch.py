"""Shared deterministic PyTorch source-routing manifest."""

from __future__ import annotations

import tomli_w
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from comfyui_docker_helper.exact_ledger import CUDA_PROTECTED_REQUIREMENTS


def pytorch_resolution_manifest_bytes(
    *,
    requirements: tuple[str, ...],
    pytorch_index_packages: tuple[str, ...],
    python_version: str,
    python_index_url: str,
    pytorch_index_url: str,
) -> bytes:
    """Serialize one internal Python-default/PyTorch-explicit project input."""
    try:
        parsed_requirements = tuple(Requirement(value) for value in requirements)
    except InvalidRequirement as error:
        raise ValueError("PyTorch manifest requirements must be valid") from error
    packages = tuple(
        canonicalize_name(requirement.name) for requirement in parsed_requirements
    )
    index_packages = tuple(canonicalize_name(name) for name in pytorch_index_packages)
    if not requirements:
        raise ValueError("PyTorch manifest requires at least one package")
    if len(packages) != len(set(packages)) or "torch" not in packages:
        raise ValueError("PyTorch manifest packages must be unique and include torch")
    if len(index_packages) != len(set(index_packages)) or not set(
        index_packages
    ).issubset(packages):
        raise ValueError("PyTorch index packages must be unique group members")
    expected_index_packages = {
        canonicalize_name(requirement.name)
        for requirement in parsed_requirements
        if requirement.url is None
    }
    if set(index_packages) != expected_index_packages:
        raise ValueError("PyTorch index packages must match index-backed members")
    protected = set(packages).intersection(CUDA_PROTECTED_REQUIREMENTS)
    if not protected.issubset(index_packages):
        raise ValueError("protected PyTorch packages must use the PyTorch index")
    document = {
        "project": {
            "name": "cdh-pytorch-resolution",
            "version": "0",
            "requires-python": f"=={python_version}",
            "dependencies": list(requirements),
        },
        "tool": {
            "uv": {
                "index": [
                    {
                        "name": "python",
                        "url": python_index_url,
                        "default": True,
                    },
                    {
                        "name": "pytorch",
                        "url": pytorch_index_url,
                        "explicit": True,
                    },
                ],
                "sources": {
                    name: {"index": "pytorch"} for name in sorted(index_packages)
                },
            }
        },
    }
    return tomli_w.dumps(document).encode("utf-8")
