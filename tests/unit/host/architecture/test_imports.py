"""Import smoke tests for the Host/shared generic selector."""

import pytest

from tests.import_support import (
    assert_clean_subprocess_import,
    container_module_names,
    host_module_names,
    package_module_names,
)


@pytest.mark.parametrize(
    "module_name",
    host_module_names(),
)
def test_host_module_imports_cleanly(module_name: str) -> None:
    """Import each Host-owned module successfully and without output."""
    assert_clean_subprocess_import(module_name)


def test_generic_import_selectors_partition_package_modules() -> None:
    """Assign every discovered module to exactly one generic import owner."""
    package_modules = set(package_module_names())
    host_modules = set(host_module_names())
    container_modules = set(container_module_names())

    assert package_modules
    assert host_modules
    assert container_modules
    assert host_modules.isdisjoint(container_modules)
    assert host_modules | container_modules == package_modules
