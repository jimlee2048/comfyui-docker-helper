"""Shared URL validation helpers."""

from urllib.parse import urlsplit


def is_http_url(url: str) -> bool:
    """Return whether a URL is HTTP(S), host-qualified, and shell-safe."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(hostname)
        and "\\" not in parsed.netloc
        and not any(character.isspace() for character in parsed.netloc)
    )
