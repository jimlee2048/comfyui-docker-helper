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
        b"x" * CREDENTIAL_SECRET_MAX_BYTES,
    ],
    ids=("provider-shape", "complete-alphabet", "acquisition-limit"),
)
def test_bearer_token_admits_rfc6750_b64token(value: bytes) -> None:
    validate_bearer_token(value)


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"has space",
        b"has\t tab",
        b"padding=inside",
        b"line\nfeed",
        b"non-ascii-\xff",
        b"x" * (CREDENTIAL_SECRET_MAX_BYTES + 1),
    ],
    ids=("empty", "space", "tab", "interior-padding", "lf", "non-ascii", "oversized"),
)
def test_bearer_token_rejects_invalid_content_without_echo(value: bytes) -> None:
    marker = b"has space"
    with pytest.raises(BearerTokenError) as raised:
        validate_bearer_token(value)

    assert marker.decode() not in str(raised.value)


def test_downloader_secret_id_and_target_are_consumer_isolated() -> None:
    secret_id = downloader_credential_secret_id("hf_read")

    assert secret_id == "cdh-downloader-credential-hf_read"
    assert downloader_credential_secret_target(secret_id) == (
        "/run/secrets/cdh-downloader-credential-hf_read"
    )


@pytest.mark.parametrize(
    "value",
    ["HF_READ", "1token", "token.dot", "token/slash", "x" * 65],
)
def test_downloader_secret_id_rejects_noncanonical_names(value: str) -> None:
    with pytest.raises(ValueError, match="must be canonical"):
        downloader_credential_secret_id(value)


def test_downloader_secret_target_rejects_other_consumer_id() -> None:
    with pytest.raises(ValueError, match="must be canonical"):
        downloader_credential_secret_target("cdh-git-credential-hf_read")
