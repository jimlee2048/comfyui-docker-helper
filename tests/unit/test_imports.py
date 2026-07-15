"""Import smoke tests for package boundaries."""

import pkgutil
import subprocess
import sys

import pytest

import comfyui_docker_helper


def package_module_names() -> list[str]:
    """Return every importable module exposed by the package."""
    package_paths = [str(path) for path in comfyui_docker_helper.__path__]
    discovered = sorted(
        module.name
        for module in pkgutil.walk_packages(
            package_paths,
            prefix=f"{comfyui_docker_helper.__name__}.",
        )
    )
    return [comfyui_docker_helper.__name__, *discovered]


# Importing public modules must not start processes, network access, or
# filesystem writes.
@pytest.mark.parametrize(
    "module_name",
    package_module_names(),
)
def test_import_has_no_observable_side_effects(module_name: str) -> None:
    """Import every current package module without output or process failure."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_missing_linkat_only_fails_when_evidence_is_published() -> None:
    """Keep host-facing imports usable when the container syscall is unavailable."""
    script = """
import ctypes
import os
import tempfile
from pathlib import Path

real_cdll = ctypes.CDLL


class LibraryWithoutLinkat:
    def __init__(self, *args, **kwargs):
        self.library = real_cdll(*args, **kwargs)

    def __getattr__(self, name):
        if name == "linkat":
            raise AttributeError(name)
        return getattr(self.library, name)


ctypes.CDLL = LibraryWithoutLinkat

import comfyui_docker_helper
import comfyui_docker_helper.cli
import comfyui_docker_helper.host.cli
import comfyui_docker_helper.version
from comfyui_docker_helper.container.evidence_writer import (
    ApplicationEvidenceError,
    write_application_evidence,
)

with tempfile.TemporaryDirectory() as directory:
    parent = Path(directory)
    try:
        write_application_evidence(
            parent / "inventory.json",
            b"evidence\\n",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    except ApplicationEvidenceError as error:
        assert "linkat(AT_EMPTY_PATH) is unavailable" in str(error)
    else:
        raise AssertionError("evidence publication unexpectedly succeeded")
    assert list(parent.iterdir()) == []
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
