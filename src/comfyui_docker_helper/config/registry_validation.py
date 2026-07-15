"""Shared Registry-node authority rules for plans and direct consumers."""

from __future__ import annotations

from collections.abc import Iterable

from comfyui_docker_helper.config.canonical_lock import normalized_registry_id


def validate_registry_node_authority(
    registry_ids: Iterable[str],
    *,
    install_manager: bool,
    has_manager_plan: bool,
) -> tuple[str, ...]:
    """Validate Manager ownership and unique normalized Registry identities."""
    normalized = tuple(normalized_registry_id(value) for value in registry_ids)
    if normalized and (not install_manager or not has_manager_plan):
        raise ValueError("Registry nodes require Manager")
    if len(normalized) != len(set(normalized)):
        raise ValueError("Registry node identities must be unique")
    return normalized
