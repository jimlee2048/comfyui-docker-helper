"""Small lexical primitives shared by consumer-specific validators."""

import unicodedata

from packaging.version import InvalidVersion, Version

_MINIMUM_MANAGED_PYTHON = Version("3.12")
_MAXIMUM_MANAGED_PYTHON = Version("3.15")


def has_control_characters(value: str) -> bool:
    """Return whether ``value`` contains a Unicode control character."""
    return any(unicodedata.category(character) == "Cc" for character in value)


def is_argv_value(value: str) -> bool:
    """Return whether one required subprocess argument is unambiguous."""
    return bool(value.strip()) and not has_control_characters(value)


def validate_managed_python_catalog_key(value: str) -> str:
    """Return one opaque, safe managed-Python catalog path component."""
    if (
        not value
        or value in {".", ".."}
        or value != value.strip()
        or "/" in value
        or "\\" in value
        or has_control_characters(value)
    ):
        raise ValueError("catalog_key must be one safe path component")
    return value


def validate_managed_python_support_range(value: str) -> str:
    """Require one managed Python inside the package support range."""
    try:
        version = Version(value)
    except InvalidVersion as error:
        raise ValueError("managed Python must satisfy >=3.12,<3.15") from error
    if not _MINIMUM_MANAGED_PYTHON <= version < _MAXIMUM_MANAGED_PYTHON:
        raise ValueError("managed Python must satisfy >=3.12,<3.15")
    return value


def replace_control_characters(value: str, replacement: str = " ") -> str:
    """Replace control characters without classifying the surrounding text."""
    return "".join(
        replacement if unicodedata.category(character) == "Cc" else character
        for character in value
    )
