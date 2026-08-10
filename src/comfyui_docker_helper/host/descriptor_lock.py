"""Caller-owned native descriptor locking for host private state."""

from filelock import lock_descriptor, unlock_descriptor


def acquire_descriptor_lock(fd: int, *, blocking: bool = True) -> bool:
    """Acquire the cross-platform native lock without taking fd ownership."""
    return lock_descriptor(fd, blocking=blocking)


def release_descriptor_lock(fd: int) -> None:
    """Release the native lock while leaving the caller-owned fd open."""
    unlock_descriptor(fd)
