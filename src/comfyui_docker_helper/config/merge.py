"""Raw TOML document merge helpers for layered host configuration."""

from collections import Counter
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

type RawDocument = Mapping[str, Any]
type RawPath = tuple[str, ...]
type MergeKey = tuple[str, ...]

_CUSTOM_NODES_PATH = ("comfyui", "custom_nodes")
_FILES_PATH = ("files",)
_GIT_CREDENTIALS_PATH = ("cdh", "git", "credentials")
_SECRETS_PATH = ("secrets",)
_KEYED_ARRAY_PATHS = frozenset({_CUSTOM_NODES_PATH, _FILES_PATH, _GIT_CREDENTIALS_PATH})


def merge_toml_documents(documents: Iterable[RawDocument]) -> dict[str, Any]:
    """Merge raw TOML documents using the public layered-config contract."""
    merged: dict[str, Any] = {}
    for document in documents:
        merged = _merge_mapping(merged, document, ())
    return merged


def _merge_mapping(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
    path: RawPath,
) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, override_value in override.items():
        item_path = (*path, key)
        if key in merged:
            merged[key] = _merge_value(merged[key], override_value, item_path)
        else:
            merged[key] = deepcopy(override_value)
    return merged


def _merge_value(base: Any, override: Any, path: RawPath) -> Any:
    if (
        len(path) == 2
        and path[:1] == _SECRETS_PATH
        and isinstance(base, Mapping)
        and isinstance(override, Mapping)
    ):
        return deepcopy(override)

    if isinstance(base, Mapping) and isinstance(override, Mapping):
        return _merge_mapping(base, override, path)

    if (
        path in _KEYED_ARRAY_PATHS
        and isinstance(base, list)
        and isinstance(override, list)
    ):
        return _merge_keyed_array(base, override, path)

    return deepcopy(override)


def _merge_keyed_array(
    base: list[Any], override: list[Any], path: RawPath
) -> list[Any]:
    if not override:
        return []
    if not base:
        return deepcopy(override)

    result = deepcopy(base)
    base_counts = Counter(_iter_keys(base, path))
    override_counts = Counter(_iter_keys(override, path))
    unique_base_indexes = _unique_key_indexes(result, path, base_counts)

    for item in override:
        key = _item_key(item, path)
        if key is not None and base_counts[key] == 1 and override_counts[key] == 1:
            index = unique_base_indexes[key]
            result[index] = (
                deepcopy(item)
                if path == _GIT_CREDENTIALS_PATH
                else _merge_value(result[index], item, (*path, "*"))
            )
        else:
            result.append(deepcopy(item))

    return result


def _iter_keys(items: list[Any], path: RawPath) -> Iterable[MergeKey]:
    for item in items:
        key = _item_key(item, path)
        if key is not None:
            yield key


def _unique_key_indexes(
    items: list[Any],
    path: RawPath,
    counts: Counter[MergeKey],
) -> dict[MergeKey, int]:
    indexes: dict[MergeKey, int] = {}
    for index, item in enumerate(items):
        key = _item_key(item, path)
        if key is not None and counts[key] == 1:
            indexes[key] = index
    return indexes


def _item_key(item: Any, path: RawPath) -> MergeKey | None:
    if not isinstance(item, Mapping):
        return None

    if path == _CUSTOM_NODES_PATH:
        node_type = item.get("type")
        if node_type == "registry":
            node_id = item.get("id")
            return ("registry", node_id) if isinstance(node_id, str) else None
        if node_type == "git":
            url = item.get("url")
            return ("git", url) if isinstance(url, str) else None
        return None

    if path == _FILES_PATH:
        directory = item.get("dir")
        filename = item.get("filename")
        if isinstance(directory, str) and isinstance(filename, str):
            return ("file", directory, filename)

    if path == _GIT_CREDENTIALS_PATH:
        match = item.get("match")
        return ("git-credential", match) if isinstance(match, str) else None

    return None
