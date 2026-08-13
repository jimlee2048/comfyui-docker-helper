"""Host raw-configuration merge policies."""

from collections.abc import Mapping
from typing import Any

from comfyui_docker_helper.config.downloader_credentials import (
    DownloaderCredentialContextError,
    canonicalize_downloader_credential_context,
)
from comfyui_docker_helper.config.git_credentials import (
    GitCredentialContextError,
    canonicalize_git_credential_context,
)
from comfyui_docker_helper.config.merge import (
    ANY_PATH_PART,
    AtomicPolicy,
    KeyedItemMerge,
    KeyedSequencePolicy,
    MergeKey,
    MergePolicyRegistry,
    PolicyRule,
)
from comfyui_docker_helper.config.os_packages import validate_apt_package_identity
from comfyui_docker_helper.config.registry_identity import (
    registry_resource_identity,
)
from comfyui_docker_helper.config.requirement_validation import (
    DirectRequirementError,
    parse_direct_requirement,
)
from comfyui_docker_helper.config.runtime_file_validation import (
    runtime_file_item_merge,
    runtime_file_target_identity,
)


def _apt_package_key(item: Any) -> MergeKey | None:
    if not isinstance(item, str):
        return None
    try:
        identity = validate_apt_package_identity(item)
    except ValueError:
        return None
    return ("apt", identity)


def _direct_requirement_key(item: Any) -> MergeKey | None:
    if not isinstance(item, str):
        return None
    try:
        identity = parse_direct_requirement(item)
    except DirectRequirementError:
        return None
    return ("python-requirement", identity.canonical_value)


def _custom_node_key(item: Any) -> MergeKey | None:
    if not isinstance(item, Mapping):
        return None
    node_type = item.get("type")
    if node_type == "registry":
        node_id = item.get("id")
        if not isinstance(node_id, str):
            return None
        try:
            identity = registry_resource_identity(node_id)
        except ValueError:
            return None
        return ("registry", identity)
    if node_type == "git":
        url = item.get("url")
        return ("git", url) if isinstance(url, str) else None
    return None


def _file_key(item: Any) -> MergeKey | None:
    return runtime_file_target_identity(item)


def _git_credential_key(item: Any) -> MergeKey | None:
    if not isinstance(item, Mapping):
        return None
    match = item.get("match")
    if not isinstance(match, str):
        return None
    try:
        canonical = canonicalize_git_credential_context(match)
    except GitCredentialContextError:
        return None
    return ("git-credential", canonical)


def _downloader_credential_key(item: Any) -> MergeKey | None:
    if not isinstance(item, Mapping):
        return None
    match = item.get("match")
    if not isinstance(match, str):
        return None
    try:
        canonical = canonicalize_downloader_credential_context(match)
    except DownloaderCredentialContextError:
        return None
    return ("downloader-credential", canonical)


HOST_CONFIG_MERGE_POLICIES = MergePolicyRegistry(
    (
        PolicyRule(
            ("secrets", ANY_PATH_PART),
            AtomicPolicy(),
        ),
        PolicyRule(
            ("system", "extra_packages"),
            KeyedSequencePolicy(_apt_package_key, KeyedItemMerge.ATOMIC),
        ),
        PolicyRule(
            ("python", "extra_packages"),
            KeyedSequencePolicy(_direct_requirement_key, KeyedItemMerge.ATOMIC),
        ),
        PolicyRule(
            ("python", "uv_tools"),
            KeyedSequencePolicy(_direct_requirement_key, KeyedItemMerge.ATOMIC),
        ),
        PolicyRule(
            ("pytorch", "extra_packages"),
            KeyedSequencePolicy(_direct_requirement_key, KeyedItemMerge.ATOMIC),
        ),
        PolicyRule(
            ("comfyui", "custom_nodes"),
            KeyedSequencePolicy(_custom_node_key, KeyedItemMerge.RECURSIVE),
        ),
        PolicyRule(
            ("files",),
            KeyedSequencePolicy(_file_key, runtime_file_item_merge),
        ),
        PolicyRule(
            ("cdh", "git", "credentials"),
            KeyedSequencePolicy(_git_credential_key, KeyedItemMerge.ATOMIC),
        ),
        PolicyRule(
            ("cdh", "downloader", "credentials"),
            KeyedSequencePolicy(_downloader_credential_key, KeyedItemMerge.ATOMIC),
        ),
    )
)
