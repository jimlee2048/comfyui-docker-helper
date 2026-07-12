"""Project packaging metadata tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet

PROJECT_ROOT = Path(__file__).parents[2]


def test_supported_python_minors_match_project_metadata() -> None:
    """Package metadata must expose exactly the automated Python minors."""

    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject["project"]
    requires_python = SpecifierSet(project["requires-python"])

    assert "3.11" not in requires_python
    assert "3.12" in requires_python
    assert "3.13" in requires_python
    assert "3.14" not in requires_python
    assert {
        classifier
        for classifier in project["classifiers"]
        if classifier.startswith("Programming Language :: Python :: 3.")
    } == {
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    }
