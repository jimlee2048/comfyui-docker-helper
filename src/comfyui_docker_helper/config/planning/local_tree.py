"""Canonical identity helpers for admitted host-local directory trees."""

from __future__ import annotations

import hashlib
import json

from comfyui_docker_helper.filesystem.admission import (
    LocalTreeInventory,
    LocalTreeMember,
    LocalTreeRecord,
)

LOCAL_TREE_DIGEST_DOMAIN = "cdh-local-tree-sha256-v1"


def canonical_local_tree_records(
    inventory: LocalTreeInventory,
) -> tuple[LocalTreeRecord, ...]:
    """Return the root-first canonical records for one accepted tree."""
    return inventory.records()


def canonical_local_tree_bytes(
    inventory: LocalTreeInventory,
) -> bytes:
    """Encode the exact domain-separated aggregate input as canonical JSON."""
    records = canonical_local_tree_records(inventory)
    payload = {
        "domain": LOCAL_TREE_DIGEST_DOMAIN,
        "records": [record.as_dict() for record in records],
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def local_tree_digest(
    inventory: LocalTreeInventory,
) -> str:
    """Compute the aggregate SHA-256 identity of one locked tree inventory."""
    return f"sha256:{hashlib.sha256(canonical_local_tree_bytes(inventory)).hexdigest()}"


__all__ = [
    "LOCAL_TREE_DIGEST_DOMAIN",
    "LocalTreeInventory",
    "LocalTreeMember",
    "LocalTreeRecord",
    "canonical_local_tree_bytes",
    "canonical_local_tree_records",
    "local_tree_digest",
]
