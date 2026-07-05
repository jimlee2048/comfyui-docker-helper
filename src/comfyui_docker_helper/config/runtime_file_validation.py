"""Shared runtime file validation helpers."""

from pathlib import PurePosixPath

from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.url_validation import (
    is_http_url,
    validate_file_name,
    validate_relative_file_directory,
)

type RuntimeFilePath = tuple[str | int, ...]


def validate_runtime_file_url(
    value: str | None,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> bool:
    """Append a stable diagnostic when a runtime file URL is invalid."""
    if value is None or is_http_url(value):
        return True
    diagnostics.append(
        Diagnostic(
            path,
            "runtime_file.invalid_url",
            "must be an HTTP(S) URL with a host",
        )
    )
    return False


def normalize_runtime_file_path(
    directory: str,
    filename: str,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> tuple[PurePosixPath, str] | None:
    """Normalize runtime file directory and target path with stable diagnostics."""
    normalized_directory = normalize_runtime_file_directory(
        directory,
        (*path, "dir"),
        diagnostics,
    )
    normalized_filename = normalize_runtime_file_filename(
        filename,
        (*path, "filename"),
        diagnostics,
    )
    if normalized_directory is None or normalized_filename is None:
        return None
    relative_target = (normalized_directory / normalized_filename).as_posix()
    return normalized_directory, relative_target


def normalize_runtime_file_directory(
    value: str,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> PurePosixPath | None:
    """Normalize a runtime file directory or append a stable diagnostic."""
    result = validate_relative_file_directory(value)
    if result.path is not None:
        return result.path
    diagnostics.append(
        Diagnostic(
            path,
            f"runtime_file.{result.code}",
            result.message or "must be a valid relative directory",
        )
    )
    return None


def normalize_runtime_file_filename(
    value: str,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
) -> str | None:
    """Validate a runtime file filename or append a stable diagnostic."""
    result = validate_file_name(value)
    if result.filename is not None:
        return result.filename
    diagnostics.append(
        Diagnostic(
            path,
            "runtime_file.invalid_filename",
            "must be one nonempty filename component",
        )
    )
    return None
