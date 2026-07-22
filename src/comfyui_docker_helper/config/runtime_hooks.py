"""Shared runtime hook tree contract constants."""

from __future__ import annotations

from types import MappingProxyType

RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS = (
    ("pre-start", "pre-start.d"),
    ("post-start", "post-start.d"),
    ("stop", "stop.d"),
)
RUNTIME_HOOK_PHASE_DIRECTORIES_BY_PHASE = MappingProxyType(
    dict(RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS)
)
RUNTIME_HOOK_PHASE_DIRECTORY_NAMES = frozenset(
    dirname for _, dirname in RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS
)
RUNTIME_HOOK_SUPPORTED_SUFFIXES = frozenset({".sh", ".py"})
RUNTIME_HOOK_LOCK_PREFIX = "runtime-hooks"
BUILD_HOOK_LOCK_PREFIX = "build-hooks"


def runtime_hook_phase_directory_list() -> str:
    """Return the allowed runtime hook phase directories for diagnostics."""
    phase_dirs = tuple(dirname for _, dirname in RUNTIME_HOOK_PHASE_DIRECTORY_ITEMS)
    return f"{', '.join(phase_dirs[:-1])}, and {phase_dirs[-1]}"
