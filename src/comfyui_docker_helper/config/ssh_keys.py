"""Validation helpers for OpenSSH authorized_keys public key lines."""

import base64
import binascii
import struct

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticPath

_OPENSSH_PUBLIC_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    }
)
_ECDSA_CURVES = {
    "ecdsa-sha2-nistp256": b"nistp256",
    "ecdsa-sha2-nistp384": b"nistp384",
    "ecdsa-sha2-nistp521": b"nistp521",
}
_AUTHORIZED_KEYS_FORBIDDEN_CHARACTERS = frozenset({"\n", "\r", "\x00"})


def normalize_ssh_public_keys(
    values: list[str],
    *,
    path: DiagnosticPath,
    code: str,
) -> tuple[tuple[str, ...], tuple[Diagnostic, ...]]:
    """Return trimmed non-empty public keys plus validation diagnostics."""
    keys: list[str] = []
    diagnostics: list[Diagnostic] = []
    seen: set[str] = set()
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
        if normalized not in seen:
            seen.add(normalized)
            keys.append(normalized)
    return tuple(keys), tuple(diagnostics)


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

    parsed = _read_ssh_string(decoded, 0)
    if parsed is None:
        return "must contain a complete OpenSSH public key blob"
    embedded_key_type, offset = parsed
    if _decode_ascii(embedded_key_type) != key_type:
        return "public key blob type must match the declared key type"
    if key_type == "ssh-ed25519":
        return _validate_ed25519_blob(decoded, offset)
    if key_type == "ssh-rsa":
        return _validate_rsa_blob(decoded, offset)
    if key_type in _ECDSA_CURVES:
        return _validate_ecdsa_blob(key_type, decoded, offset)
    return None


def _validate_ed25519_blob(value: bytes, offset: int) -> str | None:
    parsed = _read_ssh_string(value, offset)
    if parsed is None:
        return "ssh-ed25519 key must include a complete 32-byte public key"
    public_key, offset = parsed
    if len(public_key) != 32:
        return "ssh-ed25519 public key must be exactly 32 bytes"
    if offset != len(value):
        return "public key blob must not contain trailing data"
    return None


def _validate_rsa_blob(value: bytes, offset: int) -> str | None:
    parsed = _read_ssh_string(value, offset)
    if parsed is None:
        return "ssh-rsa key must include a complete exponent"
    exponent, offset = parsed
    parsed = _read_ssh_string(value, offset)
    if parsed is None:
        return "ssh-rsa key must include a complete modulus"
    modulus, offset = parsed
    if not exponent or not modulus:
        return "ssh-rsa exponent and modulus must be non-empty"
    if offset != len(value):
        return "public key blob must not contain trailing data"
    return None


def _validate_ecdsa_blob(key_type: str, value: bytes, offset: int) -> str | None:
    parsed = _read_ssh_string(value, offset)
    if parsed is None:
        return "ECDSA key must include a complete curve name"
    curve, offset = parsed
    if curve != _ECDSA_CURVES[key_type]:
        return "ECDSA curve name must match the declared key type"
    parsed = _read_ssh_string(value, offset)
    if parsed is None:
        return "ECDSA key must include a complete public point"
    public_point, offset = parsed
    if not public_point:
        return "ECDSA public point must be non-empty"
    if offset != len(value):
        return "public key blob must not contain trailing data"
    return None


def _read_ssh_string(value: bytes, offset: int) -> tuple[bytes, int] | None:
    if len(value) - offset < 4:
        return None
    (length,) = struct.unpack(">I", value[offset : offset + 4])
    start = offset + 4
    end = start + length
    if end > len(value):
        return None
    return value[start:end], end


def _decode_ascii(value: bytes) -> str | None:
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        return None
