"""Release-owned projected source and frozen production-closure artifacts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PACKAGE_ROOT = Path(__file__).parent
RELEASE_ASSETS = PACKAGE_ROOT / "templates" / "cdh-release"
PROJECTED_PYPROJECT = RELEASE_ASSETS / "pyproject.toml"
PROJECTED_README = RELEASE_ASSETS / "README.md"
PROJECTED_LICENSE = RELEASE_ASSETS / "LICENSE"
PRODUCTION_REQUIREMENTS = PACKAGE_ROOT / "templates" / "cdh-production-requirements.txt"

_HASH_SUFFIX = re.compile(r"\s+--hash=sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ProjectedSourceFile:
    """One wheel-owned file and its path in the projected build source."""

    relative_path: PurePosixPath
    source_path: Path


def release_source_files() -> tuple[ProjectedSourceFile, ...]:
    """Return the complete deterministic source projection identity."""
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
        ProjectedSourceFile(PurePosixPath("README.md"), PROJECTED_README),
        ProjectedSourceFile(PurePosixPath("LICENSE"), PROJECTED_LICENSE),
        *package_files,
    )


def release_source_digest() -> str:
    """Hash release-owned source and closure bytes with canonical paths."""
    digest = hashlib.sha256()
    for item in release_source_files():
        relative = item.relative_path.as_posix().encode("utf-8")
        content = item.source_path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def production_requirements_digest() -> str:
    """Bind the checked-in exact uv export independently from source layout."""
    return f"sha256:{hashlib.sha256(PRODUCTION_REQUIREMENTS.read_bytes()).hexdigest()}"


def production_inventory(python_version: str) -> tuple[tuple[str, str], ...]:
    """Evaluate the frozen Linux closure for one exact managed interpreter."""
    environment = default_environment()
    environment.update(
        python_version=".".join(python_version.split(".")[:2]),
        python_full_version=python_version,
        os_name="posix",
        platform_machine="x86_64",
        sys_platform="linux",
    )
    logical = PRODUCTION_REQUIREMENTS.read_text(encoding="utf-8").replace("\\\n", " ")
    result: list[tuple[str, str]] = []
    for line in logical.splitlines():
        value = _HASH_SUFFIX.sub("", line).strip()
        if not value or value.startswith("#"):
            continue
        requirement = Requirement(value)
        if requirement.marker is not None and not requirement.marker.evaluate(
            environment
        ):
            continue
        pins = tuple(requirement.specifier)
        if len(pins) != 1 or pins[0].operator != "==":
            raise ValueError("production closure must contain only exact requirements")
        result.append((canonicalize_name(requirement.name), pins[0].version))
    if len(result) != len({name for name, _ in result}):
        raise ValueError("production closure contains duplicate distributions")
    return tuple(sorted(result))
