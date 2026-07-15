"""Exact-identity providers for canonical config-lock v1.

Providers return only the identity fields required by canonical config-lock v1
and stable, credential-free failure categories.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from urllib.parse import quote, urlencode, urlparse

import httpx
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.canonical_request import SelectorStability
from comfyui_docker_helper.config.selector_validation import normalize_registry_version
from comfyui_docker_helper.config.value_validation import is_argv_value
from comfyui_docker_helper.exact_ledger import COMFYUI_REPOSITORY
from comfyui_docker_helper.host.uv_runner import HostUvRunner

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_BEARER_PARAMETER = re.compile(r'(\w+)="([^"]*)"')
_OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
_OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
_OCI_ACCEPT = ", ".join(
    (*sorted(_OCI_INDEX_MEDIA_TYPES), *sorted(_OCI_MANIFEST_MEDIA_TYPES))
)
_GIT_TIMEOUT_SECONDS = 30.0


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

    def __init__(self, source: str, kind: ProviderFailureKind) -> None:
        self.source = source
        self.kind = kind
        super().__init__(f"{source}: {_FAILURE_MESSAGES[kind]}")


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
class LocalExecutableIdentityRequest:
    root: Path
    relative_path: PurePosixPath
    identity_path: PurePosixPath | None = None
    stability: SelectorStability = SelectorStability.MOVING

    @property
    def canonical_path(self) -> PurePosixPath:
        return self.identity_path or self.relative_path


@dataclass(frozen=True, slots=True)
class LocalExecutableIdentity:
    relative_path: PurePosixPath
    digest: str


class LocalExecutableIdentityProvider(Protocol):
    def resolve(
        self, request: LocalExecutableIdentityRequest
    ) -> LocalExecutableIdentity: ...


@dataclass(frozen=True, slots=True)
class HttpOciIdentityProvider:
    """Resolve an OCI tag to its immutable top-level registry descriptor."""

    client: httpx.Client

    def resolve(self, request: OciIdentityRequest) -> OciIdentity:
        if request.role not in {"cuda-base", "uv-tool"} or (
            not is_argv_value(request.tag) or request.tag.startswith("-")
        ):
            raise IdentityProviderError(
                "OCI registry", ProviderFailureKind.INVALID_REQUEST
            )
        registry, repository = _split_oci_repository(request.repository)
        source = "OCI registry"
        tag = quote(request.tag, safe="")
        url = f"https://{registry}/v2/{repository}/manifests/{tag}"
        headers = {"Accept": _OCI_ACCEPT}
        response = _oci_get(self.client, url, headers=headers, source=source)
        if response.status_code == 401:
            authorization = _oci_bearer_authorization(
                self.client,
                response.headers.get("WWW-Authenticate", ""),
                repository,
            )
            response = _oci_get(
                self.client,
                url,
                headers={**headers, "Authorization": authorization},
                source=source,
            )
        _raise_for_http_status(response, source)
        document = _response_json(response, source)
        media_type = _required_string(document, "mediaType", source)
        digest = response.headers.get("Docker-Content-Digest", "")
        if not _content_matches_digest(response.content, digest):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)

        authorization = response.request.headers.get("Authorization", "")
        if media_type in _OCI_INDEX_MEDIA_TYPES:
            child_digest, child_media_type = _select_index_platform(
                document, request.platform, source
            )
            child_url = f"https://{registry}/v2/{repository}/manifests/{child_digest}"
            child_response = _oci_get(
                self.client,
                child_url,
                headers={"Accept": _OCI_ACCEPT, "Authorization": authorization},
                source=source,
            )
            _raise_for_http_status(child_response, source)
            if child_response.headers.get(
                "Docker-Content-Digest"
            ) != child_digest or not _content_matches_digest(
                child_response.content, child_digest
            ):
                raise IdentityProviderError(
                    source, ProviderFailureKind.INVALID_RESPONSE
                )
            child_document = _response_json(child_response, source)
            actual_child_media_type = _required_string(
                child_document, "mediaType", source
            )
            if (
                child_media_type not in _OCI_MANIFEST_MEDIA_TYPES
                or actual_child_media_type != child_media_type
            ):
                raise IdentityProviderError(
                    source, ProviderFailureKind.INVALID_RESPONSE
                )
            config_document = _verify_manifest_config_platform(
                self.client,
                child_document,
                registry=registry,
                repository=repository,
                authorization=authorization,
                platform=request.platform,
                platform_mismatch_kind=ProviderFailureKind.INVALID_RESPONSE,
                source=source,
            )
            kind: Literal["index", "manifest"] = "index"
        elif media_type in _OCI_MANIFEST_MEDIA_TYPES:
            config_document = _verify_manifest_config_platform(
                self.client,
                document,
                registry=registry,
                repository=repository,
                authorization=authorization,
                platform=request.platform,
                platform_mismatch_kind=ProviderFailureKind.NOT_FOUND,
                source=source,
            )
            kind = "manifest"
        else:
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
        return OciIdentity(
            role=request.role,
            repository=request.repository,
            tag=request.tag,
            descriptor_digest=digest,
            descriptor_kind=kind,
            platform=request.platform,
            resolved_version=(
                _uv_version_from_config(config_document, source)
                if request.role == "uv-tool"
                else None
            ),
        )


type ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class UvManagedPythonIdentityProvider:
    """Read the release-bundled managed-Python catalog through exact host uv."""

    uv: HostUvRunner
    runner: ProcessRunner = subprocess.run

    def resolve(self, request: ManagedPythonIdentityRequest) -> ManagedPythonIdentity:
        source = "managed Python catalog"
        if not _DIGEST_PATTERN.fullmatch(request.catalog_descriptor_digest):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
        _normalized_exact_version_request(request.version, source)
        argv = self.uv.argv(
            (
                "python",
                "list",
                "--only-downloads",
                "--all-versions",
                "--all-platforms",
                "--all-arches",
                "--show-urls",
                "--output-format",
                "json",
                request.version,
            )
        )
        try:
            completed = self.runner(
                argv,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "UV_NO_PROGRESS": "1"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
        if completed.returncode != 0:
            raise IdentityProviderError(source, ProviderFailureKind.NETWORK)
        try:
            rows = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as error:
            raise IdentityProviderError(
                source, ProviderFailureKind.INVALID_RESPONSE
            ) from error
        if not isinstance(rows, list):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
        for row in rows:
            _validate_managed_python_catalog_row(row, source)
        matches = [row for row in rows if _managed_python_row_matches(row, request)]
        if not matches:
            raise IdentityProviderError(source, ProviderFailureKind.NOT_FOUND)
        if len(matches) != 1:
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
        row = matches[0]
        key = _required_string(row, "key", source)
        url = _required_string(row, "url", source)
        if urlparse(url).scheme != "https":
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
        return ManagedPythonIdentity(
            version=request.version,
            implementation=request.implementation,
            platform=request.platform,
            libc=request.libc,
            provider="uv-managed",
            catalog_descriptor_digest=request.catalog_descriptor_digest,
            catalog_key=key,
            catalog_url=url,
        )


@dataclass(frozen=True, slots=True)
class GitOfficialComfyUIIdentityProvider:
    git_executable: str = "git"
    runner: ProcessRunner = subprocess.run

    def list_releases(self, repository: str) -> tuple[OfficialComfyUIIdentity, ...]:
        source = "official ComfyUI"
        _require_official_comfyui_repository(repository)
        output = _run_git_ls_remote(
            repository,
            options=("--tags",),
            patterns=(),
            source=source,
            git_executable=self.git_executable,
            runner=self.runner,
        )
        tags: dict[str, str] = {}
        peeled: dict[str, str] = {}
        for commit, ref in _parse_git_output(output, source):
            if not ref.startswith("refs/tags/"):
                continue
            base_ref = ref.removesuffix("^{}")
            if _formal_release_from_ref(base_ref) is None:
                continue
            if ref.endswith("^{}"):
                peeled[base_ref] = commit
            else:
                tags[base_ref] = commit
        identities = [
            OfficialComfyUIIdentity(
                repository=COMFYUI_REPOSITORY,
                commit=peeled.get(ref, commit),
                formal_release=_formal_release_from_ref(ref),
            )
            for ref, commit in tags.items()
        ]
        return tuple(
            sorted(identities, key=lambda item: Version(item.formal_release or "0"))
        )

    def resolve(
        self, request: OfficialComfyUIIdentityRequest
    ) -> OfficialComfyUIIdentity:
        _require_official_comfyui_repository(request.repository)
        commit = _resolve_git_ref(
            request.repository,
            request.ref,
            source="official ComfyUI",
            git_executable=self.git_executable,
            runner=self.runner,
        )
        return OfficialComfyUIIdentity(
            repository=COMFYUI_REPOSITORY,
            commit=commit,
            formal_release=_formal_release_from_ref(request.ref),
        )

    def is_ancestor(self, repository: str, ancestor: str, descendant: str) -> bool:
        """Prove one resolved commit is inside the supported official history."""
        source = "official ComfyUI ancestry"
        _require_official_comfyui_repository(repository)
        if not _COMMIT_PATTERN.fullmatch(ancestor) or not _COMMIT_PATTERN.fullmatch(
            descendant
        ):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
        environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
            "SSH_ASKPASS": "",
        }
        try:
            with tempfile.TemporaryDirectory(prefix="cdh-comfyui-ancestry-") as raw:
                checkout = Path(raw) / "repository.git"
                cloned = self.runner(
                    (
                        self.git_executable,
                        "clone",
                        "--bare",
                        "--filter=blob:none",
                        "--",
                        repository,
                        os.fspath(checkout),
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=_GIT_TIMEOUT_SECONDS,
                    env=environment,
                )
                if cloned.returncode != 0:
                    raise IdentityProviderError(source, ProviderFailureKind.NETWORK)
                checked = self.runner(
                    (
                        self.git_executable,
                        "-C",
                        os.fspath(checkout),
                        "merge-base",
                        "--is-ancestor",
                        ancestor,
                        descendant,
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=_GIT_TIMEOUT_SECONDS,
                    env=environment,
                )
        except subprocess.TimeoutExpired as error:
            raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
        except OSError as error:
            raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
        if checked.returncode == 0:
            return True
        if checked.returncode == 1:
            return False
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)


@dataclass(frozen=True, slots=True)
class HttpRegistryNodeIdentityProvider:
    client: httpx.Client
    base_url: str = "https://api.comfy.org"

    def list_versions(self, node_id: str) -> tuple[RegistryNodeIdentity, ...]:
        source = "Comfy Registry"
        response = _http_get(
            self.client,
            f"{self.base_url}/nodes/{quote(node_id, safe='')}/versions",
            source,
        )
        document = _response_json_value(response, source)
        rows = document.get("versions") if isinstance(document, Mapping) else document
        if not isinstance(rows, list):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
        identities: list[RegistryNodeIdentity] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise IdentityProviderError(
                    source, ProviderFailureKind.INVALID_RESPONSE
                )
            if row.get("node_id", node_id) != node_id:
                raise IdentityProviderError(
                    source, ProviderFailureKind.INVALID_RESPONSE
                )
            identities.append(
                RegistryNodeIdentity(
                    type="registry",
                    node_id=node_id,
                    version=_parsed_registry_exact_version_response(
                        _required_string(row, "version", source), source
                    ),
                )
            )
        return tuple(sorted(identities, key=lambda item: Version(item.version)))

    def resolve(self, request: RegistryNodeIdentityRequest) -> RegistryNodeIdentity:
        source = "Comfy Registry"
        version = _normalized_registry_exact_version_request(request.version, source)
        for identity in self.list_versions(request.node_id):
            if identity.version == version:
                return identity
        raise IdentityProviderError(source, ProviderFailureKind.NOT_FOUND)


@dataclass(frozen=True, slots=True)
class GitDirectIdentityProvider:
    git_executable: str = "git"
    runner: ProcessRunner = subprocess.run

    def resolve(self, request: DirectGitIdentityRequest) -> DirectGitIdentity:
        commit = _resolve_git_ref(
            request.url,
            request.ref,
            source="direct Git",
            git_executable=self.git_executable,
            runner=self.runner,
        )
        return DirectGitIdentity(type="git", url=request.url, commit=commit)


@dataclass(frozen=True, slots=True)
class FilesystemLocalExecutableIdentityProvider:
    """Hash one validated regular trusted-code input without following symlinks."""

    def resolve(
        self, request: LocalExecutableIdentityRequest
    ) -> LocalExecutableIdentity:
        source = "local executable"
        relative = request.relative_path
        identity = request.canonical_path
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or identity.is_absolute()
            or not identity.parts
            or ".." in identity.parts
        ):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
        try:
            root = request.root.resolve(strict=True)
            parent = root
            for component in relative.parts[:-1]:
                parent /= component
                if not stat.S_ISDIR(parent.lstat().st_mode):
                    raise IdentityProviderError(source, ProviderFailureKind.LOCAL_INPUT)
            path = parent / relative.name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise IdentityProviderError(source, ProviderFailureKind.LOCAL_INPUT)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except IdentityProviderError:
            raise
        except OSError as error:
            raise IdentityProviderError(
                source, ProviderFailureKind.LOCAL_INPUT
            ) from error
        return LocalExecutableIdentity(
            relative_path=identity, digest=f"sha256:{digest}"
        )


def _split_oci_repository(value: str) -> tuple[str, str]:
    if not value or any(character in value for character in "@:\r\n\0"):
        raise IdentityProviderError("OCI registry", ProviderFailureKind.INVALID_REQUEST)
    first, separator, remainder = value.partition("/")
    if separator and ("." in first or first == "localhost"):
        registry, repository = first, remainder
    else:
        registry, repository = "registry-1.docker.io", value
    if not repository or any(part in {"", ".", ".."} for part in repository.split("/")):
        raise IdentityProviderError("OCI registry", ProviderFailureKind.INVALID_REQUEST)
    if "/" not in repository and registry == "registry-1.docker.io":
        repository = f"library/{repository}"
    return registry, repository


def _oci_get(
    client: httpx.Client,
    url: str,
    *,
    headers: Mapping[str, str],
    source: str,
) -> httpx.Response:
    try:
        return client.get(
            url, headers={key: value for key, value in headers.items() if value}
        )
    except httpx.RequestError as error:
        raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error


def _oci_bearer_authorization(
    client: httpx.Client, challenge: str, repository: str
) -> str:
    source = "OCI registry"
    scheme, _, parameters = challenge.partition(" ")
    if scheme.lower() != "bearer":
        raise IdentityProviderError(source, ProviderFailureKind.AUTHENTICATION)
    values = dict(_BEARER_PARAMETER.findall(parameters))
    realm = values.get("realm", "")
    parsed_realm = urlparse(realm)
    if parsed_realm.scheme != "https" or not parsed_realm.netloc:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    query = {
        "service": values.get("service", ""),
        "scope": values.get("scope", f"repository:{repository}:pull"),
    }
    token_response = _http_get(client, f"{realm}?{urlencode(query)}", source)
    token_document = _response_json(token_response, source)
    token = token_document.get("token", token_document.get("access_token"))
    if not isinstance(token, str) or not token:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return f"Bearer {token}"


def _http_get(client: httpx.Client, url: str, source: str) -> httpx.Response:
    try:
        response = client.get(url)
    except httpx.RequestError as error:
        raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
    _raise_for_http_status(response, source)
    return response


def _raise_for_http_status(response: httpx.Response, source: str) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status == 404:
        kind = ProviderFailureKind.NOT_FOUND
    elif status in {401, 403}:
        kind = ProviderFailureKind.AUTHENTICATION
    elif status == 429:
        kind = ProviderFailureKind.RATE_LIMIT
    elif status >= 500:
        kind = ProviderFailureKind.NETWORK
    else:
        kind = ProviderFailureKind.INVALID_RESPONSE
    raise IdentityProviderError(source, kind)


def _response_json(response: httpx.Response, source: str) -> Mapping[str, object]:
    document = _response_json_value(response, source)
    if not isinstance(document, Mapping):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return document


def _response_json_value(response: httpx.Response, source: str) -> object:
    try:
        document = response.json()
    except ValueError as error:
        raise IdentityProviderError(
            source, ProviderFailureKind.INVALID_RESPONSE
        ) from error
    return document


def _required_string(document: Mapping[str, object], field: str, source: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return value


def _content_matches_digest(content: bytes, digest: str) -> bool:
    if not _DIGEST_PATTERN.fullmatch(digest):
        return False
    observed = hashlib.sha256(content).hexdigest()
    return digest == f"sha256:{observed}"


def _select_index_platform(
    document: Mapping[str, object], platform: str, source: str
) -> tuple[str, str]:
    operating_system, architecture = platform.split("/", maxsplit=1)
    manifests = document.get("manifests")
    if not isinstance(manifests, list):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    matches = []
    for descriptor in manifests:
        if not isinstance(descriptor, Mapping):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
        descriptor_platform = descriptor.get("platform")
        if not isinstance(descriptor_platform, Mapping):
            continue
        if (
            descriptor_platform.get("os") == operating_system
            and descriptor_platform.get("architecture") == architecture
        ):
            matches.append(descriptor)
    if len(matches) != 1:
        raise IdentityProviderError(
            source,
            ProviderFailureKind.NOT_FOUND
            if not matches
            else ProviderFailureKind.INVALID_RESPONSE,
        )
    digest = matches[0].get("digest")
    if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    media_type = matches[0].get("mediaType")
    if not isinstance(media_type, str) or not media_type:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return digest, media_type


def _verify_manifest_config_platform(
    client: httpx.Client,
    document: Mapping[str, object],
    *,
    registry: str,
    repository: str,
    authorization: str,
    platform: str,
    platform_mismatch_kind: ProviderFailureKind,
    source: str,
) -> Mapping[str, object]:
    config = document.get("config")
    if not isinstance(config, Mapping):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    config_digest = _required_string(config, "digest", source)
    if not _DIGEST_PATTERN.fullmatch(config_digest):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    blob_url = f"https://{registry}/v2/{repository}/blobs/{config_digest}"
    blob_response = _oci_get(
        client,
        blob_url,
        headers={"Authorization": authorization},
        source=source,
    )
    _raise_for_http_status(blob_response, source)
    if not _content_matches_digest(blob_response.content, config_digest):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    config_document = _response_json(blob_response, source)
    _require_config_platform(
        config_document,
        platform,
        source,
        mismatch_kind=platform_mismatch_kind,
    )
    return config_document


def _uv_version_from_config(document: Mapping[str, object], source: str) -> str:
    config = document.get("config")
    if not isinstance(config, Mapping):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    labels = config.get("Labels")
    if not isinstance(labels, Mapping):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    value = labels.get("org.opencontainers.image.version")
    if not isinstance(value, str):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    try:
        version = Version(value)
    except InvalidVersion as error:
        raise IdentityProviderError(
            source, ProviderFailureKind.INVALID_RESPONSE
        ) from error
    if str(version) != value or version.is_prerelease or version.is_devrelease:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return value


def _require_config_platform(
    document: Mapping[str, object],
    platform: str,
    source: str,
    *,
    mismatch_kind: ProviderFailureKind,
) -> None:
    operating_system, architecture = platform.split("/", maxsplit=1)
    if (
        document.get("os") != operating_system
        or document.get("architecture") != architecture
    ):
        raise IdentityProviderError(source, mismatch_kind)


def _managed_python_row_matches(
    row: object, request: ManagedPythonIdentityRequest
) -> bool:
    return isinstance(row, Mapping) and all(
        (
            row.get("version") == request.version,
            row.get("implementation") == request.implementation,
            row.get("os") == "linux",
            row.get("arch") == "x86_64",
            row.get("libc") == request.libc,
            row.get("variant") == "default",
            row.get("path") is None,
        )
    )


def _validate_managed_python_catalog_row(row: object, source: str) -> None:
    if not isinstance(row, Mapping):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    for field in (
        "key",
        "version",
        "url",
        "os",
        "variant",
        "implementation",
        "arch",
        "libc",
    ):
        _required_string(row, field, source)
    _parsed_exact_version_response(row["version"], source)
    if urlparse(str(row["url"])).scheme != "https":
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    if row.get("path") is not None and not isinstance(row.get("path"), str):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)


def _normalized_exact_version_request(value: str, source: str) -> str:
    try:
        version = Version(value.removeprefix("v"))
    except InvalidVersion as error:
        raise IdentityProviderError(
            source, ProviderFailureKind.INVALID_REQUEST
        ) from error
    if version.local is not None:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
    return str(version)


def _parsed_exact_version_response(value: object, source: str) -> str:
    if not isinstance(value, str):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    try:
        version = Version(value.removeprefix("v"))
    except InvalidVersion as error:
        raise IdentityProviderError(
            source, ProviderFailureKind.INVALID_RESPONSE
        ) from error
    if version.local is not None:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return str(version)


def _normalized_registry_exact_version_request(value: str, source: str) -> str:
    try:
        normalized = normalize_registry_version(value)
    except ValueError as error:
        raise IdentityProviderError(
            source, ProviderFailureKind.INVALID_REQUEST
        ) from error
    if normalized == "latest" or any(character in normalized for character in "<>=!"):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
    return normalized


def _parsed_registry_exact_version_response(value: str, source: str) -> str:
    try:
        normalized = normalize_registry_version(value)
    except ValueError as error:
        raise IdentityProviderError(
            source, ProviderFailureKind.INVALID_RESPONSE
        ) from error
    if normalized == "latest" or any(character in normalized for character in "<>=!"):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return normalized


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


def _require_official_comfyui_repository(repository: str) -> None:
    if repository != COMFYUI_REPOSITORY:
        raise IdentityProviderError(
            "official ComfyUI", ProviderFailureKind.INVALID_REQUEST
        )


def _run_git_ls_remote(
    url: str,
    *,
    options: tuple[str, ...],
    patterns: tuple[str, ...],
    source: str,
    git_executable: str,
    runner: ProcessRunner,
) -> str:
    if not is_argv_value(url) or any(
        not is_argv_value(value) for value in (*options, *patterns)
    ):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
        "SSH_ASKPASS": "",
    }
    try:
        completed = runner(
            (
                git_executable,
                "ls-remote",
                *options,
                "--end-of-options",
                url,
                *patterns,
            ),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
    except OSError as error:
        raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
    if completed.returncode != 0:
        raise IdentityProviderError(source, ProviderFailureKind.NETWORK)
    return completed.stdout


def _parse_git_output(output: str, source: str) -> tuple[tuple[str, str], ...]:
    resolved: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2 or not _COMMIT_PATTERN.fullmatch(parts[0]):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
        resolved.append((parts[0], parts[1]))
    return tuple(resolved)


def _resolve_git_ref(
    url: str,
    ref: str,
    *,
    source: str,
    git_executable: str,
    runner: ProcessRunner,
) -> str:
    output = _run_git_ls_remote(
        url,
        options=(),
        patterns=(ref,),
        source=source,
        git_executable=git_executable,
        runner=runner,
    )
    resolved = list(_parse_git_output(output, source))
    if not resolved:
        raise IdentityProviderError(source, ProviderFailureKind.NOT_FOUND)
    base_refs = {candidate.removesuffix("^{}") for _, candidate in resolved}
    if ref != "HEAD" and not ref.startswith("refs/") and len(base_refs) != 1:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    for commit, candidate in resolved:
        if candidate.endswith("^{}"):
            return commit
    if len(base_refs) != 1:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return resolved[0][0]
