"""Project packaging metadata tests."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet

from comfyui_docker_helper.exact_ledger import (
    UV_BUILD_REQUIREMENT,
    UV_RUNTIME_REQUIREMENT,
)
from comfyui_docker_helper.host.uv_runner import locate_host_uv
from comfyui_docker_helper.release_artifacts import (
    PACKAGE_ROOT,
    PRODUCTION_REQUIREMENTS,
    PROJECTED_LICENSE,
    PROJECTED_PYPROJECT,
    PROJECTED_README,
    release_source_files,
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


def test_exact_v05_project_toolchain_metadata() -> None:
    """The package carries the exact release-owned host toolchain ledger."""

    pyproject = _project_metadata()

    assert pyproject["project"]["version"] == "0.5.0"
    assert UV_RUNTIME_REQUIREMENT in pyproject["project"]["dependencies"]
    assert pyproject["build-system"]["requires"] == [UV_BUILD_REQUIREMENT]


def test_frozen_production_closure_matches_repository_lock() -> None:
    runner = locate_host_uv()
    completed = subprocess.run(
        runner.argv(
            (
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--no-header",
                "--no-annotate",
            )
        ),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == PRODUCTION_REQUIREMENTS.read_text(encoding="utf-8")


def test_projected_release_metadata_matches_repository_metadata() -> None:
    repository = _project_metadata()
    projected = tomllib.loads(PROJECTED_PYPROJECT.read_text(encoding="utf-8"))

    assert projected["project"] == repository["project"]
    assert projected["build-system"] == repository["build-system"]
    assert (
        projected["tool"]["uv"]["build-backend"]
        == repository["tool"]["uv"]["build-backend"]
    )
    assert PROJECTED_README.read_bytes() == (PROJECT_ROOT / "README.md").read_bytes()
    assert PROJECTED_LICENSE.read_bytes() == (PROJECT_ROOT / "LICENSE").read_bytes()


def test_projected_release_source_is_entirely_wheel_owned() -> None:
    projected = release_source_files()
    relative_paths = tuple(item.relative_path for item in projected)

    assert len(relative_paths) == len(set(relative_paths))
    for item in projected:
        assert item.source_path.is_relative_to(PACKAGE_ROOT)
