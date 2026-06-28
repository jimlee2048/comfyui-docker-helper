"""Package version helpers."""

import tomllib
from importlib import metadata
from pathlib import Path

_DISTRIBUTION_NAME = "comfyui-docker-helper"


def package_version() -> str:
    """Return the installed distribution version for the CLI."""
    try:
        return metadata.version(_DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return _current_project_version()


def _current_project_version() -> str:
    package_file = Path(__file__).resolve()
    for parent in package_file.parents:
        pyproject = parent / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as file:
                project = tomllib.load(file).get("project", {})
            if project.get("name") == _DISTRIBUTION_NAME:
                version = project.get("version")
                if isinstance(version, str) and version:
                    return version
    raise RuntimeError(f"could not determine {_DISTRIBUTION_NAME} version")
