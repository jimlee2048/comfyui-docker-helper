"""Downloader credential route parsing and selection contracts."""

import pytest
from httpx import URL

from comfyui_docker_helper.config.downloader_credentials import (
    DownloaderCredentialContextError,
    canonicalize_downloader_credential_context,
    downloader_httpx_request_context,
    parse_downloader_credential_context,
    parse_downloader_request_url,
    select_downloader_credential_context,
)


def test_authored_route_canonicalizes_origin_and_path_without_decoding() -> None:
    assert (
        canonicalize_downloader_credential_context(
            "HTTPS://EXAMPLE.com:443/models/%2F/"
        )
        == "https://example.com/models/%2F"
    )
    assert (
        canonicalize_downloader_credential_context("http://[2001:DB8::1]:8080/")
        == "http://[2001:db8::1]:8080/"
    )


@pytest.mark.parametrize(
    ("value", "code"),
    [
        pytest.param("ftp://example.com/models", "invalid", id="scheme"),
        pytest.param("https:///models", "invalid", id="missing-host"),
        pytest.param("https://example.com\\@attacker.test/a", "invalid", id="slash"),
        pytest.param("https://user@example.com/a", "userinfo", id="userinfo"),
        pytest.param(
            "https://example.com/a?scope=private",
            "query_or_fragment",
            id="query",
        ),
        pytest.param(
            "https://example.com/a#fragment",
            "query_or_fragment",
            id="fragment",
        ),
        pytest.param("https://example.com/a\n", "invalid", id="control"),
        pytest.param("https://́.example/a", "invalid", id="invalid-idna"),
    ],
)
def test_authored_route_rejects_unsupported_url_shapes(
    value: str,
    code: str,
) -> None:
    with pytest.raises(DownloaderCredentialContextError) as raised:
        parse_downloader_credential_context(value)

    assert raised.value.code == code


def test_request_parser_maps_invalid_idna_to_stable_context_error() -> None:
    with pytest.raises(DownloaderCredentialContextError) as raised:
        parse_downloader_request_url("https://́.example/file")

    assert raised.value.code == "invalid"


def test_httpx_context_excludes_query_without_decoding_raw_path() -> None:
    url = URL("https://example.com/models/%2F/file?download=true")

    request = downloader_httpx_request_context(
        scheme=url.scheme,
        host=url.host,
        port=url.port,
        raw_path=url.raw_path,
        query=url.query,
    )

    encoded = parse_downloader_credential_context("https://example.com/models/%2F")
    changed_escape = parse_downloader_credential_context(
        "https://example.com/models/%2f"
    )
    assert select_downloader_credential_context((encoded,), request) == 0
    assert select_downloader_credential_context((changed_escape,), request) is None


def test_authored_and_request_contexts_share_httpx_url_normalization() -> None:
    route = parse_downloader_credential_context(
        "https://BÜCHER.example/café/../models/"
    )
    request = parse_downloader_request_url(
        "https://xn--bcher-kva.example/models/file?download=true"
    )

    assert route.canonical_url == "https://bücher.example/models"
    assert select_downloader_credential_context((route,), request) == 0

    unicode_route = parse_downloader_credential_context("https://example.test/模型/")
    encoded_request = parse_downloader_request_url(
        "https://example.test/%E6%A8%A1%E5%9E%8B/file"
    )
    assert unicode_route.canonical_url == "https://example.test/%E6%A8%A1%E5%9E%8B"
    assert select_downloader_credential_context((unicode_route,), encoded_request) == 0


def test_longest_segment_prefix_is_scheme_origin_and_path_exact() -> None:
    routes = tuple(
        parse_downloader_credential_context(value)
        for value in (
            "https://example.com/",
            "https://example.com/models",
            "https://example.com/models/private",
            "http://example.com/models/private",
        )
    )

    assert (
        select_downloader_credential_context(
            routes,
            parse_downloader_request_url(
                "https://example.com/models/private/file?download=true"
            ),
        )
        == 2
    )
    assert (
        select_downloader_credential_context(
            routes,
            parse_downloader_request_url("https://example.com/modelshop/file"),
        )
        == 0
    )
    assert (
        select_downloader_credential_context(
            routes[:3],
            parse_downloader_request_url("http://example.com/models/private/file"),
        )
        is None
    )
