"""Canonical custom-node error and directory-admission contracts."""

from __future__ import annotations

import stat
from pathlib import Path

from comfyui_docker_helper.errors import ApplicationError


class CustomNodeInstallError(ApplicationError):
    """A custom-node process, placement, or proof invariant failed."""


def _require_real_directory(path: Path, subject: str) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CustomNodeInstallError(f"{subject} is unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path
    ):
        raise CustomNodeInstallError(f"{subject} must be one real directory")
    return resolved
