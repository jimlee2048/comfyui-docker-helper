"""Credential Secret value admission and stable consumer-specific IDs."""

from __future__ import annotations

import re

__all__ = [
    "CREDENTIAL_SECRET_MAX_BYTES",
    "BearerTokenError",
    "downloader_credential_secret_id",
    "downloader_credential_secret_target",
    "validate_bearer_token",
]

CREDENTIAL_SECRET_MAX_BYTES = 65_525

_BEARER_TOKEN_PATTERN = re.compile(rb"[A-Za-z0-9\-._~+/]+=*\Z")
_DOWNLOADER_SECRET_ID_PATTERN = re.compile(
    r"cdh-downloader-credential-[a-z][a-z0-9_-]{0,63}\Z"
)


class BearerTokenError(ValueError):
    """A content-free Bearer token admission failure."""


def validate_bearer_token(value: bytes) -> None:
    """Admit one exact RFC 6750 ``b64token`` value."""
    if (
        len(value) > CREDENTIAL_SECRET_MAX_BYTES
        or _BEARER_TOKEN_PATTERN.fullmatch(value) is None
    ):
        raise BearerTokenError("Bearer credential value is invalid")


def downloader_credential_secret_id(secret_name: str) -> str:
    """Project one admitted logical Secret name to its downloader BuildKit ID."""
    secret_id = f"cdh-downloader-credential-{secret_name}"
    if _DOWNLOADER_SECRET_ID_PATTERN.fullmatch(secret_id) is None:
        raise ValueError("downloader credential Secret name must be canonical")
    return secret_id


def downloader_credential_secret_target(secret_id: str) -> str:
    """Project one downloader BuildKit ID to its fixed mount target."""
    if _DOWNLOADER_SECRET_ID_PATTERN.fullmatch(secret_id) is None:
        raise ValueError("downloader credential Secret ID must be canonical")
    return f"/run/secrets/{secret_id}"
