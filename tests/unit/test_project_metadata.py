"""Project packaging metadata tests."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath

from packaging.specifiers import SpecifierSet

from comfyui_docker_helper.exact_ledger import (
    CDH_VERSION,
    UV_BUILD_REQUIREMENT,
    UV_RUNTIME_REQUIREMENT,
)
from comfyui_docker_helper.release_artifacts import (
    PACKAGE_ROOT,
    PROJECTED_LICENSE,
    PROJECTED_PYPROJECT,
    release_projection_files,
)

PROJECT_ROOT = Path(__file__).parents[2]


def _project_metadata() -> dict[str, object]:
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return pyproject


# Published metadata and projected source artifacts stay aligned with release authority.
def test_supported_python_minors_match_project_metadata() -> None:
    """Package metadata must expose exactly the automated Python minors."""

    pyproject = _project_metadata()
    project = pyproject["project"]
    requires_python = SpecifierSet(project["requires-python"])

    assert str(requires_python) == "<3.15,>=3.12"
    assert "3.12" in requires_python
    assert "3.13" in requires_python
    assert "3.14" in requires_python
    assert {
        classifier
        for classifier in project["classifiers"]
        if classifier.startswith("Programming Language :: Python :: 3.")
    } == {
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
    }


def test_project_release_identity_matches_toolchain_metadata() -> None:
    """Package metadata matches the current release and host toolchain ledger."""

    pyproject = _project_metadata()

    assert pyproject["project"]["version"] == CDH_VERSION
    assert UV_RUNTIME_REQUIREMENT in pyproject["project"]["dependencies"]
    assert pyproject["build-system"]["requires"] == [UV_BUILD_REQUIREMENT]


def test_projected_release_metadata_matches_repository_metadata() -> None:
    repository = _project_metadata()
    projected = tomllib.loads(PROJECTED_PYPROJECT.read_text(encoding="utf-8"))

    assert projected["project"] == repository["project"]
    assert projected["build-system"] == repository["build-system"]
    assert (
        projected["tool"]["uv"]["build-backend"]
        == repository["tool"]["uv"]["build-backend"]
    )
    assert PROJECTED_LICENSE.read_bytes() == (PROJECT_ROOT / "LICENSE").read_bytes()
    assert "readme" not in repository["project"]


def test_projected_release_source_is_entirely_wheel_owned() -> None:
    projected = release_projection_files()
    relative_paths = tuple(item.relative_path for item in projected)

    assert len(relative_paths) == len(set(relative_paths))
    for item in projected:
        assert item.source_path.is_relative_to(PACKAGE_ROOT)


def test_package_resources_contain_the_final_probe_and_release_projection() -> None:
    resource_root = PurePosixPath("src/comfyui_docker_helper/resources")
    resource_paths = {
        item.relative_path.relative_to(resource_root)
        for item in release_projection_files()
        if item.relative_path.is_relative_to(resource_root)
    }

    assert resource_paths == {
        PurePosixPath("final-core-probe.py"),
        PurePosixPath("release-projection/LICENSE"),
        PurePosixPath("release-projection/pyproject.toml"),
    }
