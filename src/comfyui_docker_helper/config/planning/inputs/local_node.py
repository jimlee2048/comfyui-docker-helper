"""Independent admitted planning facts for local custom nodes."""

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from comfyui_docker_helper.config.planning.local_tree import (
    LocalTreeInventory,
    local_tree_digest,
)
from comfyui_docker_helper.config.validation.selectors import (
    validate_local_node_target_dir,
)


def local_node_context_path(target_dir: str) -> PurePosixPath:
    validate_local_node_target_dir(target_dir)
    return (
        PurePosixPath("build/local-nodes")
        / hashlib.sha256(target_dir.encode("utf-8")).hexdigest()
    )


@dataclass(frozen=True, slots=True)
class LocalNodePlanningInput:
    target_dir: str
    context_path: PurePosixPath
    content_lock: bool
    inventory: LocalTreeInventory
    tree_digest: str | None
    kind: Literal["tree"] = "tree"

    def __post_init__(self) -> None:
        validate_local_node_target_dir(self.target_dir)
        if self.context_path != local_node_context_path(self.target_dir):
            raise ValueError("local node context path is not canonical")
        if self.kind != "tree" or type(self.content_lock) is not bool:
            raise ValueError(
                "local node planning input requires tree kind and boolean lock mode"
            )
        if self.content_lock:
            if self.tree_digest != local_tree_digest(self.inventory):
                raise ValueError("local node digest does not match inventory")
        elif self.tree_digest is not None or any(
            item.size is not None or item.digest is not None
            for item in self.inventory.members
        ):
            raise ValueError("unlocked local node must omit content identities")


def index_local_node_inputs(
    inputs: tuple[LocalNodePlanningInput, ...],
) -> dict[str, LocalNodePlanningInput]:
    indexed: dict[str, LocalNodePlanningInput] = {}
    for item in inputs:
        if not isinstance(item, LocalNodePlanningInput):
            raise ValueError("local node inputs require local node planning facts")
        if item.target_dir in indexed:
            raise ValueError("duplicate local node planning input")
        indexed[item.target_dir] = item
    return indexed
