"""Binary Git credential-helper protocol core contracts."""

import pytest

from comfyui_docker_helper.config.git_credentials import (
    parse_git_credential_context,
)
from comfyui_docker_helper.git_credential_protocol import (
    GitCredentialDecision,
    GitCredentialProtocolError,
    GitCredentialRuntimeRoute,
    evaluate_git_credential_request,
    render_git_credential_response,
)


def _route(match: str, username: bytes) -> GitCredentialRuntimeRoute:
    return GitCredentialRuntimeRoute(
        context=parse_git_credential_context(match),
        username=username,
    )


def test_get_selects_the_longest_route_before_password_resolution() -> None:
    routes = (
        _route("https://example.com/", b"root"),
        _route("https://example.com/team/", b"team"),
        _route("https://example.com/team/repository", b"repository"),
    )

    decision = evaluate_git_credential_request(
        "get",
        b"protocol=https\nhost=EXAMPLE.com:443\npath=team/repository/submodule.git\n\n",
        routes,
    )

    assert decision == GitCredentialDecision(route_index=2, username=b"repository")


def test_username_mismatch_does_not_fall_back_to_a_shorter_route() -> None:
    routes = (
        _route("https://example.com/", b"requested"),
        _route("https://example.com/team", b"other"),
    )

    assert (
        evaluate_git_credential_request(
            "get",
            b"protocol=https\nhost=example.com\npath=team/repo\nusername=requested\n",
            routes,
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"host=example.com\n",
        b"protocol=https\n",
        b"protocol=ftp\nhost=example.com\n",
        b"protocol=https\nhost=\xff.example.com\n",
        b"protocol=https\nhost=example.com\r\n",
        b"protocol=https\nhost=other.example.com\n",
    ],
)
def test_missing_invalid_and_unmatched_contexts_return_no_decision(
    payload: bytes,
) -> None:
    assert (
        evaluate_git_credential_request(
            "get", payload, (_route("https://example.com/", b"user"),)
        )
        is None
    )


def test_unknown_fields_are_not_recorded_and_values_split_at_the_first_equals() -> None:
    route = _route("https://example.com/team=one/repo", b"user")
    payload = (
        b"unknown=\xff=value\nunknown=again\nprotocol=https\n"
        b"host=example.com\npath=team=one/repo\n"
    )

    assert evaluate_git_credential_request("get", payload, (route,)) == (
        GitCredentialDecision(route_index=0, username=b"user")
    )


@pytest.mark.parametrize("key", [b"protocol", b"host", b"path", b"username"])
def test_repeated_known_scalars_fail_without_echoing_content(key: bytes) -> None:
    marker = b"synthetic-marker"
    payload = (
        b"protocol=https\nhost=example.com\npath=team\nusername=user\n"
        + key
        + b"="
        + marker
        + b"\n"
    )

    with pytest.raises(GitCredentialProtocolError) as raised:
        evaluate_git_credential_request("get", payload, ())

    assert raised.value.code == "repeated_scalar"
    assert marker.decode() not in str(raised.value)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"malformed\n", "invalid_line"),
        (b"unknown=before\0after\n", "nul_byte"),
        (b"x=" + b"a" * 65_533 + b"\n", "line_too_long"),
    ],
)
def test_malformed_binary_frames_fail_content_free(
    payload: bytes,
    code: str,
) -> None:
    with pytest.raises(GitCredentialProtocolError) as raised:
        evaluate_git_credential_request("get", payload, ())

    assert raised.value.code == code
    assert payload[:16].decode("ascii", errors="ignore") not in str(raised.value)


def test_complete_input_line_limit_accepts_65535_bytes() -> None:
    route = _route("https://example.com/", b"user")
    payload = b"x=" + b"a" * 65_532 + b"\nprotocol=https\nhost=example.com\n"

    assert evaluate_git_credential_request("get", payload, (route,)) is not None


def test_blank_line_terminates_input_before_later_malformed_content() -> None:
    route = _route("https://example.com/", b"user")

    assert evaluate_git_credential_request(
        "get",
        b"protocol=https\nhost=example.com\n\nmalformed\0content",
        (route,),
    ) == GitCredentialDecision(route_index=0, username=b"user")


@pytest.mark.parametrize("operation", ["store", "erase", "capability", "future"])
def test_non_get_operations_ignore_even_malformed_payload(operation: str) -> None:
    assert evaluate_git_credential_request(operation, b"malformed\0payload", ()) is None


def test_response_preserves_binary_password_equals_and_spaces_exactly() -> None:
    decision = GitCredentialDecision(route_index=0, username=b"user=name")
    password = b"\xff=synthetic password"

    assert render_git_credential_response(decision, password) == (
        b"username=user=name\npassword=\xff=synthetic password\n"
    )


def test_response_value_limit_accepts_65525_bytes() -> None:
    username = b"u" * 65_525
    password = b"p" * 65_525

    response = render_git_credential_response(
        GitCredentialDecision(route_index=0, username=username), password
    )

    assert len(response.splitlines()[0]) + 1 == 65_535
    assert len(response.splitlines()[1]) + 1 == 65_535


@pytest.mark.parametrize(
    ("username", "password"),
    [
        (b"", b"password"),
        (b"user", b""),
        (b"user\0name", b"password"),
        (b"user", b"pass\rword"),
        (b"user\nname", b"password"),
        (b"u" * 65_526, b"password"),
        (b"user", b"p" * 65_526),
    ],
)
def test_response_rejects_invalid_values_without_echoing_them(
    username: bytes,
    password: bytes,
) -> None:
    with pytest.raises(GitCredentialProtocolError) as raised:
        render_git_credential_response(
            GitCredentialDecision(route_index=0, username=username), password
        )

    assert raised.value.code == "invalid_response_value"
    for value in (username, password):
        sample = value[:16].decode("ascii", errors="ignore")
        if sample:
            assert sample not in str(raised.value)
