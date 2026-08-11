"""Pure Git credential-context parsing and route-selection contracts."""

import pytest

from comfyui_docker_helper.config.git_credentials import (
    GitCredentialContextError,
    canonicalize_git_credential_context,
    has_password_userinfo,
    parse_git_credential_context,
    select_git_credential_context,
)


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("HTTPS://Example.COM/team/", "https://example.com/team"),
        ("https://example.com:443/team", "https://example.com/team"),
        ("http://EXAMPLE.com:80/", "http://example.com/"),
        (
            "https://example.com:8443/team",
            "https://example.com:8443/team",
        ),
        (
            "https://alice@Example.com/team",
            "https://example.com/team",
        ),
        (
            "https://[2001:DB8::1]:443/team",
            "https://[2001:db8::1]/team",
        ),
    ],
)
def test_context_parsing_normalizes_only_authorized_url_components(
    value: str,
    canonical: str,
) -> None:
    parsed = parse_git_credential_context(value)

    assert parsed.canonical_url == canonical
    assert canonicalize_git_credential_context(value) == canonical


def test_context_parsing_preserves_escaped_dot_and_internal_empty_segments() -> None:
    parsed = parse_git_credential_context("https://example.com/a//./%2f/%2F/../b/")

    assert parsed.path_segments == ("a", "", ".", "%2f", "%2F", "..", "b")
    assert parsed.canonical_url == "https://example.com/a//./%2f/%2F/../b"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/team//",
        "https://example.com/team///",
    ],
)
def test_canonical_context_round_trips_trailing_empty_segments(value: str) -> None:
    parsed = parse_git_credential_context(value)
    reparsed = parse_git_credential_context(parsed.canonical_url)

    assert reparsed == parsed
    assert canonicalize_git_credential_context(parsed.canonical_url) == (
        parsed.canonical_url
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/team?",
        "https://example.com/team?query",
        "https://example.com/team#",
        "https://example.com/team#fragment",
    ],
)
def test_context_parsing_rejects_query_and_fragment_delimiters(value: str) -> None:
    with pytest.raises(GitCredentialContextError) as raised:
        parse_git_credential_context(value)

    assert raised.value.code == "query_or_fragment"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://user:token@example.com/team", True),
        ("https://user:@example.com/team", True),
        ("https://:token@example.com/team", True),
        ("https://user@example.com/team", False),
        ("ssh://user:token@example.com/team", False),
        ("not a URL", False),
    ],
)
def test_password_userinfo_detection_is_structural_only(
    value: str,
    expected: bool,
) -> None:
    assert has_password_userinfo(value) is expected


def test_context_parser_rejects_password_userinfo_without_echoing_it() -> None:
    marker = "synthetic-secret-marker"
    with pytest.raises(GitCredentialContextError) as raised:
        parse_git_credential_context(f"https://user:{marker}@example.com/team")

    assert raised.value.code == "password_userinfo"
    assert marker not in str(raised.value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ftp://example.com/team",
        "https:///team",
        "https://example.com:99999/team",
        "https://example.com/bad path",
        "https://example.com\\team",
    ],
)
def test_context_parser_rejects_invalid_context_shapes(value: str) -> None:
    with pytest.raises(GitCredentialContextError) as raised:
        parse_git_credential_context(value)

    assert raised.value.code == "invalid"


def test_route_selection_uses_scheme_host_port_boundary_and_longest_path() -> None:
    root = parse_git_credential_context("https://example.com/")
    team = parse_git_credential_context("https://example.com:443/team/")
    repository = parse_git_credential_context("https://EXAMPLE.com/team/repository")
    request = parse_git_credential_context(
        "https://example.com/team/repository/submodule.git"
    )

    assert select_git_credential_context((root, team, repository), request) == 2
    assert (
        select_git_credential_context(
            (team,), parse_git_credential_context("https://example.com/teamish/repo")
        )
        is None
    )
    assert (
        select_git_credential_context(
            (team,), parse_git_credential_context("http://example.com/team/repo")
        )
        is None
    )
    assert (
        select_git_credential_context(
            (team,),
            parse_git_credential_context("https://example.com:8443/team/repo"),
        )
        is None
    )


def test_route_selection_preserves_path_spelling_and_ignores_url_userinfo() -> None:
    # Normalizing dots or escapes would silently broaden the credential grant.
    route = parse_git_credential_context("https://alice@example.com/a//./%2F")
    same = parse_git_credential_context("https://bob@example.com/a//./%2F/repo")
    changed_escape = parse_git_credential_context(
        "https://bob@example.com/a//./%2f/repo"
    )
    rewritten_dots = parse_git_credential_context("https://example.com/a/%2F/repo")

    assert select_git_credential_context((route,), same) == 0
    assert select_git_credential_context((route,), changed_escape) is None
    assert select_git_credential_context((route,), rewritten_dots) is None
