"""Host raw-configuration merge policies."""

from collections.abc import Mapping
from typing import Any

from comfyui_docker_helper.config.merge import (
    ANY_PATH_PART,
    AtomicPolicy,
    KeyedItemMerge,
    KeyedSequencePolicy,
    MergeKey,
    MergePolicyRegistry,
    PolicyRule,
)


def _custom_node_key(item: Any) -> MergeKey | None:
    if not isinstance(item, Mapping):
        return None
    node_type = item.get("type")
    if node_type == "registry":
        node_id = item.get("id")
        return ("registry", node_id) if isinstance(node_id, str) else None
    if node_type == "git":
        url = item.get("url")
        return ("git", url) if isinstance(url, str) else None
    return None


def _file_key(item: Any) -> MergeKey | None:
    if not isinstance(item, Mapping):
        return None
    directory = item.get("dir")
    filename = item.get("filename")
    if isinstance(directory, str) and isinstance(filename, str):
        return ("file", directory, filename)
    return None


def _git_credential_key(item: Any) -> MergeKey | None:
    if not isinstance(item, Mapping):
        return None
    match = item.get("match")
    return ("git-credential", match) if isinstance(match, str) else None


HOST_CONFIG_MERGE_POLICIES = MergePolicyRegistry(
    (
        PolicyRule(
            ("secrets", ANY_PATH_PART),
            AtomicPolicy(),
        ),
        PolicyRule(
            ("comfyui", "custom_nodes"),
            KeyedSequencePolicy(_custom_node_key, KeyedItemMerge.RECURSIVE),
        ),
        PolicyRule(
            ("files",),
            KeyedSequencePolicy(_file_key, KeyedItemMerge.RECURSIVE),
        ),
        PolicyRule(
            ("cdh", "git", "credentials"),
            KeyedSequencePolicy(_git_credential_key, KeyedItemMerge.ATOMIC),
        ),
    )
)
