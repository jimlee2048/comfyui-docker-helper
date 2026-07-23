"""Opt-in live contracts for exact identity providers."""

from __future__ import annotations

from typing import Literal

import httpx
import pytest
from tests.acceptance_scenarios import RELEASE_PYTHON_PROFILES

from comfyui_docker_helper.exact_ledger import (
    COMFYUI_REPOSITORY,
    CUDA_IMAGE_REPOSITORY,
    CUDA_VERSION,
    DEFAULT_CUDA_IMAGE_DISTRO,
    DEFAULT_CUDA_IMAGE_FLAVOR,
    UV_VERSION,
)
from comfyui_docker_helper.host.identity_providers import (
    DirectGitIdentityRequest,
    DockerManagedPythonIdentityProvider,
    GitDirectIdentityProvider,
    GitOfficialComfyUIIdentityProvider,
    HttpOciIdentityProvider,
    HttpRegistryNodeIdentityProvider,
    ManagedPythonIdentityRequest,
    OciIdentityRequest,
    OfficialComfyUIIdentityRequest,
    RegistryNodeIdentityRequest,
)

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.network,
]

CUSTOM_SCRIPTS_REGISTRY_ID = "comfyui-custom-scripts"
CUSTOM_SCRIPTS_URL = "https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git"


# Live providers must return exact upstream identities for every canonical source kind.
@pytest.mark.parametrize(
    "role,repository,tag",
    [
        (
            "cuda-base",
            CUDA_IMAGE_REPOSITORY,
            f"{CUDA_VERSION}-{DEFAULT_CUDA_IMAGE_FLAVOR}-{DEFAULT_CUDA_IMAGE_DISTRO}",
        ),
        ("uv-tool", "ghcr.io/astral-sh/uv", f"{UV_VERSION}-debian-slim"),
    ],
)
def test_live_oci_descriptor_and_linux_amd64_binding(
    role: Literal["cuda-base", "uv-tool"], repository: str, tag: str
) -> None:
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        identity = HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest(role, repository, tag)
        )

    assert identity.descriptor_digest.startswith("sha256:")
    assert identity.platform == "linux/amd64"


@pytest.mark.parametrize(
    "version",
    RELEASE_PYTHON_PROFILES,
)
@pytest.mark.docker
def test_live_exact_uv_managed_python_catalog(version: str) -> None:
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        uv_identity = HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest(
                "uv-tool",
                "ghcr.io/astral-sh/uv",
                f"{UV_VERSION}-debian-slim",
            )
        )
    identity = DockerManagedPythonIdentityProvider().resolve(
        ManagedPythonIdentityRequest(version, uv_identity.descriptor_digest)
    )

    assert identity.version == version
    assert identity.catalog_url.startswith("https://")
    assert identity.catalog_descriptor_digest == uv_identity.descriptor_digest


def test_live_official_comfyui_release_and_head_identities() -> None:
    provider = GitOfficialComfyUIIdentityProvider()

    releases = provider.list_releases(COMFYUI_REPOSITORY)
    head = provider.resolve(OfficialComfyUIIdentityRequest(COMFYUI_REPOSITORY, "HEAD"))

    assert releases
    assert all(identity.formal_release for identity in releases)
    assert len(head.commit) == 40
    assert head.formal_release is None


def test_live_registry_exact_node_version_identity() -> None:
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        provider = HttpRegistryNodeIdentityProvider(client)
        versions = provider.list_versions(CUSTOM_SCRIPTS_REGISTRY_ID)
        identity = provider.resolve(
            RegistryNodeIdentityRequest(
                CUSTOM_SCRIPTS_REGISTRY_ID, versions[-1].version
            )
        )

    assert identity == versions[-1]


def test_live_direct_git_ref_identity() -> None:
    identity = GitDirectIdentityProvider().resolve(
        DirectGitIdentityRequest(CUSTOM_SCRIPTS_URL, "HEAD")
    )

    assert identity.url == CUSTOM_SCRIPTS_URL
    assert len(identity.commit) == 40
