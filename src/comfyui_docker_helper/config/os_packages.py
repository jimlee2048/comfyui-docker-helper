"""Canonical operating-system package authority."""

import re

DEFAULT_OS_PACKAGES = (
    "bash",
    "ca-certificates",
    "curl",
    "git",
    "build-essential",
    "aria2",
    "openssh-server",
    "tini",
    "tzdata",
)

_DEBIAN_PACKAGE_PATTERN = re.compile(r"[a-z0-9][a-z0-9+.-]+\Z")


def validate_apt_package_identity(value: str) -> str:
    """Return one canonical Debian package name or fail closed."""
    if _DEBIAN_PACKAGE_PATTERN.fullmatch(value) is None:
        raise ValueError("apt package must be one canonical package identity")
    return value
