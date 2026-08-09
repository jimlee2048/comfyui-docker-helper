"""Internal binary protocol core for cdh Git credential helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from comfyui_docker_helper.config.git_credentials import (
    GIT_CREDENTIAL_VALUE_MAX_BYTES,
    GitCredentialContext,
    parse_git_credential_fields,
    select_git_credential_context,
)

__all__ = [
    "GitCredentialDecision",
    "GitCredentialProtocolError",
    "GitCredentialRuntimeRoute",
    "evaluate_git_credential_request",
    "render_git_credential_response",
]

MAX_GIT_CREDENTIAL_LINE_BYTES = 65_535

type GitCredentialProtocolErrorCode = Literal[
    "invalid_line",
    "line_too_long",
    "nul_byte",
    "repeated_scalar",
    "invalid_response_value",
]

_CONTEXT_KEYS = frozenset({b"protocol", b"host", b"path", b"username"})


@dataclass(frozen=True, slots=True)
class GitCredentialRuntimeRoute:
    """One process-local safe route and its configured username bytes."""

    context: GitCredentialContext
    username: bytes


@dataclass(frozen=True, slots=True)
class GitCredentialDecision:
    """A selected route that may now have its password resolved."""

    route_index: int
    username: bytes


class GitCredentialProtocolError(ValueError):
    """One content-free binary credential protocol failure."""

    def __init__(self, code: GitCredentialProtocolErrorCode) -> None:
        self.code = code
        super().__init__(f"Git credential protocol failed ({code})")


def evaluate_git_credential_request(
    operation: str,
    payload: bytes,
    routes: Sequence[GitCredentialRuntimeRoute],
) -> GitCredentialDecision | None:
    """Parse and route one helper request without resolving its password."""
    if operation != "get":
        return None

    fields = _parse_context_fields(payload)
    protocol = fields.get(b"protocol")
    host = fields.get(b"host")
    if protocol is None or host is None:
        return None
    try:
        protocol_text = protocol.decode("utf-8", errors="strict")
        host_text = host.decode("utf-8", errors="strict")
        path_value = fields.get(b"path")
        path_text = (
            None if path_value is None else path_value.decode("utf-8", errors="strict")
        )
    except UnicodeDecodeError:
        return None
    request = parse_git_credential_fields(protocol_text, host_text, path_text)
    if request is None:
        return None

    route_index = select_git_credential_context(
        tuple(route.context for route in routes), request
    )
    if route_index is None:
        return None
    username = routes[route_index].username
    requested_username = fields.get(b"username")
    if requested_username is not None and requested_username != username:
        return None
    _validate_response_value(username)
    return GitCredentialDecision(route_index=route_index, username=username)


def render_git_credential_response(
    decision: GitCredentialDecision,
    password: bytes,
) -> bytes:
    """Render exact username/password bytes for one selected route."""
    _validate_response_value(decision.username)
    _validate_response_value(password)
    return b"username=" + decision.username + b"\npassword=" + password + b"\n"


def _parse_context_fields(payload: bytes) -> dict[bytes, bytes]:
    fields: dict[bytes, bytes] = {}
    cursor = 0
    while cursor < len(payload):
        newline = payload.find(b"\n", cursor)
        if newline < 0:
            line = payload[cursor:]
            framed_length = len(line)
            cursor = len(payload)
        else:
            line = payload[cursor:newline]
            framed_length = len(line) + 1
            cursor = newline + 1
        if framed_length > MAX_GIT_CREDENTIAL_LINE_BYTES:
            raise GitCredentialProtocolError("line_too_long")
        if not line:
            break
        if b"\0" in line:
            raise GitCredentialProtocolError("nul_byte")
        separator = line.find(b"=")
        if separator < 0:
            raise GitCredentialProtocolError("invalid_line")
        key = line[:separator]
        if key not in _CONTEXT_KEYS:
            continue
        if key in fields:
            raise GitCredentialProtocolError("repeated_scalar")
        fields[key] = line[separator + 1 :]
    return fields


def _validate_response_value(value: bytes) -> None:
    if (
        not value
        or len(value) > GIT_CREDENTIAL_VALUE_MAX_BYTES
        or any(character in value for character in b"\0\r\n")
    ):
        raise GitCredentialProtocolError("invalid_response_value")
