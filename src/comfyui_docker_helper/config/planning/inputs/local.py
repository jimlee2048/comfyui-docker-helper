"""Process-local planning facts produced by Host local-source admission."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from comfyui_docker_helper.config.planning.local_tree import (
    LocalTreeInventory,
    local_tree_digest,
)
from comfyui_docker_helper.config.validation.urls import (
    is_reserved_file_target_component,
)

_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class LocalFilePlanningInput:
    """Admitted shape and optional content identity for one local file."""

    relative_target: PurePosixPath
    context_path: PurePosixPath
    content_lock: bool
    digest: str | None
    kind: Literal["file"] = "file"

    def __post_init__(self) -> None:
        relative_target = _canonical_relative_target(
            self.relative_target, allow_root=False
        )
        object.__setattr__(self, "relative_target", relative_target)
        _validate_context_path(
            self.context_path,
            prefix="build/files",
            relative_target=relative_target,
        )
        _validate_content_identity(self.content_lock, self.digest, "file")
        if self.kind != "file":
            raise ValueError("local file planning input kind must be file")


@dataclass(frozen=True, slots=True)
class LocalTreePlanningInput:
    """Admitted shape, inventory, and optional content identity for one tree."""

    relative_target: PurePosixPath
    context_path: PurePosixPath
    content_lock: bool
    inventory: LocalTreeInventory
    tree_digest: str | None
    kind: Literal["tree"] = "tree"

    def __post_init__(self) -> None:
        relative_target = _canonical_relative_target(
            self.relative_target, allow_root=True
        )
        object.__setattr__(self, "relative_target", relative_target)
        _validate_context_path(
            self.context_path,
            prefix="build/trees",
            relative_target=relative_target,
        )
        if self.kind != "tree":
            raise ValueError("local tree planning input kind must be tree")
        _validate_content_identity(self.content_lock, self.tree_digest, "tree")
        if self.content_lock and self.tree_digest != local_tree_digest(self.inventory):
            raise ValueError(
                "local tree planning input digest does not match inventory"
            )


type LocalPlanningInput = LocalFilePlanningInput | LocalTreePlanningInput


def index_local_planning_inputs(
    inputs: tuple[LocalPlanningInput, ...] | list[LocalPlanningInput],
) -> dict[str, LocalPlanningInput]:
    """Validate and index admitted inputs by their normalized target identity."""
    indexed: dict[str, LocalPlanningInput] = {}
    for item in inputs:
        if not isinstance(item, (LocalFilePlanningInput, LocalTreePlanningInput)):
            raise ValueError("local planning inputs must use a supported kind")
        key = item.relative_target.as_posix()
        if key in indexed:
            raise ValueError(f"duplicate local planning input for target {key!r}")
        indexed[key] = item
    return indexed


def _canonical_relative_target(
    value: PurePosixPath,
    *,
    allow_root: bool,
) -> PurePosixPath:
    if not isinstance(value, PurePosixPath):
        raise ValueError("local planning input target must be a POSIX path")
    if value.is_absolute() or value.as_posix() != str(value):
        raise ValueError("local planning input target must be canonical")
    if allow_root and value == PurePosixPath("."):
        return value
    if not value.parts or value == PurePosixPath("."):
        raise ValueError("local planning input target must be a non-root path")
    if any(
        part in {"", ".", ".."} or is_reserved_file_target_component(part)
        for part in value.parts
    ):
        raise ValueError("local planning input target must be canonical")
    return value


def _validate_context_path(
    value: PurePosixPath,
    *,
    prefix: str,
    relative_target: PurePosixPath,
) -> None:
    if not isinstance(value, PurePosixPath):
        raise ValueError("local planning input context path must be a POSIX path")
    slot = hashlib.sha256(relative_target.as_posix().encode("utf-8")).hexdigest()
    expected = PurePosixPath(prefix) / slot
    if value != expected:
        raise ValueError("local planning input context path is not canonical")


def _validate_content_identity(
    content_lock: bool,
    digest: str | None,
    kind: Literal["file", "tree"],
) -> None:
    if type(content_lock) is not bool:
        raise ValueError("local planning input content_lock must be one bool")
    if content_lock and (
        not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None
    ):
        raise ValueError(
            f"locked local {kind} planning input requires a SHA-256 digest"
        )
    if not content_lock and digest is not None:
        raise ValueError(f"unlocked local {kind} planning input must omit its digest")


__all__ = [
    "LocalFilePlanningInput",
    "LocalPlanningInput",
    "LocalTreePlanningInput",
    "index_local_planning_inputs",
]
