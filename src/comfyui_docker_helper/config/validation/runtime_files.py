"""Shared HTTP-file and direct-target validation helpers.

The authored and mounted runtime configuration surfaces use one direct
``source + target`` operation. Internal runtime transfer models deliberately
rename the source to ``url`` after this boundary; this module owns the
shape-independent target grammar used before that projection.
"""

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.merge import KeyedItemMerge
from comfyui_docker_helper.config.validation.urls import (
    TRANSFER_STAGING_DIRECTORY_NAME,
    is_http_url,
    is_reserved_file_target_component,
)
from comfyui_docker_helper.config.validation.values import has_control_characters

type RuntimeFilePath = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class RelativeFileTargetValidationResult:
    """Normalized direct target or a stable validation failure code."""

    path: PurePosixPath | None
    code: (
        Literal[
            "absolute_target",
            "parent_target_segment",
            "empty_target",
            "control_character",
            "backslash",
            "trailing_slash",
            "reserved_target_component",
            "root_target",
        ]
        | None
    ) = None
    message: str | None = None


def runtime_file_target_identity(item: object) -> tuple[str, ...] | None:
    """Return a pure merge key for one raw direct-target file item.

    A raw layered patch may contain only its merge key. Consequently this
    helper intentionally does not require ``type`` or ``source`` and only
    returns an identity when the target itself is well-formed.
    """
    if not isinstance(item, Mapping):
        return None
    target = item.get("target")
    if not isinstance(target, str):
        return None
    result = validate_relative_file_target(target)
    if result.path is None:
        return None
    return ("runtime-file", result.path.as_posix())


def runtime_file_item_merge(base: object, override: object) -> KeyedItemMerge:
    """Replace a file item atomically only when its source variant changes."""
    if not isinstance(base, Mapping) or not isinstance(override, Mapping):
        return KeyedItemMerge.ATOMIC
    override_type = override.get("type")
    if override_type is None or override_type == base.get("type"):
        return KeyedItemMerge.RECURSIVE
    return KeyedItemMerge.ATOMIC


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


def validate_relative_file_target(
    value: str,
    *,
    allow_root: bool = True,
) -> RelativeFileTargetValidationResult:
    """Validate and normalize one direct relative POSIX file target.

    ``.`` is the explicit destination-root sentinel used by local directory
    inputs. HTTP and local-file callers pass ``allow_root=False`` because
    those variants require an exact file path.
    """
    if not value:
        return RelativeFileTargetValidationResult(
            None,
            "empty_target",
            "must not be empty",
        )
    if has_control_characters(value):
        return RelativeFileTargetValidationResult(
            None,
            "control_character",
            "must not contain control characters",
        )
    if "\\" in value:
        return RelativeFileTargetValidationResult(
            None,
            "backslash",
            "must use relative POSIX path separators",
        )
    if value.startswith("/"):
        return RelativeFileTargetValidationResult(
            None,
            "absolute_target",
            "must be relative",
        )
    if value.endswith("/"):
        return RelativeFileTargetValidationResult(
            None,
            "trailing_slash",
            "must not use trailing-slash directory spelling",
        )

    if any(part == ".." for part in value.split("/")):
        return RelativeFileTargetValidationResult(
            None,
            "parent_target_segment",
            "must not contain '..'",
        )

    normalized = PurePosixPath(posixpath.normpath(value))
    if not allow_root and normalized == PurePosixPath("."):
        return RelativeFileTargetValidationResult(
            None,
            "root_target",
            "must name an exact file below COMFYUI_PATH",
        )
    if any(is_reserved_file_target_component(part) for part in normalized.parts):
        return RelativeFileTargetValidationResult(
            None,
            "reserved_target_component",
            f"contains {TRANSFER_STAGING_DIRECTORY_NAME!r}, which is reserved for "
            "HTTP download staging; rename or remove that path component",
        )
    return RelativeFileTargetValidationResult(normalized)


def relative_file_targets_overlap(
    earlier: PurePosixPath | str,
    later: PurePosixPath | str,
) -> bool:
    """Return whether two normalized target regions equal or contain one another."""
    earlier_path = PurePosixPath(earlier)
    later_path = PurePosixPath(later)
    earlier_parts = () if earlier_path == PurePosixPath(".") else earlier_path.parts
    later_parts = () if later_path == PurePosixPath(".") else later_path.parts
    return (
        earlier_parts[: len(later_parts)] == later_parts
        or later_parts[: len(earlier_parts)] == earlier_parts
    )


def normalize_runtime_file_target(
    value: str,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
    *,
    allow_root: bool = False,
) -> PurePosixPath | None:
    """Normalize a runtime direct target and append a stable diagnostic."""
    result = validate_relative_file_target(value, allow_root=allow_root)
    if result.path is not None:
        return result.path
    diagnostics.append(
        Diagnostic(
            path,
            f"runtime_file.{result.code}",
            result.message or "must be a valid relative target",
        )
    )
    return None


def normalize_authored_file_target(
    value: str,
    path: RuntimeFilePath,
    diagnostics: list[Diagnostic],
    *,
    allow_root: bool = True,
) -> PurePosixPath | None:
    """Normalize an authored direct target and append a file diagnostic."""
    result = validate_relative_file_target(value, allow_root=allow_root)
    if result.path is not None:
        return result.path
    diagnostics.append(
        Diagnostic(
            path,
            f"file.{result.code}",
            result.message or "must be a valid relative target",
        )
    )
    return None
