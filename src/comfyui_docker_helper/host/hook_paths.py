"""Lexical host hook roots and their statically observed directory shape."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_absolute_path = os.path.abspath
_platform_name = os.name


def lexical_hook_source_root(
    value: str | Path,
    *,
    working_directory: str | Path | None,
) -> Path:
    """Return a normalized absolute path without resolving filesystem links."""
    base = Path.cwd() if working_directory is None else Path(working_directory)
    selected = Path(value)
    candidate = selected if selected.is_absolute() else base / selected
    root = Path(_absolute_path(candidate))
    if _platform_name == "nt":
        from comfyui_docker_helper._windows_files import (
            validate_local_absolute_path,
        )

        validate_local_absolute_path(os.fspath(root))
    return root


def observed_path_is_real_directory(path: Path) -> bool:
    """Check the currently observed path components without claiming race safety."""
    for component in (*reversed(path.parents), path):
        observed = component.lstat()
        if observed_path_is_reparse(observed) or not stat.S_ISDIR(observed.st_mode):
            return False
    return True


def observed_path_is_reparse(observed: os.stat_result) -> bool:
    """Recognize Unix links and Windows reparse points in one lstat result."""
    return stat.S_ISLNK(observed.st_mode) or bool(
        getattr(observed, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )
