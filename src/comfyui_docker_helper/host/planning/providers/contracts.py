"""Provider request, identity, and protocol contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Protocol

from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.planning.inputs.executable import (
    LocalExecutableIdentityRequest,
)
from comfyui_docker_helper.config.planning.request import SelectorStability

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ProviderFailureKind(StrEnum):
    """Stable failure classes shared by every identity provider."""

    NOT_FOUND = "not-found"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate-limit"
    NETWORK = "network"
    INVALID_RESPONSE = "invalid-response"
    INVALID_REQUEST = "invalid-request"
    LOCAL_INPUT = "local-input"


_FAILURE_MESSAGES: Mapping[ProviderFailureKind, str] = {
    ProviderFailureKind.NOT_FOUND: "requested identity was not found",
    ProviderFailureKind.AUTHENTICATION: "identity provider authentication failed",
    ProviderFailureKind.RATE_LIMIT: "identity provider rate limit was reached",
    ProviderFailureKind.NETWORK: "identity provider request failed",
    ProviderFailureKind.INVALID_RESPONSE: "identity provider returned invalid data",
    ProviderFailureKind.INVALID_REQUEST: "identity provider request is invalid",
    ProviderFailureKind.LOCAL_INPUT: "local executable input is invalid",
}


class IdentityProviderError(Exception):
    """A short stable provider error that never contains request credentials."""

    def __init__(
        self,
        source: str,
        kind: ProviderFailureKind,
        *,
        controlled_detail: str | None = None,
    ) -> None:
        self.source = source
        self.kind = kind
        message = f"{source}: {_FAILURE_MESSAGES[kind]}"
        if controlled_detail is not None:
            message = f"{message}: {controlled_detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class OciIdentityRequest:
    role: Literal["cuda-base", "uv-tool"]
    repository: str
    tag: str
    platform: Literal["linux/amd64"] = "linux/amd64"
    stability: SelectorStability = SelectorStability.MOVING


@dataclass(frozen=True, slots=True)
class OciIdentity:
    role: Literal["cuda-base", "uv-tool"]
    repository: str
    tag: str
    descriptor_digest: str
    descriptor_kind: Literal["index", "manifest"]
    platform: Literal["linux/amd64"]
    resolved_version: str | None = None


class OciIdentityProvider(Protocol):
    def resolve(self, request: OciIdentityRequest) -> OciIdentity: ...


@dataclass(frozen=True, slots=True)
class ManagedPythonIdentityRequest:
    version: str
    catalog_descriptor_digest: str
    implementation: Literal["cpython"] = "cpython"
    platform: Literal["linux/amd64"] = "linux/amd64"
    libc: Literal["gnu"] = "gnu"
    stability: SelectorStability = SelectorStability.EXACT


@dataclass(frozen=True, slots=True)
class ManagedPythonIdentity:
    version: str
    implementation: Literal["cpython"]
    platform: Literal["linux/amd64"]
    libc: Literal["gnu"]
    provider: Literal["uv-managed"]
    catalog_descriptor_digest: str
    catalog_key: str
    catalog_url: str


class ManagedPythonIdentityProvider(Protocol):
    def resolve(
        self, request: ManagedPythonIdentityRequest
    ) -> ManagedPythonIdentity: ...


@dataclass(frozen=True, slots=True)
class OfficialComfyUIIdentityRequest:
    repository: str
    ref: str

    @property
    def stability(self) -> SelectorStability:
        return (
            SelectorStability.EXACT
            if _COMMIT_PATTERN.fullmatch(self.ref) or _formal_release_from_ref(self.ref)
            else SelectorStability.MOVING
        )


@dataclass(frozen=True, slots=True)
class OfficialComfyUIIdentity:
    repository: str
    commit: str
    formal_release: str | None


class OfficialComfyUIIdentityProvider(Protocol):
    def list_releases(self, repository: str) -> tuple[OfficialComfyUIIdentity, ...]: ...

    def resolve(
        self, request: OfficialComfyUIIdentityRequest
    ) -> OfficialComfyUIIdentity: ...

    def is_ancestor(self, repository: str, ancestor: str, descendant: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class RegistryNodeIdentityRequest:
    node_id: str
    version: str
    stability: SelectorStability = SelectorStability.EXACT


@dataclass(frozen=True, slots=True)
class RegistryNodeIdentity:
    type: Literal["registry"]
    node_id: str
    version: str


class RegistryNodeIdentityProvider(Protocol):
    def list_versions(self, node_id: str) -> tuple[RegistryNodeIdentity, ...]: ...

    def resolve(self, request: RegistryNodeIdentityRequest) -> RegistryNodeIdentity: ...


@dataclass(frozen=True, slots=True)
class DirectGitIdentityRequest:
    url: str
    ref: str

    @property
    def stability(self) -> SelectorStability:
        return (
            SelectorStability.EXACT
            if _COMMIT_PATTERN.fullmatch(self.ref)
            else SelectorStability.MOVING
        )


@dataclass(frozen=True, slots=True)
class DirectGitIdentity:
    type: Literal["git"]
    url: str
    commit: str


class DirectGitIdentityProvider(Protocol):
    def resolve(self, request: DirectGitIdentityRequest) -> DirectGitIdentity: ...


@dataclass(frozen=True, slots=True)
class LocalExecutableIdentity:
    relative_path: PurePosixPath
    digest: str


class LocalExecutableIdentityProvider(Protocol):
    def resolve(
        self, request: LocalExecutableIdentityRequest
    ) -> LocalExecutableIdentity: ...


def _formal_release_from_ref(ref: str) -> str | None:
    candidate = ref.removeprefix("refs/tags/").removeprefix("v")
    if candidate == ref:
        return None
    try:
        version = Version(candidate)
    except InvalidVersion:
        return None
    if version.is_prerelease or version.is_devrelease or version.local is not None:
        return None
    return str(version)
