"""Small lexical primitives shared by consumer-specific validators."""

import unicodedata


def has_control_characters(value: str) -> bool:
    """Return whether ``value`` contains a Unicode control character."""
    return any(unicodedata.category(character) == "Cc" for character in value)


def is_argv_value(value: str) -> bool:
    """Return whether one required subprocess argument is unambiguous."""
    return bool(value.strip()) and not has_control_characters(value)


def replace_control_characters(value: str, replacement: str = " ") -> str:
    """Replace control characters without classifying the surrounding text."""
    return "".join(
        replacement if unicodedata.category(character) == "Cc" else character
        for character in value
    )
