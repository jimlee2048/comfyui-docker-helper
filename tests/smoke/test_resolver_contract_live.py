"""Live smoke checks for resolver provider contracts."""

from __future__ import annotations

import os
import re

import httpx
import pytest

from comfyui_docker_helper.config import resolve_git_custom_node
from comfyui_docker_helper.host.source_providers import (
    GitRemoteProvider,
    HttpRegistryProvider,
    PyPIComfyCliProvider,
)

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("CDH_RUN_NETWORK_SMOKE") != "1",
        reason="set CDH_RUN_NETWORK_SMOKE=1 to run live resolver smoke",
    ),
]

COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
CUSTOM_SCRIPTS_LIVE_REF = "HEAD"
CUSTOM_SCRIPTS_REGISTRY_ID = "comfyui-custom-scripts"
CUSTOM_SCRIPTS_URL = "https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git"


def assert_commit(value: str) -> None:
    """Assert a concrete full Git commit SHA."""
    assert COMMIT_RE.fullmatch(value) is not None


def test_live_comfyui_git_release_and_nightly_contracts() -> None:
    """ComfyUI Git refs expose release tags and the current HEAD commit."""
    provider = GitRemoteProvider()

    releases = provider.list_releases()
    assert releases
    assert any(
        candidate.version and COMMIT_RE.fullmatch(candidate.commit)
        for candidate in releases
    )

    assert_commit(provider.get_nightly_commit())


def test_live_comfy_cli_pypi_version_contract() -> None:
    """PyPI exposes at least one comfy-cli version candidate."""
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        versions = PyPIComfyCliProvider(client).list_versions()

    assert versions
    assert any(candidate.version for candidate in versions)


def test_live_comfy_registry_install_contract() -> None:
    """Comfy Registry latest install metadata exposes node ID and version."""
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        provider = HttpRegistryProvider(client)
        metadata = provider.get_install_metadata(CUSTOM_SCRIPTS_REGISTRY_ID)

    assert metadata.node_id == CUSTOM_SCRIPTS_REGISTRY_ID
    assert metadata.version


def test_live_comfy_registry_versions_contract() -> None:
    """Comfy Registry version listing exposes candidates for constrained selectors."""
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        provider = HttpRegistryProvider(client)
        versions = provider.list_versions(CUSTOM_SCRIPTS_REGISTRY_ID)

    assert versions
    assert any(
        candidate.node_id == CUSTOM_SCRIPTS_REGISTRY_ID and candidate.version
        for candidate in versions
    )


def test_live_git_custom_node_ref_contract() -> None:
    """Git provider resolves a public custom-node ref through the remote."""
    resolved = resolve_git_custom_node(
        CUSTOM_SCRIPTS_URL,
        CUSTOM_SCRIPTS_LIVE_REF,
        GitRemoteProvider(),
    )

    assert resolved.url == CUSTOM_SCRIPTS_URL
    assert_commit(resolved.commit)
