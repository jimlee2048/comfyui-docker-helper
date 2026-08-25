"""Shared Registry-node authority rules for plans and direct consumers."""

from __future__ import annotations

from collections.abc import Iterable

from packaging.utils import InvalidName, canonicalize_name

from comfyui_docker_helper.config.validation.values import has_control_characters


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


def validate_registry_node_authority(
    registry_ids: Iterable[str],
    *,
    install_manager: bool,
    has_manager_plan: bool,
) -> tuple[str, ...]:
    """Validate Manager ownership and unique installed-distribution identities."""
    normalized = tuple(registry_distribution_identity(value) for value in registry_ids)
    if normalized and (not install_manager or not has_manager_plan):
        raise ValueError("Registry nodes require Manager")
    if len(normalized) != len(set(normalized)):
        raise ValueError("Registry node identities must be unique")
    return normalized
