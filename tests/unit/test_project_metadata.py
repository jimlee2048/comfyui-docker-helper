"""Project packaging metadata tests."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from comfyui_docker_helper.release_artifacts import (
    PACKAGE_ROOT,
    PROJECTED_LICENSE,
    PROJECTED_PYPROJECT,
    release_projection_files,
)
from comfyui_docker_helper.version import package_version

PROJECT_ROOT = Path(__file__).parents[2]
INLINE_README = """\
`comfyui-docker-helper` (`cdh`) is an independent, unofficial command-line
helper for using ComfyUI with Docker. It is not affiliated with or endorsed by
the ComfyUI project.

See the [GitHub repository](https://github.com/jimlee2048/comfyui-docker-helper)
for current capabilities, requirements, documentation, examples, and issue
tracking."""


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
    """Package metadata matches the current release and runtime dependencies."""

    pyproject = _project_metadata()
    project = pyproject["project"]
    locked = _locked_project()

    assert project["version"] == package_version() == locked["version"]
    assert project["description"] == (
        "Unofficial command-line helper for using ComfyUI with Docker"
    )
    assert project["readme"] == {
        "text": INLINE_README,
        "content-type": "text/markdown",
    }
    assert {
        classifier
        for classifier in project["classifiers"]
        if classifier.startswith("Operating System ::")
    } == {
        "Operating System :: Microsoft :: Windows",
        "Operating System :: POSIX :: Linux",
    }
    assert (
        project["urls"]["Changelog"]
        == "https://github.com/jimlee2048/comfyui-docker-helper/releases"
    )
    assert "build>=1,<2" in project["dependencies"]
    assert "filelock>=3.32.2,<4" in project["dependencies"]
    assert "cryptography>=49" in project["dependencies"]
    assert "python-on-whales>=0.81.0" in project["dependencies"]
    assert "pywin32==312; sys_platform == 'win32'" in project["dependencies"]
    assert "twine" in pyproject["dependency-groups"]["dev"]
    assert "twine" in {item["name"] for item in locked["dev-dependencies"]["dev"]}
    assert "twine" not in {item["name"] for item in locked["dependencies"]}


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
