"""Registry resource and installed-distribution identity authorities."""

from packaging.utils import InvalidName, canonicalize_name

from comfyui_docker_helper.config.value_validation import has_control_characters


def validate_registry_id(value: str) -> str:
    """Return one argv-safe valid Registry resource ID or raise ValueError."""
    if not value or value != value.strip() or has_control_characters(value):
        raise ValueError("id must be one canonical non-empty value")
    if value.startswith("-"):
        raise ValueError("id must be one argv-safe Registry ID")
    try:
        canonicalize_name(value, validate=True)
    except InvalidName as error:
        raise ValueError("id must be one valid Registry project name") from error
    return value


def registry_resource_identity(value: str) -> str:
    """Return the lowercase-only identity of one valid Registry resource ID."""
    return validate_registry_id(value).lower()


def registry_distribution_identity(value: str) -> str:
    """Return the PyPA installed-distribution identity for one Registry ID."""
    return canonicalize_name(validate_registry_id(value), validate=True)
