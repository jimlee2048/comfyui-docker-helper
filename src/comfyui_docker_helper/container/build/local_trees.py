"""Plan-driven normalization of selected local-tree image paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from comfyui_docker_helper.container.build.admission import LocalTreeNormalizationInput
from comfyui_docker_helper.errors import ApplicationError

_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class LocalTreeNormalizationError(ApplicationError):
    """A selected local-tree path could not be normalized safely."""


def normalize_local_trees(
    trees: tuple[LocalTreeNormalizationInput, ...],
    comfyui_root: str | os.PathLike[str],
) -> None:
    """Admit and normalize only the Plan-selected tree roots and members.

    The BuildPlan is the complete source of expected paths.  This operation
    deliberately never enumerates a destination directory: unrelated lower
    entries remain untouched and are outside this mutation authority.
    """
    root = PurePosixPath(os.fspath(comfyui_root))
    if not root.is_absolute():
        raise LocalTreeNormalizationError("COMFYUI_PATH must be absolute")
    for tree in trees:
        target = _tree_target(tree.target, root)
        _ensure_target_root(root, target, mode=int(tree.root_mode, 8))
        for member in tree.members:
            member_path = PurePosixPath(member.relative_path)
            destination = target / member_path
            if (
                member_path.is_absolute()
                or member_path == PurePosixPath(".")
                or ".." in member_path.parts
                or not destination.is_relative_to(target)
            ):
                raise LocalTreeNormalizationError(
                    "local tree member path escapes its selected target"
                )
            if member.kind == "directory":
                _ensure_selected_directory(destination, mode=int(member.mode, 8))
            else:
                _normalize_selected_file(destination, mode=int(member.mode, 8))


def _tree_target(
    value: str,
    root: PurePosixPath,
) -> PurePosixPath:
    target = PurePosixPath(value)
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise LocalTreeNormalizationError(
            "local tree target escapes COMFYUI_PATH"
        ) from error
    if ".." in relative.parts:
        raise LocalTreeNormalizationError("local tree target escapes COMFYUI_PATH")
    return target


def _ensure_target_root(
    root: PurePosixPath,
    target: PurePosixPath,
    *,
    mode: int,
) -> None:
    """Create target ancestors as needed, admitting every traversed node."""
    _admit_or_create_directory(
        Path(root),
        label="COMFYUI_PATH",
        create=target == root,
        mode=mode if target == root else None,
    )
    relative = target.relative_to(root)
    current = Path(root)
    for part in relative.parts:
        current /= part
        is_target = current == target
        _admit_or_create_directory(
            current,
            label="local tree target root" if is_target else "local tree target parent",
            create=True,
            mode=mode if is_target else None,
        )


def _ensure_selected_directory(path: PurePosixPath, *, mode: int) -> None:
    _admit_or_create_directory(
        Path(path), label="local tree selected directory", create=True, mode=mode
    )


def _admit_or_create_directory(
    path: Path,
    *,
    label: str,
    create: bool,
    mode: int | None,
) -> None:
    observed = _lstat(path, label)
    created = False
    if observed is None:
        if not create:
            raise LocalTreeNormalizationError(f"{label} is missing")
        try:
            path.mkdir(mode=0o755)
        except OSError as error:
            raise LocalTreeNormalizationError(
                f"{label} could not be created"
            ) from error
        observed = _lstat(path, label)
        if observed is None:
            raise LocalTreeNormalizationError(f"{label} disappeared after creation")
        created = True
    _require_real_directory(observed, label)
    if mode is not None or created:
        _set_mode(path, 0o755 if mode is None else mode, label)


def _normalize_selected_file(path: PurePosixPath, *, mode: int) -> None:
    destination = Path(path)
    observed = _lstat(destination, "local tree selected file")
    if observed is None:
        raise LocalTreeNormalizationError("local tree selected file is missing")
    if _is_reparse(observed):
        raise LocalTreeNormalizationError(
            "local tree selected file must not be a link or reparse point"
        )
    if not stat.S_ISREG(observed.st_mode):
        if stat.S_ISDIR(observed.st_mode):
            raise LocalTreeNormalizationError(
                "local tree selected file conflicts with a directory"
            )
        raise LocalTreeNormalizationError(
            "local tree selected file is a special filesystem node"
        )
    _set_mode(destination, mode, "local tree selected file")


def _lstat(path: Path, label: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LocalTreeNormalizationError(f"{label} could not be inspected") from error


def _require_real_directory(observed: os.stat_result, label: str) -> None:
    if _is_reparse(observed):
        raise LocalTreeNormalizationError(
            f"{label} must not be a link or reparse point"
        )
    if not stat.S_ISDIR(observed.st_mode):
        if stat.S_ISREG(observed.st_mode):
            raise LocalTreeNormalizationError(f"{label} conflicts with a file")
        raise LocalTreeNormalizationError(f"{label} is a special filesystem node")


def _set_mode(path: Path, mode: int, label: str) -> None:
    try:
        os.chmod(path, mode, follow_symlinks=False)
        observed = path.lstat()
    except OSError as error:
        raise LocalTreeNormalizationError(
            f"{label} mode could not be normalized"
        ) from error
    if _is_reparse(observed) or stat.S_IMODE(observed.st_mode) != mode:
        raise LocalTreeNormalizationError(f"{label} mode could not be normalized")


def _is_reparse(observed: os.stat_result) -> bool:
    return stat.S_ISLNK(observed.st_mode) or bool(
        getattr(observed, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


__all__ = [
    "LocalTreeNormalizationError",
    "normalize_local_trees",
]
