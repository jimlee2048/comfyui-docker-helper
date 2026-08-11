"""Import smoke tests for the supported host/shared package closure."""

import subprocess
import sys

import pytest

from tests.import_support import package_module_names

_CONTAINER_PREFIX = "comfyui_docker_helper.container."
_HOST_CONTAINER_MODULES = frozenset({"comfyui_docker_helper.container.cli"})


def host_module_names() -> tuple[str, ...]:
    """Return modules owned by the native host execution contract."""
    return tuple(
        name
        for name in package_module_names()
        if not name.startswith(_CONTAINER_PREFIX) or name in _HOST_CONTAINER_MODULES
    )


# Importing public modules must not start processes, network access, or
# filesystem writes.
@pytest.mark.parametrize(
    "module_name",
    host_module_names(),
)
def test_import_has_no_observable_side_effects(module_name: str) -> None:
    """Import every host-owned module without output or process failure."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
