"""Shared URL and download-target validation helpers."""

import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from comfyui_docker_helper.config.value_validation import has_control_characters

type DownloaderName = Literal["aria2", "httpx"]

DOWNLOADERS: frozenset[DownloaderName] = frozenset({"aria2", "httpx"})


@dataclass(frozen=True, slots=True)
class RelativeDirectoryValidationResult:
    """Normalized relative directory or a stable validation failure code."""

    path: PurePosixPath | None
    code: (
        Literal[
            "absolute_directory",
            "trailing_slash",
            "empty_directory_segment",
            "current_directory_segment",
            "parent_directory_segment",
            "empty_directory",
            "control_character",
        ]
        | None
    ) = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class FilenameValidationResult:
    """Validated filename or a stable validation failure code."""

    filename: str | None
    code: Literal["invalid_filename"] | None = None
    message: str | None = None


def is_http_url(url: str) -> bool:
    """Return whether a URL is HTTP(S), host-qualified, and consumer-safe."""
    if has_control_characters(url):
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(hostname)
        and "\\" not in parsed.netloc
        and not any(character.isspace() for character in parsed.netloc)
    )


def normalize_downloader_name(value: str) -> DownloaderName | None:
    """Return a supported internal downloader name, if one was provided."""
    if value in DOWNLOADERS:
        return value
    return None


def require_downloader_name(value: str) -> DownloaderName:
    """Return a supported downloader name after public validation has run."""
    downloader = normalize_downloader_name(value)
    if downloader is None:
        raise ValueError(f"unsupported downloader: {value}")
    return downloader


def validate_relative_file_directory(value: str) -> RelativeDirectoryValidationResult:
    """Validate and normalize a runtime-compatible relative file directory."""
    if has_control_characters(value):
        return RelativeDirectoryValidationResult(
            None,
            "control_character",
            "must not contain control characters",
        )
    if value.startswith("/"):
        return RelativeDirectoryValidationResult(
            None,
            "absolute_directory",
            "must be relative",
        )
    if value.endswith("/"):
        return RelativeDirectoryValidationResult(
            None,
            "trailing_slash",
            "must not end with a slash",
        )

    parts = value.split("/")
    if not value or any(part == "" for part in parts):
        return RelativeDirectoryValidationResult(
            None,
            "empty_directory_segment",
            "must not contain empty path segments",
        )
    if any(part == "." for part in parts):
        return RelativeDirectoryValidationResult(
            None,
            "current_directory_segment",
            "must not contain '.'",
        )
    if any(part == ".." for part in parts):
        return RelativeDirectoryValidationResult(
            None,
            "parent_directory_segment",
            "must not contain '..'",
        )

    normalized = PurePosixPath(posixpath.normpath(value))
    if normalized == PurePosixPath("."):
        return RelativeDirectoryValidationResult(
            None,
            "empty_directory",
            "must not be empty",
        )
    return RelativeDirectoryValidationResult(normalized, None)


def validate_file_name(value: str) -> FilenameValidationResult:
    """Validate one nonempty POSIX filename component."""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or has_control_characters(value)
    ):
        return FilenameValidationResult(
            None,
            "invalid_filename",
            "must be one nonempty filename component",
        )
    return FilenameValidationResult(value)
