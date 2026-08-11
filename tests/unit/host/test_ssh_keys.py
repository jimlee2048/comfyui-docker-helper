"""OpenSSH public-key parsing and normalization contracts."""

import base64
import struct

import pytest

from comfyui_docker_helper.config.ssh_keys import normalize_ssh_public_key


def _ssh_string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _key_line(
    key_type: str,
    *fields: bytes,
    embedded_key_type: str | None = None,
    comment: str = "test@example",
) -> str:
    blob = _ssh_string((embedded_key_type or key_type).encode("ascii")) + b"".join(
        _ssh_string(field) for field in fields
    )
    return f"{key_type} {base64.b64encode(blob).decode('ascii')} {comment}"


_RSA_EXPONENT = bytes.fromhex("010001")
_RSA_MODULUS = bytes.fromhex(
    "00c5c3b5c8cd904126f96e6c6c54c3ed43d9e1ccc1530a3ef7fa40b935b2fb3e"
    "5feeed3d69e7b523ff0b897eb780c3d846e2b31e8bcbedf750304a19dcf24c072"
    "85ed93c08d110f1a63c4be2ec688950856b14e943a95120bb536ba23c6a621081"
    "fd4f610c2d410d4e4c0da980869d9b21f1287a43fc7130ead2460953504984e1"
)
_P256_GENERATOR = bytes.fromhex(
    "04"
    "6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296"
    "4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5"
)
_P384_GENERATOR = bytes.fromhex(
    "04"
    "aa87ca22be8b05378eb1c71ef320ad746e1d3b628ba79b9859f741e082542a38"
    "5502f25dbf55296c3a545e3872760ab7"
    "3617de4a96262c6f5d9e98bf9292dc29f8f41dbd289a147ce9da3113b5f0b8c"
    "00a60b1ce1d7e819d7a431d7c90ea0e5f"
)
_P521_GENERATOR = bytes.fromhex(
    "04"
    "00c6858e06b70404e9cd9e3ecb662395b4429c648139053fb521f828af606b4d"
    "3dbaa14b5e77efe75928fe1dc127a2ffa8de3348b3c1856a429bf97e7e31c2e5"
    "bd66"
    "011839296a789a3bc0045c8a5fb42c7d1bd998f54449579b446817afbd17273e"
    "662c97ee72995ef42640c550b9013fad0761353c7086a272c24088be94769fd1"
    "6650"
)
_ED25519_PUBLIC_KEY = bytes(range(32))


# Fixed fixtures prove every admitted wire type reaches the shared loader.
@pytest.mark.parametrize(
    "key",
    [
        _key_line("ssh-rsa", _RSA_EXPONENT, _RSA_MODULUS),
        _key_line("ssh-ed25519", _ED25519_PUBLIC_KEY),
        _key_line("ecdsa-sha2-nistp256", b"nistp256", _P256_GENERATOR),
        _key_line("ecdsa-sha2-nistp384", b"nistp384", _P384_GENERATOR),
        _key_line("ecdsa-sha2-nistp521", b"nistp521", _P521_GENERATOR),
        _key_line(
            "sk-ssh-ed25519@openssh.com",
            _ED25519_PUBLIC_KEY,
            b"ssh:cdh",
        ),
        _key_line(
            "sk-ecdsa-sha2-nistp256@openssh.com",
            b"nistp256",
            _P256_GENERATOR,
            b"ssh:\x00",
        ),
    ],
    ids=("rsa", "ed25519", "p256", "p384", "p521", "ed25519-sk", "ecdsa-sk"),
)
def test_supported_public_key_formats_are_accepted(key: str) -> None:
    normalized, diagnostic = normalize_ssh_public_key(
        key,
        path=("system", "ssh", "pub_keys", 0),
        code="ssh.invalid_public_key",
    )

    assert normalized == key
    assert diagnostic is None


# These malformed blobs exercise CDH policy and the dependency-owned key checks.
@pytest.mark.parametrize(
    "key",
    [
        _key_line("ssh-rsa", b"\x02", _RSA_MODULUS),
        _key_line(
            "ssh-rsa",
            _RSA_EXPONENT,
            b"\x00\x80" + (b"\x00" * 62) + b"\x01",
        ),
        _key_line("ssh-ed25519", bytes(range(31))),
        _key_line("ecdsa-sha2-nistp256", b"nistp256", b"x"),
        _key_line("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + (b"\x00" * 64)),
        _key_line(
            "ecdsa-sha2-nistp256",
            b"nistp256",
            b"\x03" + _P256_GENERATOR[1:33],
        ),
        _key_line(
            "sk-ssh-ed25519@openssh.com",
            _ED25519_PUBLIC_KEY,
            b"ssh:",
            embedded_key_type="ssh-ed25519",
        ),
        _key_line(
            "sk-ecdsa-sha2-nistp256@openssh.com",
            b"nistp384",
            _P256_GENERATOR,
            b"ssh:",
        ),
        _key_line(
            "sk-ecdsa-sha2-nistp256@openssh.com",
            b"nistp256",
            _P256_GENERATOR,
            b"ssh:",
            b"trailing",
        ),
        _key_line("sk-ssh-ed25519@openssh.com", _ED25519_PUBLIC_KEY),
        _key_line(
            "sk-ssh-ed25519@openssh.com",
            _ED25519_PUBLIC_KEY,
            b"example:cdh",
        ),
        _key_line(
            "sk-ssh-ed25519@openssh.com",
            _ED25519_PUBLIC_KEY,
            b"ssh:\x00unexpected",
        ),
    ],
    ids=(
        "invalid-rsa-exponent",
        "undersized-rsa",
        "invalid-ed25519-length",
        "invalid-ecdsa-encoding",
        "off-curve-ecdsa-point",
        "compressed-ecdsa-point",
        "embedded-type-mismatch",
        "wrong-sk-curve",
        "trailing-field",
        "missing-sk-application",
        "invalid-sk-application-prefix",
        "embedded-sk-application-nul",
    ),
)
def test_malformed_public_key_blobs_are_rejected(key: str) -> None:
    normalized, diagnostic = normalize_ssh_public_key(
        key,
        path=("system", "ssh", "pub_keys", 0),
        code="ssh.invalid_public_key",
    )

    assert normalized is None
    assert diagnostic is not None


def test_noncanonical_base64_is_rejected_before_key_loading() -> None:
    normalized, diagnostic = normalize_ssh_public_key(
        "ssh-ed25519 AB== test@example",
        path=("system", "ssh", "pub_keys", 0),
        code="ssh.invalid_public_key",
    )

    assert normalized is None
    assert diagnostic is not None
    assert diagnostic.message == "must contain a canonical base64 public key blob"


@pytest.mark.parametrize(
    "key",
    [
        "restrict "
        + _key_line("sk-ssh-ed25519@openssh.com", _ED25519_PUBLIC_KEY, b"ssh:"),
        _key_line("ssh-ed25519-cert-v01@openssh.com", _ED25519_PUBLIC_KEY),
        _key_line("ssh-dss", b"not", b"supported"),
        "ssh-ed25519 invalid! test@example",
    ],
    ids=("options", "certificate", "dsa", "invalid-base64"),
)
def test_non_public_key_line_syntax_is_rejected(key: str) -> None:
    normalized, diagnostic = normalize_ssh_public_key(
        key,
        path=("system", "ssh", "pub_keys", 0),
        code="ssh.invalid_public_key",
    )

    assert normalized is None
    assert diagnostic is not None


def test_loader_failure_diagnostic_does_not_expose_key_or_comment() -> None:
    key = _key_line(
        "sk-ecdsa-sha2-nistp256@openssh.com",
        b"nistp256",
        b"x",
        b"ssh:sensitive-application",
        comment="sensitive-comment",
    )
    blob = key.split()[1]

    normalized, diagnostic = normalize_ssh_public_key(
        key,
        path=("system", "ssh", "pub_keys", 0),
        code="ssh.invalid_public_key",
    )

    assert normalized is None
    assert diagnostic is not None
    assert blob not in diagnostic.message
    assert "sensitive-application" not in diagnostic.message
    assert "sensitive-comment" not in diagnostic.message
