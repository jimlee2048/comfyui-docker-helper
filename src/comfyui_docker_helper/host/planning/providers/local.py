"""Local executable identity provider."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from comfyui_docker_helper.config.planning.inputs.executable import (
    LocalExecutableIdentityRequest,
)
from comfyui_docker_helper.filesystem.admission import read_regular_absolute_file
from comfyui_docker_helper.host.planning.providers.contracts import (
    IdentityProviderError,
    LocalExecutableIdentity,
    ProviderFailureKind,
)


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
            content = read_regular_absolute_file(request.root.joinpath(*relative.parts))
            digest = hashlib.sha256(content).hexdigest()
        except (OSError, ValueError) as error:
            raise IdentityProviderError(
                source, ProviderFailureKind.LOCAL_INPUT
            ) from error
        return LocalExecutableIdentity(
            relative_path=identity, digest=f"sha256:{digest}"
        )
