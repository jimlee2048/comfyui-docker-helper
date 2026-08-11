"""Validation helpers for OpenSSH authorized_keys public key lines."""

import base64
import binascii
import struct
from dataclasses import dataclass

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_ssh_public_key

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticPath

_OPENSSH_PUBLIC_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)
_SECURITY_KEY_APPLICATION_FIELD_COUNTS = {
    "sk-ssh-ed25519@openssh.com": 3,
    "sk-ecdsa-sha2-nistp256@openssh.com": 4,
}
_AUTHORIZED_KEYS_FORBIDDEN_CHARACTERS = frozenset({"\n", "\r", "\x00"})


@dataclass(frozen=True, slots=True)
class SshPublicKeyNormalization:
    """Normalized key set plus authored invalid and redundant locations."""

    values: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]
    duplicate_paths: tuple[DiagnosticPath, ...]


def normalize_ssh_public_keys(
    values: list[str],
    *,
    path: DiagnosticPath,
    code: str,
) -> SshPublicKeyNormalization:
    """Return trimmed non-empty public keys plus validation diagnostics."""
    keys: list[str] = []
    diagnostics: list[Diagnostic] = []
    duplicate_paths: list[DiagnosticPath] = []
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(values):
        normalized = value.strip()
        if not normalized:
            continue
        key_error = _validate_ssh_public_key(normalized)
        if key_error is not None:
            diagnostics.append(
                Diagnostic(
                    path=(*path, index),
                    code=code,
                    message=key_error,
                )
            )
            continue
        parts = normalized.split(maxsplit=2)
        identity = (parts[0], parts[1])
        if identity in seen:
            duplicate_paths.append((*path, index))
            continue
        seen.add(identity)
        keys.append(normalized)
    return SshPublicKeyNormalization(
        tuple(keys),
        tuple(diagnostics),
        tuple(duplicate_paths),
    )


def normalize_ssh_public_key(
    value: str,
    *,
    path: DiagnosticPath,
    code: str,
) -> tuple[str | None, Diagnostic | None]:
    """Return one trimmed public key, no key for empty input, or a diagnostic."""
    normalized = value.strip()
    if not normalized:
        return None, None
    key_error = _validate_ssh_public_key(normalized)
    if key_error is not None:
        return None, Diagnostic(path=path, code=code, message=key_error)
    return normalized, None


def _validate_ssh_public_key(value: str) -> str | None:
    if any(character in value for character in _AUTHORIZED_KEYS_FORBIDDEN_CHARACTERS):
        return "must be a single authorized_keys line without newline or NUL characters"

    parts = value.split(maxsplit=2)
    if len(parts) < 2:
        return (
            "must be an OpenSSH public key line: key type, base64 key, optional comment"
        )

    key_type, blob = parts[0], parts[1]
    if key_type not in _OPENSSH_PUBLIC_KEY_TYPES:
        return "must use a supported OpenSSH public key type"

    try:
        decoded = base64.b64decode(blob.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        return "must contain a valid base64 public key blob"

    if base64.b64encode(decoded).decode("ascii") != blob:
        return "must contain a canonical base64 public key blob"

    application = _security_key_application(key_type, decoded)
    if application is not None:
        if not application.startswith(b"ssh:"):
            return "security-key application must start with ssh:"
        if b"\x00" in application[:-1]:
            return "security-key application must not contain embedded NUL"

    try:
        public_key = load_ssh_public_key(f"{key_type} {blob}".encode("ascii"))
    except (ValueError, UnsupportedAlgorithm, NotImplementedError):
        return "must contain a valid supported OpenSSH public key"

    if isinstance(public_key, rsa.RSAPublicKey) and public_key.key_size < 1024:
        return "ssh-rsa public key must be at least 1024 bits"
    return None


def _security_key_application(key_type: str, value: bytes) -> bytes | None:
    field_count = _SECURITY_KEY_APPLICATION_FIELD_COUNTS.get(key_type)
    if field_count is None:
        return None

    field = b""
    offset = 0
    for _ in range(field_count):
        parsed = _read_ssh_string(value, offset)
        if parsed is None:
            return b""
        field, offset = parsed
    return field


def _read_ssh_string(value: bytes, offset: int) -> tuple[bytes, int] | None:
    if len(value) - offset < 4:
        return None
    (length,) = struct.unpack(">I", value[offset : offset + 4])
    start = offset + 4
    end = start + length
    if end > len(value):
        return None
    return value[start:end], end
