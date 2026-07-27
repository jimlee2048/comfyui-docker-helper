"""Verify the identity and source-archive safety of built distributions."""

from __future__ import annotations

import os
import tarfile
import tomllib
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)


def _project_identity(project_file: Path) -> tuple[str, str]:
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
    return canonicalize_name(project["name"]), project["version"]


def verify_distributions(project_file: Path, dist_dir: Path) -> None:
    """Verify an exact wheel/sdist pair against project metadata."""

    normalized_name, version = _project_identity(project_file)
    entries = tuple(sorted(dist_dir.iterdir()))
    if len(entries) != 2 or any(
        not entry.is_file() or entry.is_symlink() for entry in entries
    ):
        raise SystemExit("distribution set is not two regular files")

    wheels = tuple(entry for entry in entries if entry.suffix == ".whl")
    sdists = tuple(entry for entry in entries if entry.name.endswith(".tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit("distribution set is not one wheel and one sdist")
    wheel = wheels[0]
    sdist = sdists[0]

    try:
        wheel_distribution, wheel_version, wheel_build, wheel_tags = (
            parse_wheel_filename(wheel.name)
        )
    except InvalidWheelFilename as error:
        raise SystemExit("wheel filename is invalid") from error
    if (
        canonicalize_name(wheel_distribution) != normalized_name
        or str(wheel_version) != version
        or wheel_build
        or {str(tag) for tag in wheel_tags} != {"py3-none-any"}
    ):
        raise SystemExit("wheel filename identity is invalid")

    try:
        sdist_distribution, sdist_version = parse_sdist_filename(sdist.name)
    except InvalidSdistFilename as error:
        raise SystemExit("sdist filename is invalid") from error
    if (
        canonicalize_name(sdist_distribution) != normalized_name
        or str(sdist_version) != version
    ):
        raise SystemExit("sdist filename identity is invalid")

    filename_name = normalized_name.replace("-", "_")
    sdist_root = f"{filename_name}-{version}"
    package_info_name = f"{sdist_root}/PKG-INFO"
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise SystemExit("sdist archive is empty")
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or not path.parts
                or path.parts[0] != sdist_root
                or ".." in path.parts
                or "\\" in member.name
                or not (member.isfile() or member.isdir())
            ):
                raise SystemExit("sdist archive contains an unsafe entry")
        try:
            package_info_file = archive.extractfile(package_info_name)
        except KeyError as error:
            raise SystemExit("sdist has no PKG-INFO") from error
        if package_info_file is None:
            raise SystemExit("sdist has no PKG-INFO")
        package_info = BytesParser().parsebytes(package_info_file.read())

    if (
        canonicalize_name(package_info.get("Name", "")) != normalized_name
        or package_info.get("Version") != version
    ):
        raise SystemExit("sdist metadata identity is invalid")


def main() -> None:
    """Run distribution verification from the GitHub Actions environment."""

    verify_distributions(Path("pyproject.toml"), Path(os.environ["DIST_DIR"]))


if __name__ == "__main__":
    main()
