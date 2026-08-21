"""Package-owned inputs for rebuilding the canonical cdh wheel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PACKAGE_ROOT = Path(__file__).parent
RELEASE_PROJECTION = PACKAGE_ROOT / "resources" / "release-projection"
PROJECTED_PYPROJECT = RELEASE_PROJECTION / "pyproject.toml"
PROJECTED_LICENSE = RELEASE_PROJECTION / "LICENSE"
WORKSPACE_PROFILE_RESOURCE = PACKAGE_ROOT / "resources" / "cdh-workspace.sh"
WORKSPACE_PROFILE_WHEEL_MEMBER = PurePosixPath(
    "comfyui_docker_helper/resources/cdh-workspace.sh"
)
WORKSPACE_PROFILE_CONTEXT_PATH = PurePosixPath("runtime/cdh-workspace.sh")


@dataclass(frozen=True, slots=True)
class ProjectedSourceFile:
    """One wheel-owned file and its path in the temporary release projection."""

    relative_path: PurePosixPath
    source_path: Path


@dataclass(frozen=True, slots=True)
class CanonicalWheel:
    """One fully validated canonical wheel held across planning/materialization."""

    filename: str
    version: str
    digest: str
    content: bytes


def release_projection_files() -> tuple[ProjectedSourceFile, ...]:
    """Return the complete deterministic source projection for one wheel build."""
    package_files = tuple(
        ProjectedSourceFile(
            PurePosixPath("src/comfyui_docker_helper")
            / path.relative_to(PACKAGE_ROOT).as_posix(),
            path,
        )
        for path in sorted(PACKAGE_ROOT.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    )
    return (
        ProjectedSourceFile(PurePosixPath("pyproject.toml"), PROJECTED_PYPROJECT),
        ProjectedSourceFile(PurePosixPath("LICENSE"), PROJECTED_LICENSE),
        *package_files,
    )
