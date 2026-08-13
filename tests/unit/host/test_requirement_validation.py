"""Standards-derived admission for user-authored Python requirements."""

import pytest

from comfyui_docker_helper.config.requirement_validation import (
    DirectRequirementError,
    direct_requirement_is_active,
    direct_selector_is_exact,
    parse_direct_requirement,
)


def test_requirement_identity_canonicalizes_standard_components() -> None:
    identity = parse_direct_requirement(
        'Demo[Z_Extra,Foo_Bar]>=1rc1,<2; python_version < "3.14"'
    )

    assert identity.name == "demo"
    assert identity.extras == ("foo-bar", "z-extra")
    assert identity.specifier == "<2,>=1rc1"
    assert identity.direct_reference is None
    assert identity.marker == 'python_version < "3.14"'
    assert identity.resolver_requirement == "demo[foo-bar,z-extra]<2,>=1rc1"
    assert (
        identity.canonical_value
        == 'demo[foo-bar,z-extra]<2,>=1rc1; python_version < "3.14"'
    )


@pytest.mark.parametrize(
    ("requirement", "specifier"),
    [
        ("demo==1.*", "==1.*"),
        ("demo===legacy", "===legacy"),
        ("demo>=1rc1,<2", "<2,>=1rc1"),
        ("demo==1.dev1", "==1.dev1"),
        ("demo==1+cu130", "==1+cu130"),
        ("demo==1,!=1", "!=1,==1"),
        ("demo==1,==2", "==1,==2"),
    ],
)
def test_requirement_identity_accepts_representative_packaging_selectors(
    requirement: str,
    specifier: str,
) -> None:
    identity = parse_direct_requirement(requirement)

    assert identity.specifier == specifier
    assert identity.direct_reference is None


# Standard requirement syntax is followed by the narrower executable transport boundary.
@pytest.mark.parametrize(
    "direct_reference",
    [
        "https://example.com/demo.whl",
        "http://example.com/demo.tar.gz",
        "git+https://example.com/demo.git@main#subdirectory=package",
        "git+http://example.com/demo.git@main",
    ],
)
def test_requirement_identity_accepts_supported_remote_direct_references(
    direct_reference: str,
) -> None:
    identity = parse_direct_requirement(f"Demo[CLI] @ {direct_reference}")

    assert identity.name == "demo"
    assert identity.extras == ("cli",)
    assert identity.specifier == ""
    assert identity.direct_reference == direct_reference
    assert identity.marker is None
    assert identity.resolver_requirement == f"demo[cli] @ {direct_reference}"


def test_direct_reference_preserves_opaque_url_and_canonical_marker() -> None:
    direct_reference = "https://EXAMPLE.com:443/a%2Fb.whl?name=Token#sha256=ABCDEF"
    identity = parse_direct_requirement(
        f'Demo @ {direct_reference} ; python_version >= "3.12"'
    )

    assert identity.direct_reference == direct_reference
    assert (
        identity.canonical_value
        == f'demo @ {direct_reference} ; python_version >= "3.12"'
    )


@pytest.mark.parametrize(
    "requirement",
    [
        "demo @ file:///tmp/demo.whl",
        "demo @ ./demo",
        "demo @ git+file:///tmp/demo",
        "demo @ git+ssh://example.com/demo.git",
        "demo @ ftp://example.com/demo.tar.gz",
        "demo @ https:///demo.whl",
        "demo @ https://example.com:invalid/demo.whl",
        "demo @ https://user@example.com/demo.whl",
        "demo @ https://:token@example.com/demo.whl",
    ],
)
def test_requirement_identity_rejects_unsupported_direct_references(
    requirement: str,
) -> None:
    with pytest.raises(DirectRequirementError) as raised:
        parse_direct_requirement(requirement)

    assert raised.value.code == "python.unsupported_direct_reference"
    assert str(raised.value) == "must use a supported public remote direct reference"


@pytest.mark.parametrize(
    "requirement",
    [
        "",
        " demo==1",
        "demo==1 ",
        "demo\n==1",
        "https://example.com/demo.whl",
        "-e demo",
        "-r requirements.txt",
        "--index-url https://example.com/simple",
    ],
)
def test_requirement_identity_rejects_non_requirement_inputs(requirement: str) -> None:
    with pytest.raises(DirectRequirementError) as raised:
        parse_direct_requirement(requirement)

    assert raised.value.code == "python.invalid_requirement"


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("==1", True),
        ("==1,>=1", True),
        ("===legacy", True),
        ("!=1,==1", True),
        ("==1.*", False),
        ("==1,==2", False),
        (">=1", False),
        ("", False),
    ],
)
def test_exact_selector_classification_uses_standard_specifiers(
    selector: str,
    expected: bool,
) -> None:
    assert direct_selector_is_exact(selector) is expected


def test_requirement_marker_evaluation_uses_requirement_context() -> None:
    identity = parse_direct_requirement('demo; python_version == "3.13"')

    assert direct_requirement_is_active(
        identity,
        {
            "python_version": "3.13",
        },
    )


@pytest.mark.parametrize(
    "marker",
    [
        'extra == "gpu"',
        '"gpu" in extras',
        '"gpu" in dependency_groups',
    ],
)
def test_requirement_marker_rejects_undefined_containing_context(marker: str) -> None:
    identity = parse_direct_requirement(f"demo; {marker}")

    with pytest.raises(DirectRequirementError) as raised:
        direct_requirement_is_active(identity, {"python_version": "3.13"})

    assert raised.value.code == "python.unsupported_marker_context"


def test_requirement_marker_rejects_undefined_standard_comparison() -> None:
    identity = parse_direct_requirement('demo; os_name ~= "posix"')

    with pytest.raises(DirectRequirementError) as raised:
        direct_requirement_is_active(identity, {"os_name": "posix"})

    assert raised.value.code == "python.invalid_environment_marker"
