"""Shared pytest fixtures."""

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from typer.testing import CliRunner

from comfyui_docker_helper.config import (
    ComfyCliVersionCandidate,
    ComfyUIReleaseCandidate,
    RegistryInstallMetadata,
    RegistryVersionCandidate,
    SourceResolvers,
)

COMMIT_1 = "1" * 40
COMMIT_2 = "2" * 40
COMMIT_A = "a" * 40


@pytest.fixture
def cli_runner() -> CliRunner:
    """Return a CLI runner for command tests."""
    return CliRunner()


@dataclass(slots=True)
class OfflineComfyUIProvider:
    """Network-free ComfyUI provider for CLI tests."""

    release_calls: int = 0
    nightly_calls: int = 0

    def list_releases(self) -> Sequence[ComfyUIReleaseCandidate]:
        self.release_calls += 1
        return [ComfyUIReleaseCandidate(version="0.26.0", commit=COMMIT_1)]

    def get_nightly_commit(self) -> str:
        self.nightly_calls += 1
        return COMMIT_2


@dataclass(slots=True)
class OfflineComfyCliProvider:
    """Network-free comfy-cli provider for CLI tests."""

    calls: int = 0

    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        self.calls += 1
        return [ComfyCliVersionCandidate(version="1.5.0")]


@dataclass(slots=True)
class OfflineRegistryProvider:
    """Network-free registry provider for CLI tests."""

    install_calls: list[tuple[str, str | None]] = field(default_factory=list)
    version_calls: list[str] = field(default_factory=list)

    def get_install_metadata(
        self,
        node_id: str,
        version: str | None = None,
    ) -> RegistryInstallMetadata:
        self.install_calls.append((node_id, version))
        return RegistryInstallMetadata(
            node_id=node_id,
            version="1.0.0" if version is None else version,
        )

    def list_versions(self, node_id: str) -> Sequence[RegistryVersionCandidate]:
        self.version_calls.append(node_id)
        return [RegistryVersionCandidate(node_id=node_id, version="1.0.0")]


@dataclass(slots=True)
class OfflineGitProvider:
    """Network-free Git provider for CLI tests."""

    default_calls: list[str] = field(default_factory=list)
    ref_calls: list[tuple[str, str]] = field(default_factory=list)

    def resolve_default_branch_head(self, url: str) -> str:
        self.default_calls.append(url)
        return COMMIT_A

    def resolve_ref(self, url: str, ref: str) -> str:
        self.ref_calls.append((url, ref))
        return COMMIT_A


@pytest.fixture(autouse=True)
def offline_cli_source_resolvers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI tests offline unless they explicitly patch resolver creation."""

    def create_resolvers() -> SourceResolvers:
        git = OfflineGitProvider()
        return SourceResolvers(
            comfyui=OfflineComfyUIProvider(),
            comfy_cli=OfflineComfyCliProvider(),
            registry=OfflineRegistryProvider(),
            git=git,
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.create_default_source_resolvers",
        create_resolvers,
    )
