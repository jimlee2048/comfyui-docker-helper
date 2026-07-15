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
