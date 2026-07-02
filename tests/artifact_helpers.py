"""Test helpers for rendered root config and lock artifacts."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from comfyui_docker_helper.config import (
    COMFYUI_REPO_URL,
    Config,
    GitLockedCustomNode,
    LockedComfyUI,
    Lockfile,
    LockManifest,
    RegistryLockedCustomNode,
    compute_lock_input_digest,
    dump_lockfile_toml,
)
from comfyui_docker_helper.config.models import GitCustomNodeConfig

COMMIT_1 = "1" * 40
COMMIT_A = "a" * 40
_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}\Z")

_MINIMAL_ROOT_PREFIX = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"
"""


def write_root_artifacts(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """Write root config.toml and a compatible config.lock.toml."""
    config = tmp_path / "config.toml"
    config.write_text(_MINIMAL_ROOT_PREFIX + body.lstrip(), encoding="utf-8")
    lock = tmp_path / "config.lock.toml"
    parsed_config = Config.model_validate(tomllib.loads(config.read_text()))
    lock.write_text(
        dump_lockfile_toml(make_lockfile(parsed_config)),
        encoding="utf-8",
    )
    return config, lock


def make_lockfile(config: Config) -> Lockfile:
    """Return a compatible lockfile for a test config."""
    custom_nodes: list[RegistryLockedCustomNode | GitLockedCustomNode] = []
    for node in config.comfyui.custom_nodes:
        if isinstance(node, GitCustomNodeConfig):
            custom_nodes.append(
                GitLockedCustomNode(
                    type="git",
                    url=node.url,
                    commit=node.ref.lower()
                    if node.ref is not None and _COMMIT_PATTERN.fullmatch(node.ref)
                    else COMMIT_A,
                )
            )
        else:
            custom_nodes.append(
                RegistryLockedCustomNode(
                    type="registry",
                    id=node.id,
                    version="1.0.0" if node.version is None else node.version,
                )
            )
    return Lockfile(
        schema_version=1,
        manifest=LockManifest(lock_input_digest=compute_lock_input_digest(config)),
        comfyui=LockedComfyUI(
            repo=COMFYUI_REPO_URL,
            version="0.26.0",
            commit=COMMIT_1,
            cli_version="1.5.0",
        ),
        custom_nodes=custom_nodes,
    )
