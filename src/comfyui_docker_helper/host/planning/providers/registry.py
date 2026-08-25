"""Comfy Registry node identity provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from packaging.version import Version

from comfyui_docker_helper.config.validation.registry import registry_resource_identity
from comfyui_docker_helper.config.validation.selectors import normalize_registry_version
from comfyui_docker_helper.host.planning.providers.contracts import (
    IdentityProviderError,
    ProviderFailureKind,
    RegistryNodeIdentity,
    RegistryNodeIdentityRequest,
)


@dataclass(frozen=True, slots=True)
class HttpRegistryNodeIdentityProvider:
    client: httpx.Client
    base_url: str = "https://api.comfy.org"

    def list_versions(self, node_id: str) -> tuple[RegistryNodeIdentity, ...]:
        source = "Comfy Registry"
        try:
            request_identity = registry_resource_identity(node_id)
        except ValueError as error:
            raise IdentityProviderError(
                source, ProviderFailureKind.INVALID_REQUEST
            ) from error
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
            response_node_id = row.get("node_id", node_id)
            if not isinstance(response_node_id, str):
                raise IdentityProviderError(
                    source, ProviderFailureKind.INVALID_RESPONSE
                )
            try:
                response_identity = registry_resource_identity(response_node_id)
            except ValueError as error:
                raise IdentityProviderError(
                    source, ProviderFailureKind.INVALID_RESPONSE
                ) from error
            if response_identity != request_identity:
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


def _required_string(document: Mapping[str, object], field: str, source: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return value


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


def _response_json_value(response: httpx.Response, source: str) -> object:
    try:
        document = response.json()
    except ValueError as error:
        raise IdentityProviderError(
            source, ProviderFailureKind.INVALID_RESPONSE
        ) from error
    return document


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
