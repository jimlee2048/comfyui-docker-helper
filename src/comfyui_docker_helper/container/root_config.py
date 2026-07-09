"""Container-side loading for root config and lock artifacts."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from comfyui_docker_helper.config import (
    Config,
    Diagnostic,
    GitCustomNodeConfig,
    GitLockedCustomNode,
    Lockfile,
    LockOptions,
    LockServiceError,
    RegistryLockedCustomNode,
    SourceResolvers,
    parse_lockfile_toml,
    resolve_lockfile,
)
from comfyui_docker_helper.errors import ApplicationError


class ContainerRootConfigError(ApplicationError):
    """A user-facing root config/lock loading failure."""


@dataclass(frozen=True, slots=True)
class ContainerRootArtifacts:
    """Validated root artifacts consumed by container helpers."""

    config: Config
    lockfile: Lockfile


def load_container_root_artifacts(
    config_path: str | Path,
    lock_path: str | Path,
) -> ContainerRootArtifacts:
    """Load and validate root config and lock artifacts for container helpers."""
    config = _load_config(config_path)
    lockfile = _load_lockfile(lock_path)
    _validate_lock_compatible(config, lockfile)
    return ContainerRootArtifacts(config=config, lockfile=lockfile)


def custom_nodes_document(config: Config, lockfile: Lockfile) -> dict[str, object]:
    """Extract the locked custom-node helper view from root artifacts."""
    registry_locks = {
        node.id: node
        for node in lockfile.custom_nodes
        if isinstance(node, RegistryLockedCustomNode)
    }
    git_locks = {
        node.url: node
        for node in lockfile.custom_nodes
        if isinstance(node, GitLockedCustomNode)
    }
    nodes: list[dict[str, object]] = []
    for node in config.comfyui.custom_nodes:
        item = node.model_dump(mode="json", exclude_none=True)
        if isinstance(node, GitCustomNodeConfig):
            locked = git_locks[node.url]
            item["url"] = locked.url
            item["ref"] = locked.commit
        else:
            locked = registry_locks[node.id]
            item["version"] = locked.version
        nodes.append(item)

    return {"comfyui": {"custom_nodes": nodes}}


def files_document(config: Config) -> dict[str, object]:
    """Extract the file-download helper view from the full root config."""
    downloader = config.cdh.downloader
    default_downloader = config.cdh.default_downloader
    return {
        "cdh": {
            "download_max_attempts": config.cdh.download_max_attempts,
            "download_failure_policy": config.cdh.download_failure_policy,
        },
        "downloader": {
            "default": default_downloader,
            "aria2": downloader.aria2.model_dump(mode="json"),
            "httpx": downloader.httpx.model_dump(mode="json"),
        },
        "files": [
            {
                "url": file.url,
                "dir": file.dir,
                "filename": file.filename,
                "overwrite": file.overwrite,
                "downloader": file.downloader or default_downloader,
            }
            for file in config.files
        ],
    }


def _load_config(config_path: str | Path) -> Config:
    path = Path(config_path)
    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ContainerRootConfigError(f"root config does not exist: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ContainerRootConfigError(
            f"root config is not valid TOML: {path}: {error}"
        ) from error
    except OSError as error:
        raise ContainerRootConfigError(
            f"root config cannot be read: {path}: {error}"
        ) from error

    try:
        return Config.model_validate(document)
    except ValidationError as error:
        raise ContainerRootConfigError(
            f"root config validation failed: {error}"
        ) from error


def _load_lockfile(lock_path: str | Path) -> Lockfile:
    path = Path(lock_path)
    try:
        return parse_lockfile_toml(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ContainerRootConfigError(f"root lock does not exist: {path}") from error
    except UnicodeDecodeError as error:
        raise ContainerRootConfigError(
            f"root lock is not valid UTF-8: {path}: {error}"
        ) from error
    except ValidationError as error:
        raise ContainerRootConfigError(
            f"root lock validation failed: {path}: {error}"
        ) from error
    except (OSError, ValueError) as error:
        raise ContainerRootConfigError(
            f"root lock cannot be read: {path}: {error}"
        ) from error


def _validate_lock_compatible(config: Config, lockfile: Lockfile) -> None:
    try:
        resolve_lockfile(
            config,
            lockfile,
            SourceResolvers(
                comfyui=_UnusedResolver(),
                comfy_cli=_UnusedResolver(),
                registry=_UnusedResolver(),
                git=_UnusedResolver(),
            ),
            LockOptions(locked=True),
        )
    except LockServiceError as error:
        details = "; ".join(
            _format_diagnostic(diagnostic) for diagnostic in error.diagnostics
        )
        raise ContainerRootConfigError(
            f"root lock is incompatible with root config: {details}"
        ) from error
    except ValueError as error:
        raise ContainerRootConfigError(
            f"root lock is incompatible with root config: {error}"
        ) from error


def _format_diagnostic(diagnostic: Diagnostic) -> str:
    path = ".".join(str(part) for part in diagnostic.path) or "config"
    return f"{path}: {diagnostic.message} ({diagnostic.code})"


class _UnusedResolver:
    """Resolver protocol placeholder that must never be called in locked mode."""

    def list_releases(self) -> Any:
        raise AssertionError("locked container validation must not resolve sources")

    def get_nightly_commit(self) -> str:
        raise AssertionError("locked container validation must not resolve sources")

    def list_versions(self) -> Any:
        raise AssertionError("locked container validation must not resolve sources")

    def get_install_metadata(self, node_id: str, version: str | None = None) -> Any:
        del node_id, version
        raise AssertionError("locked container validation must not resolve sources")

    def resolve_default_branch_head(self, url: str) -> str:
        del url
        raise AssertionError("locked container validation must not resolve sources")

    def resolve_ref(self, url: str, ref: str) -> str:
        del url, ref
        raise AssertionError("locked container validation must not resolve sources")
