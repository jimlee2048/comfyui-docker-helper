"""Canonical validation for optional downloaded-file content identity."""

import re

_SHA256_CHECKSUM = re.compile(r"sha256:([0-9A-Fa-f]{64})")
_CANONICAL_SHA256_CHECKSUM = re.compile(r"sha256:[0-9a-f]{64}")


def normalize_file_checksum(value: str) -> str:
    """Validate public checksum syntax and normalize equivalent hex case."""
    match = _SHA256_CHECKSUM.fullmatch(value)
    if match is None:
        raise ValueError("must be sha256:<64 hexadecimal digits>")
    return f"sha256:{match.group(1).lower()}"


def validate_canonical_file_checksum(value: str) -> str:
    """Require the canonical lowercase representation at internal boundaries."""
    if _CANONICAL_SHA256_CHECKSUM.fullmatch(value) is None:
        raise ValueError("checksum must be canonical sha256:<64 lowercase hex>")
    return value
