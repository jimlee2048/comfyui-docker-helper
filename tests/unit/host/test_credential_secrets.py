"""Credential Secret value and stable-ID contracts."""

import pytest

from comfyui_docker_helper.config.credential_secrets import (
    CREDENTIAL_SECRET_MAX_BYTES,
    BearerTokenError,
    downloader_credential_secret_id,
    downloader_credential_secret_target,
    validate_bearer_token,
)


@pytest.mark.parametrize(
    "value",
    [
        b"hf_example-token",
        b"abc.DEF_123~+/==",
    ],
    ids=("provider-shape", "complete-alphabet"),
)
def test_bearer_token_admits_rfc6750_b64token(value: bytes) -> None:
    validate_bearer_token(value)


def test_bearer_token_admits_the_acquisition_limit() -> None:
    validate_bearer_token(b"x" * CREDENTIAL_SECRET_MAX_BYTES)


@pytest.mark.parametrize(
    ("value", "marker"),
    [
        (b"", None),
        (b"sensitive-space-marker value", "sensitive-space-marker"),
        (b"sensitive-tab-marker\tvalue", "sensitive-tab-marker"),
        (b"sensitive-padding-marker=inside", "sensitive-padding-marker"),
        (b"sensitive-line-marker\nvalue", "sensitive-line-marker"),
        (b"sensitive-nonascii-marker\xff", "sensitive-nonascii-marker"),
    ],
    ids=("empty", "space", "tab", "interior-padding", "lf", "non-ascii"),
)
def test_bearer_token_rejects_invalid_content_without_echo(
    value: bytes,
    marker: str | None,
) -> None:
    with pytest.raises(BearerTokenError) as raised:
        validate_bearer_token(value)

    if marker is not None:
        assert marker not in str(raised.value)


def test_bearer_token_rejects_the_next_byte_without_echo() -> None:
    marker = b"oversized-sensitive-marker"
    value = marker + b"x" * (CREDENTIAL_SECRET_MAX_BYTES + 1 - len(marker))

    with pytest.raises(BearerTokenError) as raised:
        validate_bearer_token(value)

    assert marker.decode() not in str(raised.value)


def test_downloader_secret_id_and_target_are_consumer_isolated() -> None:
    secret_id = downloader_credential_secret_id("hf_read")

    assert secret_id == "cdh-downloader-credential-hf_read"
    assert downloader_credential_secret_target(secret_id) == (
        "/run/secrets/cdh-downloader-credential-hf_read"
    )


def test_downloader_secret_id_rejects_path_injection() -> None:
    with pytest.raises(ValueError, match="must be canonical"):
        downloader_credential_secret_id("token/slash")


def test_downloader_secret_target_rejects_other_consumer_id() -> None:
    with pytest.raises(ValueError, match="must be canonical"):
        downloader_credential_secret_target("cdh-git-credential-hf_read")
