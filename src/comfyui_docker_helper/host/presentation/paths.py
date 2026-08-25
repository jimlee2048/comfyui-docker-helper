"""Lexical, native-flavor display of approved Host paths."""

from __future__ import annotations

import ntpath
import posixpath
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from comfyui_docker_helper.cli_output.text import control_safe_text


def display_host_path(
    path: str | PurePath,
    *,
    working_directory: str | PurePath | None = None,
) -> str:
    """Return a control-safe relative-inside/absolute-outside Host path."""
    cwd_input = Path.cwd() if working_directory is None else working_directory
    path_type = _path_type(path, cwd_input)
    cwd = _normalized(path_type(cwd_input), path_type)
    if not cwd.is_absolute():
        raise ValueError("The Host display working directory must be absolute.")

    candidate = path_type(path)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = _normalized(candidate, path_type)

    try:
        displayed = candidate.relative_to(cwd)
    except ValueError:
        displayed = candidate
    value = "." if str(displayed) == "." else str(displayed)
    return control_safe_text(value, escape_backslashes=False)


def _path_type(
    path: str | PurePath,
    working_directory: str | PurePath,
) -> type[PurePosixPath] | type[PureWindowsPath]:
    for candidate in (path, working_directory):
        if isinstance(candidate, PureWindowsPath):
            return PureWindowsPath
        if isinstance(candidate, PurePosixPath):
            return PurePosixPath
    return PureWindowsPath if isinstance(Path(), PureWindowsPath) else PurePosixPath


def _normalized(
    path: PurePosixPath | PureWindowsPath,
    path_type: type[PurePosixPath] | type[PureWindowsPath],
) -> PurePosixPath | PureWindowsPath:
    normalizer = ntpath.normpath if path_type is PureWindowsPath else posixpath.normpath
    return path_type(normalizer(str(path)))
