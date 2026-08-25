"""Linux authority for importing Container implementation modules."""

import pytest

from tests.import_support import (
    assert_clean_subprocess_import,
    container_module_names,
)


@pytest.mark.parametrize("module_name", container_module_names())
def test_container_module_imports_cleanly(module_name: str) -> None:
    """Import each Linux Container implementation successfully and without output."""
    assert_clean_subprocess_import(module_name)
