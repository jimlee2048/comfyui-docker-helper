"""Typed custom-node evidence recorded in the final image manifest."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from comfyui_docker_helper.config.build_plan import (
    CustomNodePlan,
    RegistryNodePlan,
)
from comfyui_docker_helper.config.canonical_lock import (
    validate_exact_registry_version,
    validate_git_commit,
    validate_git_url,
    validate_registry_id,
)
from comfyui_docker_helper.config.selector_validation import resolve_git_target_dir


class _InventoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RegistryInventoryEntry(_InventoryModel):
    type: Literal["registry"]
    id: str
    version: str
    verification: Literal["registry-version"]
    control: Literal["direct-cm-cli"]

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_registry_id(value)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return validate_exact_registry_version(value)


class GitInventoryEntry(_InventoryModel):
    type: Literal["git"]
    url: str
    commit: str
    target: str
    verification: Literal["git-commit"]
    control: Literal["direct-git"]

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return validate_git_url(value)

    @field_validator("commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        return validate_git_commit(value)

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return resolve_git_target_dir("https://example.invalid/repository.git", value)


CustomNodeInventoryEntry = Annotated[
    RegistryInventoryEntry | GitInventoryEntry,
    Field(discriminator="type"),
]


class CustomNodeInventory(_InventoryModel):
    schema_version: Literal[1]
    nodes: tuple[CustomNodeInventoryEntry, ...]


def custom_node_inventory(nodes: Sequence[CustomNodePlan]) -> CustomNodeInventory:
    """Project verified declarations into final typed evidence."""
    entries: list[RegistryInventoryEntry | GitInventoryEntry] = []
    for node in nodes:
        if isinstance(node, RegistryNodePlan):
            entries.append(
                RegistryInventoryEntry(
                    type="registry",
                    id=node.id,
                    version=node.version,
                    verification="registry-version",
                    control="direct-cm-cli",
                )
            )
        else:
            entries.append(
                GitInventoryEntry(
                    type="git",
                    url=node.url,
                    commit=node.commit,
                    target=node.target.rsplit("/", 1)[-1],
                    verification="git-commit",
                    control="direct-git",
                )
            )
    return CustomNodeInventory(schema_version=1, nodes=tuple(entries))
