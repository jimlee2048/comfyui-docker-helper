"""Canonical identity helpers for admitted host-local directory trees."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, NotRequired, TypedDict

from comfyui_docker_helper.filesystem.admission import (
    LocalTreeInventory,
    LocalTreeMember,
    local_tree_mode,
)

LOCAL_TREE_DIGEST_DOMAIN = "cdh-local-tree-sha256-v1"


class _LocalTreeDigestRecord(TypedDict):
    """One private record projection used only for aggregate digest encoding."""

    path: str
    kind: Literal["directory", "file"]
    mode: Literal["0755", "0644"]
    size: NotRequired[int]
    digest: NotRequired[str]


def _canonical_local_tree_records(
    inventory: LocalTreeInventory,
) -> list[_LocalTreeDigestRecord]:
    """Return the root-first canonical records for one accepted tree."""
    records: list[_LocalTreeDigestRecord] = [
        {"path": ".", "kind": "directory", "mode": local_tree_mode("directory")}
    ]
    for item in inventory.members:
        if item.kind == "file":
            if item.size is None or item.digest is None:
                raise ValueError("regular-file tree records require size and digest")
            records.append(
                {
                    "path": item.relative_path.as_posix(),
                    "kind": item.kind,
                    "mode": local_tree_mode(item.kind),
                    "size": item.size,
                    "digest": item.digest,
                }
            )
        else:
            records.append(
                {
                    "path": item.relative_path.as_posix(),
                    "kind": item.kind,
                    "mode": local_tree_mode(item.kind),
                }
            )
    return records


def canonical_local_tree_bytes(
    inventory: LocalTreeInventory,
) -> bytes:
    """Encode the exact domain-separated aggregate input as canonical JSON."""
    payload = {
        "domain": LOCAL_TREE_DIGEST_DOMAIN,
        "records": _canonical_local_tree_records(inventory),
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
    "canonical_local_tree_bytes",
    "local_tree_digest",
    "local_tree_mode",
]
