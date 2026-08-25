"""Docker-managed Python identity provider."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.validation.values import (
    validate_managed_python_catalog_key,
)
from comfyui_docker_helper.host.planning.providers.contracts import (
    IdentityProviderError,
    ManagedPythonIdentity,
    ManagedPythonIdentityRequest,
    ProviderFailureKind,
)
from comfyui_docker_helper.host.planning.providers.uv import (
    ManagedPythonCatalogOperation,
    UvDockerExecutor,
    UvDockerExecutorError,
    UvResolverDescriptor,
)

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DockerManagedPythonIdentityProvider:
    """Read the managed-Python catalog through the exact uv OCI descriptor."""

    executor: UvDockerExecutor | None = None

    def resolve(self, request: ManagedPythonIdentityRequest) -> ManagedPythonIdentity:
        source = "managed Python catalog"
        if not _DIGEST_PATTERN.fullmatch(request.catalog_descriptor_digest):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
        _normalized_exact_version_request(request.version, source)
        try:
            result = (self.executor or UvDockerExecutor()).execute(
                UvResolverDescriptor(
                    request.catalog_descriptor_digest, request.platform
                ),
                ManagedPythonCatalogOperation(request.version),
            )
        except UvDockerExecutorError as error:
            raise IdentityProviderError(
                source,
                ProviderFailureKind.NETWORK,
                controlled_detail=str(error),
            ) from error
        try:
            rows = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as error:
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


def _required_string(document: Mapping[str, object], field: str, source: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return value


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
    try:
        validate_managed_python_catalog_key(str(row["key"]))
    except ValueError as error:
        raise IdentityProviderError(
            source, ProviderFailureKind.INVALID_RESPONSE
        ) from error
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
