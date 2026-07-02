"""Tests for container-side root config and lock loading."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from tests.artifact_helpers import COMMIT_1, write_root_artifacts

from comfyui_docker_helper.config import (
    COMFYUI_REPO_URL,
    Config,
    LockedComfyUI,
    Lockfile,
    LockManifest,
    dump_lockfile_toml,
)
from comfyui_docker_helper.container.root_config import (
    ContainerRootConfigError,
    load_container_root_artifacts,
)


def test_load_container_root_artifacts_accepts_compatible_config_and_lock(
    tmp_path: Path,
) -> None:
    """Load valid root artifacts as the shared container helper boundary."""
    config, lock = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "latest"
""",
    )

    artifacts = load_container_root_artifacts(config, lock)

    assert artifacts.config.comfyui.version == "latest"
    assert artifacts.lockfile.comfyui.commit == COMMIT_1


def test_load_container_root_artifacts_rejects_missing_files(tmp_path: Path) -> None:
    """Both root artifacts are required."""
    with pytest.raises(ContainerRootConfigError, match="root config does not exist"):
        load_container_root_artifacts(
            tmp_path / "missing-config.toml",
            tmp_path / "missing-lock.toml",
        )

    config, _ = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "latest"
""",
    )
    with pytest.raises(ContainerRootConfigError, match="root lock does not exist"):
        load_container_root_artifacts(config, tmp_path / "missing-lock.toml")


def test_load_container_root_artifacts_rejects_invalid_toml(tmp_path: Path) -> None:
    """Invalid root artifact syntax fails before helper plan extraction."""
    config = tmp_path / "config.toml"
    lock = tmp_path / "config.lock.toml"
    config.write_text("[comfyui\n", encoding="utf-8")
    lock.write_text("schema_version = 1\n", encoding="utf-8")

    with pytest.raises(ContainerRootConfigError, match="root config is not valid TOML"):
        load_container_root_artifacts(config, lock)

    config, lock = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "latest"
""",
    )
    lock.write_text("[manifest\n", encoding="utf-8")

    with pytest.raises(ContainerRootConfigError, match="root lock cannot be read"):
        load_container_root_artifacts(config, lock)


def test_load_container_root_artifacts_rejects_digest_mismatch(
    tmp_path: Path,
) -> None:
    """Digest mismatches make the lock incompatible for container helpers."""
    config, lock = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "latest"
""",
    )
    lockfile = Lockfile(
        schema_version=1,
        manifest=LockManifest(lock_input_digest="sha256:" + "0" * 64),
        comfyui=LockedComfyUI(
            repo=COMFYUI_REPO_URL,
            version="0.26.0",
            commit=COMMIT_1,
            cli_version="1.5.0",
        ),
        custom_nodes=[],
    )
    lock.write_text(dump_lockfile_toml(lockfile), encoding="utf-8")

    with pytest.raises(ContainerRootConfigError, match=r"lockfile\.digest_mismatch"):
        load_container_root_artifacts(config, lock)


@pytest.mark.parametrize(
    "tampered_config",
    [
        """
[comfyui]
version = "not-a-version"
""",
        """
[comfyui]
version = "latest"

[[comfyui.custom_nodes]]
type = "registry"
id = "node"
version = "not-a-version"
""",
    ],
)
def test_load_container_root_artifacts_wraps_structural_selector_failures(
    tmp_path: Path,
    tampered_config: str,
) -> None:
    """Structurally valid but invalid selectors fail at the container boundary."""
    config, lock = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "latest"

[[comfyui.custom_nodes]]
type = "registry"
id = "node"
version = "1.0.0"
""",
    )
    config.write_text(
        """
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"
"""
        + tampered_config.lstrip(),
        encoding="utf-8",
    )

    assert Config.model_validate(tomllib.loads(config.read_text(encoding="utf-8")))
    with pytest.raises(
        ContainerRootConfigError,
        match="root lock is incompatible with root config",
    ) as exc_info:
        load_container_root_artifacts(config, lock)

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert not isinstance(exc_info.value.__cause__, ContainerRootConfigError)


def test_load_container_root_artifacts_rejects_incompatible_lock(
    tmp_path: Path,
) -> None:
    """A lock that does not satisfy requested selectors fails closed."""
    config, lock = write_root_artifacts(
        tmp_path,
        """
[comfyui]
version = "nightly"
""",
    )

    with pytest.raises(
        ContainerRootConfigError,
        match=r"lockfile\.comfyui_incompatible",
    ):
        load_container_root_artifacts(config, lock)
