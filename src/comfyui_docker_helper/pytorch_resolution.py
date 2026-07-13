"""Shared deterministic PyTorch source-routing manifest."""

from __future__ import annotations

import tomli_w
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


def pytorch_resolution_manifest_bytes(
    *,
    requirements: tuple[str, ...],
    direct_packages: tuple[str, ...],
    python_version: str,
    python_index_url: str,
    pytorch_index_url: str,
) -> bytes:
    """Serialize one internal Python-default/PyTorch-explicit project input."""
    packages = tuple(canonicalize_name(name) for name in direct_packages)
    if not requirements or len(requirements) != len(packages):
        raise ValueError("PyTorch manifest requires one requirement per package")
    if len(packages) != len(set(packages)) or "torch" not in packages:
        raise ValueError(
            "PyTorch manifest direct packages must be unique and include torch"
        )
    try:
        requirement_packages = tuple(
            canonicalize_name(Requirement(value).name) for value in requirements
        )
    except InvalidRequirement as error:
        raise ValueError("PyTorch manifest requirements must be valid") from error
    if requirement_packages != packages:
        raise ValueError("PyTorch manifest requirements must match direct packages")
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
                "sources": {name: {"index": "pytorch"} for name in sorted(packages)},
            }
        },
    }
    return tomli_w.dumps(document).encode("utf-8")
