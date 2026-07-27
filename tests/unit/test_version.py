"""Tests for package version lookup."""

import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from comfyui_docker_helper import version as version_module


# Version reporting prefers installed metadata and supports source-tree execution.
def test_package_version_uses_distribution_metadata(monkeypatch) -> None:
    """Prefer installed package metadata for runtime version reporting."""

    def distribution_version(name: str) -> str:
        assert name == "comfyui-docker-helper"
        return "1.2.3"

    monkeypatch.setattr(version_module.metadata, "version", distribution_version)

    assert version_module.package_version() == "1.2.3"


def test_package_version_falls_back_to_current_project_pyproject(monkeypatch) -> None:
    """Keep source-checkout CLI execution working without installed metadata."""

    def missing_distribution(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(version_module.metadata, "version", missing_distribution)

    with open("pyproject.toml", "rb") as file:
        expected = tomllib.load(file)["project"]["version"]

    assert version_module.package_version() == expected


def test_package_version_fails_without_distribution_or_project_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail closed when neither supported version authority is available."""

    def missing_distribution(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    package_file = tmp_path / "src" / "comfyui_docker_helper" / "version.py"
    monkeypatch.setattr(version_module.metadata, "version", missing_distribution)
    monkeypatch.setattr(version_module, "__file__", str(package_file))

    with pytest.raises(
        RuntimeError, match="could not determine comfyui-docker-helper version"
    ):
        version_module.package_version()
