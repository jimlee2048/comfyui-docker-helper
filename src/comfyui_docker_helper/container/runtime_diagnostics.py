"""Bounded, control-safe runtime download diagnostics for container logs."""

from __future__ import annotations

from urllib.parse import urlsplit

from comfyui_docker_helper.container.runtime_state import summarize_runtime_error

_UNKNOWN_SOURCE_HOST = "unknown"
_IDENTITY_HEX_LENGTH = 12
_REASON_MAX_LENGTH = 160


def runtime_source_host(source: str) -> str:
    """Return a URL host suitable for logs without userinfo or path details."""
    try:
        host = urlsplit(source).hostname
    except ValueError:
        return _UNKNOWN_SOURCE_HOST
    if not host:
        return _UNKNOWN_SOURCE_HOST
    return host.lower()


def short_runtime_identity(identity: str) -> str:
    """Return a short stable digest identity for logs."""
    algorithm, separator, digest = identity.partition(":")
    if not separator:
        return identity[:_IDENTITY_HEX_LENGTH]
    return f"{algorithm}:{digest[:_IDENTITY_HEX_LENGTH]}"


def runtime_error_reason(error: object) -> str:
    """Return a quoted, bounded, control-safe error reason for logs."""
    reason = summarize_runtime_error(error, max_length=_REASON_MAX_LENGTH)
    return _quote_log_value(reason)


def _quote_log_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
