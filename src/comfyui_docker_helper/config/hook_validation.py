"""Shared scalar and identity rules for content-locked hooks."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from comfyui_docker_helper.config.runtime_hooks import (
    BUILD_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_PHASE_DIRECTORY_NAMES,
    RUNTIME_HOOK_SUPPORTED_SUFFIXES,
)
from comfyui_docker_helper.config.value_validation import has_control_characters

type HookTree = Literal["build", "runtime"]
_SHA256_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


def validate_hook_relative_path(value: str) -> str:
    """Return one canonical safe relative executable-hook path."""
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or has_control_characters(value)
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("hook path must be one canonical safe POSIX path")
    if path.suffix not in RUNTIME_HOOK_SUPPORTED_SUFFIXES:
        raise ValueError("hook path must end in .sh or .py")
    return value


def validate_hook_digest(value: str) -> str:
    """Return one exact lowercase SHA-256 hook digest."""
    if _SHA256_DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError("hook digest must be sha256 followed by 64 lowercase hex")
    return value


def hook_lock_identity(tree: HookTree, relative_path: str) -> str:
    """Return the canonical lock identity for one hook tree member."""
    relative_path = validate_hook_relative_path(relative_path)
    if tree == "runtime":
        path = PurePosixPath(relative_path)
        if (
            len(path.parts) != 2
            or path.parts[0] not in RUNTIME_HOOK_PHASE_DIRECTORY_NAMES
        ):
            raise ValueError(
                "runtime hook path must be one supported phase directory and filename"
            )
        prefix = RUNTIME_HOOK_LOCK_PREFIX
    else:
        prefix = BUILD_HOOK_LOCK_PREFIX
    return f"{prefix}/{relative_path}"


def materialized_hook_identity(tree: HookTree, relative_path: str) -> PurePosixPath:
    """Return the canonical destination identity for one materialized hook."""
    identity = hook_lock_identity(tree, relative_path)
    relative = identity.split("/", 1)[1]
    root = PurePosixPath("runtime/hooks" if tree == "runtime" else "build/hooks")
    return root / relative
