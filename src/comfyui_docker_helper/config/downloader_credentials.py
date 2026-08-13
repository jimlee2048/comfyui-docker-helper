"""Pure HTTP(S) downloader credential-route parsing and selection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

from comfyui_docker_helper.config.value_validation import has_control_characters

__all__ = [
    "DownloaderCredentialContext",
    "DownloaderCredentialContextError",
    "canonicalize_downloader_credential_context",
    "downloader_httpx_request_context",
    "downloader_request_context",
    "parse_downloader_credential_context",
    "parse_downloader_request_url",
    "select_downloader_credential_context",
]

type DownloaderCredentialScheme = Literal["http", "https"]
type DownloaderCredentialContextErrorCode = Literal[
    "invalid",
    "query_or_fragment",
    "userinfo",
]

_DEFAULT_PORTS: dict[DownloaderCredentialScheme, int] = {"http": 80, "https": 443}


@dataclass(frozen=True, slots=True)
class DownloaderCredentialContext:
    """One normalized route or actual outbound request context."""

    scheme: DownloaderCredentialScheme
    host: str
    port: int
    path_segments: tuple[str, ...]

    @property
    def canonical_url(self) -> str:
        """Return the safe canonical route spelling without userinfo."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port == _DEFAULT_PORTS[self.scheme] else f":{self.port}"
        path = "/".join(self.path_segments)
        if self.path_segments and self.path_segments[-1] == "":
            path += "/"
        return f"{self.scheme}://{host}{port}/{path}"


class DownloaderCredentialContextError(ValueError):
    """A content-free expected downloader route parsing failure."""

    def __init__(
        self,
        code: DownloaderCredentialContextErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


def parse_downloader_credential_context(value: str) -> DownloaderCredentialContext:
    """Parse one authored credential route without performing I/O."""
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
        _ = parsed.port
        username = parsed.username
        password = parsed.password
    except (TypeError, ValueError) as error:
        raise _invalid_context() from error
    if scheme_text not in _DEFAULT_PORTS or not hostname or "\\" in parsed.netloc:
        raise _invalid_context()
    if username is not None or password is not None:
        raise DownloaderCredentialContextError(
            "userinfo",
            "credential route URLs must not contain user information",
        )
    if parsed.query or parsed.fragment:
        raise DownloaderCredentialContextError(
            "query_or_fragment",
            "credential routes must not contain query or fragment components",
        )
    try:
        return _httpx_url_context(httpx.URL(value))
    except (TypeError, ValueError, httpx.InvalidURL) as error:
        raise _invalid_context() from error


def canonicalize_downloader_credential_context(value: str) -> str:
    """Return the safe canonical spelling of one authored route."""
    return parse_downloader_credential_context(value).canonical_url


def parse_downloader_request_url(value: str) -> DownloaderCredentialContext:
    """Parse one actual HTTP(S) URL while excluding query from route selection."""
    try:
        return _httpx_url_context(httpx.URL(value))
    except (TypeError, ValueError, httpx.InvalidURL) as error:
        raise _invalid_context() from error


def downloader_request_context(
    *,
    scheme: str,
    host: str,
    port: int | None,
    path: str,
) -> DownloaderCredentialContext:
    """Build route context from transport-effective public URL components."""
    scheme_text = scheme.lower()
    if (
        scheme_text not in _DEFAULT_PORTS
        or not host
        or has_control_characters(host)
        or any(character.isspace() for character in host)
        or any(character in host for character in "@/\\?#")
        or has_control_characters(path)
        or (port is not None and (type(port) is not int or not 0 <= port <= 65_535))
    ):
        raise _invalid_context()
    typed_scheme: DownloaderCredentialScheme = scheme_text
    return DownloaderCredentialContext(
        scheme=typed_scheme,
        host=host.lower(),
        port=_DEFAULT_PORTS[typed_scheme] if port is None else port,
        path_segments=_path_segments(path),
    )


def downloader_httpx_request_context(
    *,
    scheme: str,
    host: str,
    port: int | None,
    raw_path: bytes,
    query: bytes,
) -> DownloaderCredentialContext:
    """Build context from HTTPX raw path/query components without URL decoding."""
    if type(raw_path) is not bytes or type(query) is not bytes:
        raise _invalid_context()
    suffix = b"?" + query
    if raw_path.endswith(suffix):
        raw_path = raw_path[: -len(suffix)]
    elif query:
        raise _invalid_context()
    try:
        path = raw_path.decode("ascii")
    except UnicodeDecodeError as error:
        raise _invalid_context() from error
    return downloader_request_context(
        scheme=scheme,
        host=host,
        port=port,
        path=path,
    )


def _httpx_url_context(url: httpx.URL) -> DownloaderCredentialContext:
    return downloader_httpx_request_context(
        scheme=url.scheme,
        host=url.host,
        port=url.port,
        raw_path=url.raw_path,
        query=url.query,
    )


def select_downloader_credential_context(
    candidates: Sequence[DownloaderCredentialContext],
    request: DownloaderCredentialContext,
) -> int | None:
    """Return the first longest path-segment prefix for one outbound request."""
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


def _invalid_context() -> DownloaderCredentialContextError:
    return DownloaderCredentialContextError(
        "invalid",
        "credential route must be one host-qualified HTTP(S) URL",
    )
