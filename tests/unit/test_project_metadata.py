"""Project packaging metadata tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet

from comfyui_docker_helper.exact_ledger import (
    UV_BUILD_REQUIREMENT,
    UV_RUNTIME_REQUIREMENT,
)

PROJECT_ROOT = Path(__file__).parents[2]


def _project_metadata() -> dict[str, object]:
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return pyproject


def test_supported_python_minors_match_project_metadata() -> None:
    """Package metadata must expose exactly the automated Python minors."""

    pyproject = _project_metadata()
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


def test_exact_v05_project_toolchain_metadata() -> None:
    """The package carries the exact release-owned host toolchain ledger."""

    pyproject = _project_metadata()

    assert pyproject["project"]["version"] == "0.5.0"
    assert UV_RUNTIME_REQUIREMENT in pyproject["project"]["dependencies"]
    assert pyproject["build-system"]["requires"] == [UV_BUILD_REQUIREMENT]
