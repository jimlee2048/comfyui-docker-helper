"""Shared runtime hook tree contract constants."""

from __future__ import annotations

import stat
from enum import Enum, auto
from types import MappingProxyType

RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS = (
    ("pre-start", "pre-start.d"),
    ("post-start", "post-start.d"),
    ("stop", "stop.d"),
)
RUNTIME_HOOK_PHASE_DIRECTORIES_BY_PHASE = MappingProxyType(
    dict(RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS)
)
RUNTIME_HOOK_PHASES_BY_DIRECTORY = MappingProxyType(
    {dirname: phase for phase, dirname in RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS}
)
RUNTIME_HOOK_PHASE_DIRECTORY_NAMES = frozenset(
    dirname for _, dirname in RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS
)
RUNTIME_HOOK_SUPPORTED_SUFFIXES = frozenset({".sh", ".py"})
RUNTIME_HOOK_LOCK_PREFIX = "runtime-hooks"
BUILD_HOOK_LOCK_PREFIX = "build-hooks"


class RuntimeHookEntryKind(Enum):
    """Shared semantic classification for one inspected hook-tree entry."""

    SELECTABLE_FILE = auto()
    OTHER_REGULAR_FILE = auto()
    DIRECTORY = auto()
    SYMLINK = auto()
    SPECIAL = auto()


def classify_runtime_hook_entry(mode: int, suffix: str) -> RuntimeHookEntryKind:
    """Classify an already-inspected entry without owning filesystem I/O."""
    if stat.S_ISLNK(mode):
        return RuntimeHookEntryKind.SYMLINK
    if stat.S_ISDIR(mode):
        return RuntimeHookEntryKind.DIRECTORY
    if not stat.S_ISREG(mode):
        return RuntimeHookEntryKind.SPECIAL
    if suffix in RUNTIME_HOOK_SUPPORTED_SUFFIXES:
        return RuntimeHookEntryKind.SELECTABLE_FILE
    return RuntimeHookEntryKind.OTHER_REGULAR_FILE


def runtime_hook_phase_directory_list() -> str:
    """Return the allowed runtime hook phase directories for diagnostics."""
    phase_dirs = tuple(dirname for _, dirname in RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS)
    return f"{', '.join(phase_dirs[:-1])}, and {phase_dirs[-1]}"
