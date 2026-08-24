"""Docker Engine OCI identity provider."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from docker import DockerClient, from_env
from docker.errors import DockerException
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.planning.canonical_lock import (
    validate_oci_repository,
    validate_oci_tag,
)
from comfyui_docker_helper.host.planning.providers.contracts import (
    IdentityProviderError,
    OciIdentity,
    OciIdentityRequest,
    ProviderFailureKind,
)
from comfyui_docker_helper.host.planning.providers.uv import (
    UvDockerExecutorError,
    UvImageEvidenceError,
    UvResolverDescriptor,
    uv_image_version_label,
)

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_UV_IMAGE_VERSION_LABEL_PATTERN = re.compile(
    r"^(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*))(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?$"
)
_OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
_OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
type DockerClientFactory = Callable[[], DockerClient]
type UvImageEvidence = Callable[[UvResolverDescriptor], str | None]


def _default_docker_client() -> DockerClient:
    return from_env(
        version="auto",
        timeout=30,
        use_context=True,
        use_ssh_client=True,
    )


@dataclass(frozen=True, slots=True)
class DockerEngineOciIdentityProvider:
    """Resolve project-owned OCI tags through Docker Engine metadata."""

    client_factory: DockerClientFactory = _default_docker_client
    uv_image_evidence: UvImageEvidence = uv_image_version_label

    def resolve(self, request: OciIdentityRequest) -> OciIdentity:
        source = "Docker Engine OCI identity"
        if not _valid_oci_request(request):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
        try:
            client = self.client_factory()
            try:
                document = client.api.inspect_distribution(
                    f"{request.repository}:{request.tag}"
                )
            finally:
                client.close()
        except (DockerException, OSError) as error:
            raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error

        digest, kind = _engine_descriptor_identity(document, request.platform, source)
        resolved_version = None
        if request.role == "uv-tool":
            try:
                label = self.uv_image_evidence(
                    UvResolverDescriptor(digest, request.platform)
                )
            except UvImageEvidenceError as error:
                raise IdentityProviderError(
                    source, ProviderFailureKind.INVALID_RESPONSE
                ) from error
            except UvDockerExecutorError as error:
                raise IdentityProviderError(
                    source, ProviderFailureKind.NETWORK
                ) from error
            resolved_version = _uv_version_from_label(label, source)
        return OciIdentity(
            role=request.role,
            repository=request.repository,
            tag=request.tag,
            descriptor_digest=digest,
            descriptor_kind=kind,
            platform=request.platform,
            resolved_version=resolved_version,
        )


def _valid_oci_request(request: OciIdentityRequest) -> bool:
    try:
        validate_oci_repository(request.repository)
        validate_oci_tag(request.tag)
    except ValueError:
        return False
    return (
        request.role in {"cuda-base", "uv-tool"} and request.platform == "linux/amd64"
    )


def _engine_descriptor_identity(
    document: object,
    platform: str,
    source: str,
) -> tuple[str, Literal["index", "manifest"]]:
    if not isinstance(document, Mapping):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    descriptor = document.get("Descriptor")
    platforms = document.get("Platforms")
    if not isinstance(descriptor, Mapping) or not isinstance(platforms, list):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    digest = descriptor.get("digest")
    media_type = descriptor.get("mediaType")
    if not isinstance(digest, str) or _DIGEST_PATTERN.fullmatch(digest) is None:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    if media_type in _OCI_INDEX_MEDIA_TYPES:
        kind: Literal["index", "manifest"] = "index"
    elif media_type in _OCI_MANIFEST_MEDIA_TYPES:
        kind = "manifest"
    else:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    operating_system, architecture = platform.split("/", maxsplit=1)
    if not any(
        isinstance(item, Mapping)
        and item.get("os") == operating_system
        and item.get("architecture") == architecture
        for item in platforms
    ):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return digest, kind


def _uv_version_from_label(value: str | None, source: str) -> str:
    if not isinstance(value, str):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    match = _UV_IMAGE_VERSION_LABEL_PATTERN.fullmatch(value)
    if match is None:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    release = match.group("version")
    try:
        version = Version(release)
    except InvalidVersion as error:
        raise IdentityProviderError(
            source, ProviderFailureKind.INVALID_RESPONSE
        ) from error
    if str(version) != release or version.is_prerelease or version.is_devrelease:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return release
