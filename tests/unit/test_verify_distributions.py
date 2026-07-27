"""Unit coverage for built-distribution qualification."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
from tools.ci.verify_distributions import verify_distributions

PROJECT_NAME = "comfyui-docker-helper"
VERSION = "1.2.3"
SDIST_ROOT = "comfyui_docker_helper-1.2.3"
WHEEL_NAME = f"{SDIST_ROOT}-py3-none-any.whl"
SDIST_NAME = f"{SDIST_ROOT}.tar.gz"


def _write_project(project_file: Path) -> None:
    project_file.write_text(
        f'[project]\nname = "{PROJECT_NAME}"\nversion = "{VERSION}"\n',
        encoding="utf-8",
    )


def _regular_member(name: str, content: bytes = b"") -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    return member, content


def _write_sdist(
    path: Path,
    *,
    package_name: str = PROJECT_NAME,
    version: str = VERSION,
    extra_member: tarfile.TarInfo | None = None,
    include_package_info: bool = True,
) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        root = tarfile.TarInfo(SDIST_ROOT)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        if include_package_info:
            metadata = (
                f"Metadata-Version: 2.4\nName: {package_name}\nVersion: {version}\n\n"
            ).encode()
            member, content = _regular_member(f"{SDIST_ROOT}/PKG-INFO", metadata)
            archive.addfile(member, io.BytesIO(content))
        if extra_member is not None:
            archive.addfile(extra_member)


def _write_valid_pair(project_file: Path, dist_dir: Path) -> None:
    _write_project(project_file)
    dist_dir.mkdir()
    (dist_dir / WHEEL_NAME).write_bytes(b"wheel content is checked elsewhere")
    _write_sdist(dist_dir / SDIST_NAME)


def test_distribution_verifier_accepts_valid_pair(tmp_path: Path) -> None:
    project_file = tmp_path / "pyproject.toml"
    dist_dir = tmp_path / "dist"
    _write_valid_pair(project_file, dist_dir)

    verify_distributions(project_file, dist_dir)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda dist: (dist / SDIST_NAME).unlink(),
        lambda dist: (dist / "extra.txt").write_text("extra", encoding="utf-8"),
        lambda dist: (
            (dist / WHEEL_NAME).unlink(),
            (dist / WHEEL_NAME).symlink_to(dist.parent / "wheel-target"),
        ),
        lambda dist: (
            (dist / WHEEL_NAME).unlink(),
            (dist / f"another_project-{VERSION}-py3-none-any.whl").write_bytes(b""),
        ),
        lambda dist: (
            (dist / WHEEL_NAME).unlink(),
            (dist / "comfyui_docker_helper-1.2.4-py3-none-any.whl").write_bytes(b""),
        ),
        lambda dist: (
            (dist / WHEEL_NAME).unlink(),
            (dist / f"comfyui_docker_helper-{VERSION}-1-py3-none-any.whl").write_bytes(
                b""
            ),
        ),
        lambda dist: (
            (dist / WHEEL_NAME).unlink(),
            (dist / f"{SDIST_ROOT}-py3-none-linux_x86_64.whl").write_bytes(b""),
        ),
        lambda dist: (dist / SDIST_NAME).rename(
            dist / "comfyui_docker_helper-1.2.4.tar.gz"
        ),
        lambda dist: (dist / SDIST_NAME).rename(
            dist / f"another_project-{VERSION}.tar.gz"
        ),
    ],
    ids=[
        "missing",
        "extra",
        "top-level-symlink",
        "wheel-name",
        "wheel-version",
        "wheel-build-tag",
        "wheel-tag",
        "sdist-version",
        "sdist-name",
    ],
)
def test_distribution_verifier_rejects_output_set_or_identity(
    tmp_path: Path, mutate: Callable[[Path], object]
) -> None:
    project_file = tmp_path / "pyproject.toml"
    dist_dir = tmp_path / "dist"
    _write_valid_pair(project_file, dist_dir)
    (tmp_path / "wheel-target").write_bytes(b"target")
    mutate(dist_dir)

    with pytest.raises(SystemExit):
        verify_distributions(project_file, dist_dir)


@pytest.mark.parametrize(
    "member_name",
    [
        "/absolute",
        f"{SDIST_ROOT}/../escape",
        f"{SDIST_ROOT}\\backslash",
        "another-root/file",
    ],
)
def test_distribution_verifier_rejects_unsafe_sdist_path(
    tmp_path: Path, member_name: str
) -> None:
    project_file = tmp_path / "pyproject.toml"
    dist_dir = tmp_path / "dist"
    _write_valid_pair(project_file, dist_dir)
    member, _ = _regular_member(member_name)
    _write_sdist(dist_dir / SDIST_NAME, extra_member=member)

    with pytest.raises(SystemExit, match="unsafe entry"):
        verify_distributions(project_file, dist_dir)


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.CHRTYPE])
def test_distribution_verifier_rejects_non_regular_sdist_member(
    tmp_path: Path, member_type: bytes
) -> None:
    project_file = tmp_path / "pyproject.toml"
    dist_dir = tmp_path / "dist"
    _write_valid_pair(project_file, dist_dir)
    member = tarfile.TarInfo(f"{SDIST_ROOT}/special")
    member.type = member_type
    if member_type == tarfile.SYMTYPE:
        member.linkname = "target"
    _write_sdist(dist_dir / SDIST_NAME, extra_member=member)

    with pytest.raises(SystemExit, match="unsafe entry"):
        verify_distributions(project_file, dist_dir)


@pytest.mark.parametrize(
    ("include_package_info", "package_name", "version", "message"),
    [
        (False, PROJECT_NAME, VERSION, "no PKG-INFO"),
        (True, "another-project", VERSION, "metadata identity"),
        (True, PROJECT_NAME, "1.2.4", "metadata identity"),
    ],
)
def test_distribution_verifier_rejects_missing_or_mismatched_package_info(
    tmp_path: Path,
    include_package_info: bool,
    package_name: str,
    version: str,
    message: str,
) -> None:
    project_file = tmp_path / "pyproject.toml"
    dist_dir = tmp_path / "dist"
    _write_valid_pair(project_file, dist_dir)
    _write_sdist(
        dist_dir / SDIST_NAME,
        include_package_info=include_package_info,
        package_name=package_name,
        version=version,
    )

    with pytest.raises(SystemExit, match=message):
        verify_distributions(project_file, dist_dir)
