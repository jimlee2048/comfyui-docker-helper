"""Policy-driven raw TOML document merging with parallel authorship metadata."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from types import MappingProxyType
from typing import Any, Protocol

from comfyui_docker_helper.config.diagnostics import (
    DiagnosticPath,
    DiagnosticPathPart,
    SourceLocation,
    SourceReference,
)

type RawDocument = Mapping[str, Any]
type MergeKey = tuple[str, ...]
type PolicyPath = tuple[DiagnosticPathPart, ...]


class IdentityFunction(Protocol):
    """Return a stable identity for one raw keyed-sequence item."""

    def __call__(self, item: Any) -> MergeKey | None: ...


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """One parsed TOML document paired with its ordered source identity."""

    source: SourceReference
    document: RawDocument


@dataclass(frozen=True, slots=True)
class OriginNode:
    """Immutable authorship metadata parallel to one effective raw value."""

    authored_at: SourceLocation | None = None
    replacement_owner: SourceLocation | None = None
    contributors: tuple[SourceLocation, ...] = ()
    children: Mapping[DiagnosticPathPart, OriginNode] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "contributors", tuple(self.contributors))
        object.__setattr__(self, "children", MappingProxyType(dict(self.children)))

    def node_at(self, path: DiagnosticPath) -> OriginNode | None:
        """Return the exact effective origin node without ancestor fallback."""
        node: OriginNode = self
        for part in path:
            child = node.children.get(part)
            if child is None:
                return None
            node = child
        return node

    def exact_location(self, path: DiagnosticPath) -> SourceLocation | None:
        """Return the exact authored location for an effective path."""
        node = self.node_at(path)
        return None if node is None else node.authored_at

    def missing_field_location(self, path: DiagnosticPath) -> SourceLocation | None:
        """Attribute an absent field only to its nearest replacement boundary."""
        node: OriginNode = self
        replacement_owner = node.replacement_owner
        for part in path:
            child = node.children.get(part)
            if child is None:
                return replacement_owner
            node = child
            if node.replacement_owner is not None:
                replacement_owner = node.replacement_owner
        return node.authored_at


@dataclass(frozen=True, slots=True)
class MergedDocument:
    """One effective raw document and its parallel immutable origin tree."""

    document: dict[str, Any]
    origins: OriginNode


class _Wildcard(Enum):
    ANY = auto()


type PolicyPatternPart = str | _Wildcard
type PolicyPattern = tuple[PolicyPatternPart, ...]


@dataclass(frozen=True, slots=True)
class AtomicPolicy:
    """Replace the effective field atomically with the later authored value."""


class KeyedItemMerge(StrEnum):
    """How a uniquely matched keyed-sequence item is overlaid."""

    RECURSIVE = "recursive"
    ATOMIC = "atomic"


class KeyedItemMergeFunction(Protocol):
    """Select how one uniquely matched keyed-sequence pair is overlaid."""

    def __call__(self, base: Any, override: Any) -> KeyedItemMerge: ...


@dataclass(frozen=True, slots=True)
class KeyedSequencePolicy:
    """Overlay uniquely identified items while retaining ambiguous input."""

    identity: IdentityFunction
    item_merge: KeyedItemMerge | KeyedItemMergeFunction


type FieldPolicy = AtomicPolicy | KeyedSequencePolicy


@dataclass(frozen=True, slots=True)
class PolicyRule:
    pattern: PolicyPattern
    policy: FieldPolicy

    def __post_init__(self) -> None:
        object.__setattr__(self, "pattern", tuple(self.pattern))


@dataclass(frozen=True, slots=True)
class MergePolicyRegistry:
    """Immutable path-to-policy registry for one configuration context."""

    rules: tuple[PolicyRule, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", tuple(self.rules))

    def policy_for(self, path: PolicyPath) -> FieldPolicy | None:
        for rule in self.rules:
            if _pattern_matches(rule.pattern, path):
                return rule.policy
        return None


def merge_toml_documents(
    documents: Iterable[SourceDocument],
    *,
    policies: MergePolicyRegistry,
) -> MergedDocument:
    """Merge source-labelled raw documents under one context policy registry."""
    merged: dict[str, Any] = {}
    origins = OriginNode()
    for sourced_document in documents:
        incoming = deepcopy(dict(sourced_document.document))
        incoming_origins = _origin_for_value(
            incoming,
            SourceLocation(sourced_document.source, ()),
            replacement_boundary=False,
        )
        merged, origins = _merge_mapping(
            merged,
            incoming,
            origins,
            incoming_origins,
            (),
            policies,
        )
    return MergedDocument(merged, origins)


def _merge_mapping(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
    base_origin: OriginNode,
    override_origin: OriginNode,
    path: PolicyPath,
    policies: MergePolicyRegistry,
) -> tuple[dict[str, Any], OriginNode]:
    merged = deepcopy(dict(base))
    children = dict(base_origin.children)
    for key, override_value in override.items():
        item_path = (*path, key)
        override_child = override_origin.children[key]
        if key in merged:
            base_child = children[key]
            merged[key], children[key] = _merge_value(
                merged[key],
                override_value,
                base_child,
                override_child,
                item_path,
                policies,
            )
        else:
            merged[key] = deepcopy(override_value)
            children[key] = override_child

    contributors = _ordered_contributors(
        base_origin.contributors,
        override_origin.contributors,
    )
    authored_at = (
        base_origin.authored_at
        if not override_origin.contributors
        else override_origin.authored_at
        if not base_origin.contributors
        else None
    )
    return merged, OriginNode(
        authored_at=authored_at,
        contributors=contributors,
        children=children,
    )


def _merge_value(
    base: Any,
    override: Any,
    base_origin: OriginNode,
    override_origin: OriginNode,
    path: PolicyPath,
    policies: MergePolicyRegistry,
) -> tuple[Any, OriginNode]:
    policy = policies.policy_for(path)
    if isinstance(policy, AtomicPolicy):
        return deepcopy(override), override_origin

    if isinstance(base, Mapping) and isinstance(override, Mapping):
        return _merge_mapping(
            base,
            override,
            base_origin,
            override_origin,
            path,
            policies,
        )

    if (
        isinstance(base, list)
        and isinstance(override, list)
        and isinstance(policy, KeyedSequencePolicy)
    ):
        return _merge_keyed_sequence(
            base,
            override,
            base_origin,
            override_origin,
            path,
            policies,
            policy,
        )

    return deepcopy(override), override_origin


def _merge_keyed_sequence(
    base: list[Any],
    override: list[Any],
    base_origin: OriginNode,
    override_origin: OriginNode,
    path: PolicyPath,
    policies: MergePolicyRegistry,
    policy: KeyedSequencePolicy,
) -> tuple[list[Any], OriginNode]:
    if not override:
        return [], override_origin
    if not base:
        return deepcopy(override), override_origin

    result = deepcopy(base)
    children = dict(base_origin.children)
    base_counts = Counter(_iter_keys(base, policy.identity))
    override_counts = Counter(_iter_keys(override, policy.identity))
    unique_base_indexes = _unique_key_indexes(
        result,
        policy.identity,
        base_counts,
    )

    for authored_index, item in enumerate(override):
        key = policy.identity(item)
        override_child = override_origin.children[authored_index]
        if key is not None and base_counts[key] == 1 and override_counts[key] == 1:
            effective_index = unique_base_indexes[key]
            item_merge = (
                policy.item_merge(result[effective_index], item)
                if callable(policy.item_merge)
                else policy.item_merge
            )
            if item_merge == KeyedItemMerge.ATOMIC:
                result[effective_index] = deepcopy(item)
                children[effective_index] = override_child
            else:
                result[effective_index], children[effective_index] = _merge_value(
                    result[effective_index],
                    item,
                    children[effective_index],
                    override_child,
                    (*path, effective_index),
                    policies,
                )
        else:
            effective_index = len(result)
            result.append(deepcopy(item))
            children[effective_index] = override_child

    return result, OriginNode(
        contributors=_ordered_contributors(
            base_origin.contributors,
            override_origin.contributors,
        ),
        children=children,
    )


def _origin_for_value(
    value: Any,
    location: SourceLocation,
    *,
    replacement_boundary: bool = True,
) -> OriginNode:
    children: dict[DiagnosticPathPart, OriginNode] = {}
    if isinstance(value, Mapping):
        children = {
            key: _origin_for_value(
                child,
                SourceLocation(location.source, (*location.path, key)),
            )
            for key, child in value.items()
        }
    elif isinstance(value, list):
        children = {
            index: _origin_for_value(
                child,
                SourceLocation(location.source, (*location.path, index)),
            )
            for index, child in enumerate(value)
        }
    return OriginNode(
        authored_at=location,
        replacement_owner=(
            location
            if replacement_boundary and isinstance(value, (Mapping, list))
            else None
        ),
        contributors=(location,),
        children=children,
    )


def _pattern_matches(pattern: PolicyPattern, path: PolicyPath) -> bool:
    return len(pattern) == len(path) and all(
        expected is _Wildcard.ANY or expected == actual
        for expected, actual in zip(pattern, path, strict=True)
    )


ANY_PATH_PART = _Wildcard.ANY


def _ordered_contributors(
    *groups: tuple[SourceLocation, ...],
) -> tuple[SourceLocation, ...]:
    result: list[SourceLocation] = []
    seen: set[SourceLocation] = set()
    for group in groups:
        for source in group:
            if source not in seen:
                seen.add(source)
                result.append(source)
    return tuple(result)


def _iter_keys(items: list[Any], identity: IdentityFunction) -> Iterable[MergeKey]:
    for item in items:
        key = identity(item)
        if key is not None:
            yield key


def _unique_key_indexes(
    items: list[Any],
    identity: IdentityFunction,
    counts: Counter[MergeKey],
) -> dict[MergeKey, int]:
    indexes: dict[MergeKey, int] = {}
    for index, item in enumerate(items):
        key = identity(item)
        if key is not None and counts[key] == 1:
            indexes[key] = index
    return indexes
