"""Project packaging metadata tests."""

from __future__ import annotations

import tomllib
from pathlib import PurePosixPath

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version
from tests.project_paths import PROJECT_ROOT

from comfyui_docker_helper.release_artifacts import (
    PACKAGE_ROOT,
    PROJECTED_LICENSE,
    PROJECTED_PYPROJECT,
    WORKSPACE_PROFILE_RESOURCE,
    release_projection_files,
)
from comfyui_docker_helper.version import package_version


def _project_metadata() -> dict[str, object]:
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return pyproject


def _locked_project() -> dict[str, object]:
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return next(
        package
        for package in lock["package"]
        if package["name"] == "comfyui-docker-helper"
    )


def _requirement_identity(
    requirement: Requirement,
) -> tuple[str, tuple[str, ...], str, str | None]:
    marker = str(requirement.marker) if requirement.marker is not None else None
    return (
        canonicalize_name(requirement.name),
        tuple(sorted(requirement.extras)),
        str(requirement.specifier),
        marker,
    )


def _locked_requirement(item: dict[str, object]) -> Requirement:
    extras = item.get("extras", ())
    extras_suffix = f"[{','.join(extras)}]" if extras else ""
    value = f"{item['name']}{extras_suffix}{item.get('specifier', '')}"
    if marker := item.get("marker"):
        value = f"{value}; {marker}"
    return Requirement(value)


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


def test_project_release_identity_matches_package_metadata() -> None:
    """Package metadata matches the current release and dependency roles."""

    pyproject = _project_metadata()
    project = pyproject["project"]
    locked = _locked_project()

    assert project["version"] == package_version() == locked["version"]
    assert {
        classifier
        for classifier in project["classifiers"]
        if classifier.startswith("Operating System ::")
    } == {
        "Operating System :: Microsoft :: Windows",
        "Operating System :: POSIX :: Linux",
    }
    runtime_requirements = {
        _requirement_identity(Requirement(item)) for item in project["dependencies"]
    }
    development_requirements = {
        _requirement_identity(Requirement(item))
        for item in pyproject["dependency-groups"]["dev"]
    }
    locked_runtime_requirements = {
        _requirement_identity(_locked_requirement(item))
        for item in locked["metadata"]["requires-dist"]
    }
    locked_development_requirements = {
        _requirement_identity(_locked_requirement(item))
        for item in locked["metadata"]["requires-dev"]["dev"]
    }

    assert runtime_requirements == locked_runtime_requirements
    assert development_requirements == locked_development_requirements
    assert any(item[0] == "twine" for item in development_requirements)
    assert all(item[0] != "twine" for item in runtime_requirements)


def test_projected_release_metadata_matches_repository_metadata() -> None:
    """The packaged release projection retains one exact stable uv_build backend."""

    repository = _project_metadata()
    projected = tomllib.loads(PROJECTED_PYPROJECT.read_text(encoding="utf-8"))
    build_system = repository["build-system"]
    build_requirements = build_system["requires"]

    assert projected["project"] == repository["project"]
    assert projected["build-system"] == build_system
    assert (
        projected["tool"]["uv"]["build-backend"]
        == repository["tool"]["uv"]["build-backend"]
    )
    assert PROJECTED_LICENSE.read_bytes() == (PROJECT_ROOT / "LICENSE").read_bytes()
    assert build_system["build-backend"] == "uv_build"
    assert len(build_requirements) == 1

    requirement = Requirement(build_requirements[0])
    specifiers = tuple(requirement.specifier)
    assert canonicalize_name(requirement.name) == "uv-build"
    assert requirement.extras == set()
    assert requirement.marker is None
    assert requirement.url is None
    assert len(specifiers) == 1
    assert specifiers[0].operator == "=="
    version = Version(specifiers[0].version)
    assert len(version.release) == 3
    assert not any(
        (version.is_prerelease, version.is_devrelease, version.is_postrelease)
    )
    assert version.local is None


def test_projected_release_source_is_entirely_wheel_owned() -> None:
    projected = release_projection_files()
    relative_paths = tuple(item.relative_path for item in projected)

    assert projected
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
        PurePosixPath("cdh-workspace.sh"),
        PurePosixPath("final-core-probe.py"),
        PurePosixPath("release-projection/LICENSE"),
        PurePosixPath("release-projection/pyproject.toml"),
    }
    assert WORKSPACE_PROFILE_RESOURCE.read_bytes()
