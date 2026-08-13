"""Pure HTTP(S) Git credential-context parsing and route selection."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from comfyui_docker_helper.config.credential_secrets import (
    CREDENTIAL_SECRET_MAX_BYTES,
)
from comfyui_docker_helper.config.value_validation import has_control_characters

__all__ = [
    "GIT_CREDENTIAL_VALUE_MAX_BYTES",
    "GitCredentialContext",
    "GitCredentialContextError",
    "canonicalize_git_credential_context",
    "git_credential_secret_id",
    "git_credential_secret_target",
    "has_password_userinfo",
    "parse_git_credential_context",
    "parse_git_credential_fields",
    "select_git_credential_context",
]

# Keep the established Git-facing name while one consumer-neutral acquisition
# envelope owns credential Secret size.
GIT_CREDENTIAL_VALUE_MAX_BYTES = CREDENTIAL_SECRET_MAX_BYTES

type GitCredentialScheme = Literal["http", "https"]
type GitCredentialContextErrorCode = Literal[
    "invalid",
    "query_or_fragment",
    "password_userinfo",
]

_DEFAULT_PORTS: dict[GitCredentialScheme, int] = {"http": 80, "https": 443}
_SECRET_ID_PATTERN = re.compile(r"cdh-git-credential-[a-z][a-z0-9_-]{0,63}\Z")


def git_credential_secret_id(secret_name: str) -> str:
    """Project one admitted logical Secret name to its stable BuildKit ID."""
    secret_id = f"cdh-git-credential-{secret_name}"
    if _SECRET_ID_PATTERN.fullmatch(secret_id) is None:
        raise ValueError("Git credential Secret name must be canonical")
    return secret_id


def git_credential_secret_target(secret_id: str) -> str:
    """Project one admitted stable ID to its fixed BuildKit mount target."""
    if _SECRET_ID_PATTERN.fullmatch(secret_id) is None:
        raise ValueError("Git credential Secret ID must be canonical")
    return f"/run/secrets/{secret_id}"


@dataclass(frozen=True, slots=True)
class GitCredentialContext:
    """One normalized route or helper-supplied credential context."""

    scheme: GitCredentialScheme
    host: str
    port: int
    path_segments: tuple[str, ...]

    @property
    def canonical_url(self) -> str:
        """Return a safe canonical match spelling without userinfo."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port == _DEFAULT_PORTS[self.scheme] else f":{self.port}"
        path = "/".join(self.path_segments)
        if self.path_segments and self.path_segments[-1] == "":
            path += "/"
        return f"{self.scheme}://{host}{port}/{path}"


class GitCredentialContextError(ValueError):
    """A content-free expected credential-context parsing failure."""

    def __init__(
        self,
        code: GitCredentialContextErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


def has_password_userinfo(value: str) -> bool:
    """Return whether an HTTP(S) URL structurally carries a password."""
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        password = parsed.password
    except (TypeError, ValueError):
        return False
    return scheme in _DEFAULT_PORTS and password is not None


def parse_git_credential_context(value: str) -> GitCredentialContext:
    """Parse one authored HTTP(S) credential route without performing I/O."""
    if "?" in value or "#" in value:
        raise GitCredentialContextError(
            "query_or_fragment",
            "credential contexts must not contain query or fragment components",
        )
    if (
        not value
        or has_control_characters(value)
        or any(character.isspace() for character in value)
    ):
        raise _invalid_context()
    try:
        parsed = urlsplit(value)
        scheme_text = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise _invalid_context() from error
    if scheme_text not in _DEFAULT_PORTS or not hostname:
        raise _invalid_context()
    if "\\" in parsed.netloc:
        raise _invalid_context()
    if parsed.password is not None:
        raise GitCredentialContextError(
            "password_userinfo",
            "credential context URLs must not contain a password",
        )

    scheme: GitCredentialScheme = scheme_text
    return GitCredentialContext(
        scheme=scheme,
        host=hostname.lower(),
        port=_DEFAULT_PORTS[scheme] if port is None else port,
        path_segments=_path_segments(parsed.path),
    )


def canonicalize_git_credential_context(value: str) -> str:
    """Return the safe canonical spelling of one authored route context."""
    return parse_git_credential_context(value).canonical_url


def parse_git_credential_fields(
    protocol: str,
    host: str,
    path: str | None = None,
) -> GitCredentialContext | None:
    """Normalize the protocol, host, and optional path supplied by Git."""
    if (
        not protocol
        or not host
        or has_control_characters(protocol)
        or has_control_characters(host)
        or any(character.isspace() for character in protocol)
        or any(character.isspace() for character in host)
        or any(character in host for character in "@/\\?#")
        or (
            path is not None
            and (has_control_characters(path) or "?" in path or "#" in path)
        )
    ):
        return None
    try:
        authority = parse_git_credential_context(f"{protocol}://{host}/")
    except GitCredentialContextError:
        return None
    return GitCredentialContext(
        scheme=authority.scheme,
        host=authority.host,
        port=authority.port,
        path_segments=_path_segments(path or ""),
    )


def select_git_credential_context(
    candidates: Sequence[GitCredentialContext],
    request: GitCredentialContext,
) -> int | None:
    """Return the index of the longest path-segment prefix for one request."""
    selected_index: int | None = None
    selected_depth = -1
    for index, candidate in enumerate(candidates):
        if (
            candidate.scheme != request.scheme
            or candidate.host != request.host
            or candidate.port != request.port
            or len(candidate.path_segments) > len(request.path_segments)
            or request.path_segments[: len(candidate.path_segments)]
            != candidate.path_segments
        ):
            continue
        depth = len(candidate.path_segments)
        if depth > selected_depth:
            selected_index = index
            selected_depth = depth
    return selected_index


def _path_segments(path: str) -> tuple[str, ...]:
    if path.startswith("/"):
        path = path[1:]
    if path.endswith("/"):
        path = path[:-1]
    if not path:
        return ()
    return tuple(path.split("/"))


def _invalid_context() -> GitCredentialContextError:
    return GitCredentialContextError(
        "invalid",
        "credential context must be one host-qualified HTTP(S) URL",
    )
