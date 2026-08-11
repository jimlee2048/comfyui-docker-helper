"""Linux authority for importing every package module."""

import subprocess
import sys

import pytest

from tests.import_support import package_module_names


@pytest.mark.parametrize("module_name", package_module_names())
def test_every_package_module_imports_without_side_effects(module_name: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
