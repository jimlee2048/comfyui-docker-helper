"""Lockfile models, TOML I/O, and lock input digest helpers."""

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Annotated, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, model_validator

from comfyui_docker_helper.config.models import (
    Config,
    GitCustomNodeConfig,
    RegistryCustomNodeConfig,
)
from comfyui_docker_helper.config.validation import (
    normalize_comfy_cli_version,
    normalize_comfyui_version,
    normalize_registry_version,
)

LOCKFILE_SCHEMA_VERSION = 1
_REGISTRY_LATEST_SELECTOR = "latest"


class LockDomainError(ValueError):
    """Raised when config cannot map cleanly into the minimal lock domain."""


class LockConfigModel(BaseModel):
    """Apply strict structural validation to every lockfile block."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class LockManifest(LockConfigModel):
    """Lockfile manifest metadata."""

    lock_input_digest: str


class LockedComfyUI(LockConfigModel):
    """Resolved ComfyUI and comfy-cli source selections."""

    repo: str
    commit: str
    version: str | None = None
    cli_version: str


class RegistryLockedCustomNode(LockConfigModel):
    """Resolved registry custom-node source selection."""

    type: Literal["registry"]
    id: str
    version: str


class GitLockedCustomNode(LockConfigModel):
    """Resolved Git custom-node source selection."""

    type: Literal["git"]
    url: str
    commit: str


LockedCustomNode = Annotated[
    RegistryLockedCustomNode | GitLockedCustomNode,
    Field(discriminator="type"),
]


class Lockfile(LockConfigModel):
    """Root ``config.lock.toml`` contract."""

    schema_version: Literal[1]
    manifest: LockManifest
    comfyui: LockedComfyUI
    custom_nodes: list[LockedCustomNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_custom_node_keys(self) -> "Lockfile":
        _raise_duplicate_lock_entries(self.custom_nodes)
        return self


def load_lockfile(path: str | Path) -> Lockfile:
    """Load and structurally validate one ``config.lock.toml`` file."""
    with Path(path).open("rb") as lock_file:
        return parse_lockfile_toml(lock_file.read())


def parse_lockfile_toml(document: str | bytes) -> Lockfile:
    """Parse and structurally validate ``config.lock.toml`` content."""
    if isinstance(document, bytes):
        document = document.decode("utf-8")
    return Lockfile.model_validate(tomllib.loads(document))


def dump_lockfile_toml(lockfile: Lockfile) -> str:
    """Serialize ``config.lock.toml`` with deterministic field and entry order."""
    return tomli_w.dumps(_lockfile_to_toml_data(lockfile))


def write_lockfile(path: str | Path, lockfile: Lockfile) -> None:
    """Write ``config.lock.toml`` using deterministic TOML serialization."""
    Path(path).write_text(dump_lockfile_toml(lockfile), encoding="utf-8")


def compute_lock_input_digest(config: Config) -> str:
    """Return the digest for the lock-relevant effective config subset only."""
    payload = _lock_input_payload(config)
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def validate_lock_domain_unique(config: Config) -> None:
    """Require unique config keys needed to map minimal lock entries to inputs."""
    registry_ids: set[str] = set()
    git_urls: set[str] = set()

    for index, node in enumerate(config.comfyui.custom_nodes):
        if isinstance(node, RegistryCustomNodeConfig):
            if node.id in registry_ids:
                raise LockDomainError(
                    "duplicate registry custom-node id at "
                    f"comfyui.custom_nodes[{index}].id: {node.id}"
                )
            registry_ids.add(node.id)
        elif isinstance(node, GitCustomNodeConfig):
            if node.url in git_urls:
                raise LockDomainError(
                    "duplicate git custom-node url at "
                    f"comfyui.custom_nodes[{index}].url: {node.url}"
                )
            git_urls.add(node.url)


def _lock_input_payload(config: Config) -> dict[str, object]:
    validate_lock_domain_unique(config)

    registry_nodes = []
    git_nodes = []
    for node in config.comfyui.custom_nodes:
        if isinstance(node, RegistryCustomNodeConfig):
            registry_nodes.append(
                {
                    "id": node.id,
                    "version": _normalize_registry_lock_selector(node.version),
                }
            )
        elif isinstance(node, GitCustomNodeConfig):
            git_nodes.append({"url": node.url, "ref": node.ref})

    return {
        "comfyui": {
            "version": normalize_comfyui_version(config.comfyui.version),
            "cli_version": normalize_comfy_cli_version(config.comfyui.cli_version),
        },
        "custom_nodes": {
            "registry": sorted(registry_nodes, key=lambda item: item["id"]),
            "git": sorted(git_nodes, key=lambda item: item["url"]),
        },
    }


def _normalize_registry_lock_selector(version: str | None) -> str:
    if version is None:
        return _REGISTRY_LATEST_SELECTOR
    return normalize_registry_version(version)


def _lockfile_to_toml_data(lockfile: Lockfile) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": lockfile.schema_version,
        "manifest": lockfile.manifest.model_dump(mode="json"),
        "comfyui": lockfile.comfyui.model_dump(mode="json", exclude_none=True),
    }
    if lockfile.custom_nodes:
        data["custom_nodes"] = [
            node.model_dump(mode="json") for node in lockfile.custom_nodes
        ]
    return data


def _raise_duplicate_lock_entries(custom_nodes: list[LockedCustomNode]) -> None:
    registry_ids: set[str] = set()
    git_urls: set[str] = set()

    for index, node in enumerate(custom_nodes):
        if isinstance(node, RegistryLockedCustomNode):
            if node.id in registry_ids:
                raise ValueError(
                    "duplicate registry custom-node id at "
                    f"custom_nodes[{index}].id: {node.id}"
                )
            registry_ids.add(node.id)
        elif isinstance(node, GitLockedCustomNode):
            if node.url in git_urls:
                raise ValueError(
                    "duplicate git custom-node url at "
                    f"custom_nodes[{index}].url: {node.url}"
                )
            git_urls.add(node.url)
