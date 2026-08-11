"""Publication tag syntax and resolution contracts."""

import pytest

from comfyui_docker_helper.config.publication_tags import (
    PublicationTagError,
    resolve_publication_tags,
    validate_publication_tags,
)

_COMMIT = "09725967cf76304371c390ca1d6483e04061da48"


@pytest.mark.parametrize(
    "value",
    [
        "busybox",
        "docker.io/library/busybox:latest",
        "REGISTRY.example:5000/team/image:Release_1",
        "localhost:5000/team/image:x",
        "[2001:db8::1]:5000/team/image:x",
        "example/image:${{ comfyui.release }}",
        "example/image:${{ comfyui.commit }}",
        "example/image:custom-${{ comfyui.commit.prefix(12) }}",
        "example/image:custom-${{ comfyui.commit.prefix(40) }}",
        pytest.param(f"example/image:{'x' * 128}", id="max-tag-length"),
        pytest.param("a" * 247, id="max-reference-length"),
        pytest.param(
            f"registry.example/{'a' * 255}",
            id="max-path-component-length",
        ),
        "Example/image:tag",
    ],
)
def test_validation_accepts_distribution_references_and_closed_expressions(
    value: str,
) -> None:
    assert validate_publication_tags([value]) == ()


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("", "build.invalid_image_reference"),
        ("example/Image:tag", "build.invalid_image_reference"),
        ("example//image:tag", "build.invalid_image_reference"),
        ("example/image:", "build.invalid_image_reference"),
        ("example/image:bad tag", "build.invalid_image_reference"),
        pytest.param(
            f"example/image:{'x' * 129}",
            "build.invalid_image_reference",
            id="tag-too-long",
        ),
        pytest.param(
            "a" * 248,
            "build.invalid_image_reference",
            id="reference-too-long",
        ),
        pytest.param(
            f"registry.example/{'a' * 256}",
            "build.invalid_image_reference",
            id="path-component-too-long",
        ),
        ("a" * 64, "build.invalid_image_reference"),
        ("registry.example:/image:tag", "build.invalid_image_reference"),
        ("[not-ipv6]/image:tag", "build.invalid_image_reference"),
        ("[::ffff:192.0.2.1]/image:tag", "build.invalid_image_reference"),
        ("example/image@sha256:abcd", "build.digest_target_forbidden"),
        ("example/${{ comfyui.commit }}:tag", "build.invalid_tag_expression"),
        ("example/image-${{ comfyui.commit }}", "build.invalid_tag_expression"),
        ("example/image:${{comfyui.commit}}", "build.invalid_tag_expression"),
        ("example/image:${{  comfyui.commit }}", "build.invalid_tag_expression"),
        ("example/image:${{ comfyui.unknown }}", "build.invalid_tag_expression"),
        (
            "example/image:${{ comfyui.commit.prefix(11) }}",
            "build.invalid_tag_expression",
        ),
        (
            "example/image:${{ comfyui.commit.prefix(41) }}",
            "build.invalid_tag_expression",
        ),
        (
            "example/image:${{ comfyui.commit.prefix(012) }}",
            "build.invalid_tag_expression",
        ),
        (
            "example/image:${{ comfyui.commit.prefix(x) }}",
            "build.invalid_tag_expression",
        ),
        (
            "example/image:${{ comfyui.commit.prefix(12) }",
            "build.invalid_tag_expression",
        ),
    ],
)
def test_validation_rejects_invalid_references_and_expression_spellings(
    value: str,
    code: str,
) -> None:
    issues = validate_publication_tags([value])

    assert [(issue.index, issue.code) for issue in issues] == [(0, code)]


@pytest.mark.parametrize(
    "values",
    [
        ["busybox:x", "docker.io/library/busybox:x"],
        ["index.docker.io/library/busybox:x", "DOCKER.IO/library/busybox:x"],
        ["EXAMPLE.COM/image:x", "example.com/image:x"],
        ["busybox", "index.docker.io/library/busybox:latest"],
    ],
)
def test_validation_uses_familiar_normalization_only_for_duplicates(
    values: list[str],
) -> None:
    issues = validate_publication_tags(values)

    assert [(issue.index, issue.code) for issue in issues] == [
        (1, "build.duplicate_tag")
    ]


def test_duplicate_normalization_does_not_rewrite_ports_or_registry_one() -> None:
    assert (
        validate_publication_tags(
            [
                "registry.example/team/image:x",
                "registry.example:443/team/image:x",
                "docker.io/library/busybox:x",
                "registry-1.docker.io/library/busybox:x",
            ]
        )
        == ()
    )


def test_validation_reports_each_invalid_index_without_hiding_later_values() -> None:
    issues = validate_publication_tags(
        ["bad tag", "example/image:x", "example/image:x", "upper/Path:x"]
    )

    assert [(issue.index, issue.code) for issue in issues] == [
        (0, "build.invalid_image_reference"),
        (2, "build.duplicate_tag"),
        (3, "build.invalid_image_reference"),
    ]


def test_resolution_expands_exact_identity_and_preserves_input_spelling() -> None:
    values = [
        "REGISTRY.example:5000/team/image:v${{ comfyui.release }}",
        "example/image:${{ comfyui.commit }}",
        "example/image:custom-${{ comfyui.commit.prefix(12) }}",
    ]

    assert resolve_publication_tags(
        values, commit=_COMMIT, formal_release="0.30.2"
    ) == (
        "REGISTRY.example:5000/team/image:v0.30.2",
        f"example/image:{_COMMIT}",
        "example/image:custom-09725967cf76",
    )


def test_resolution_requires_a_formal_release_for_each_release_expression() -> None:
    with pytest.raises(PublicationTagError) as raised:
        resolve_publication_tags(
            ["example/image:latest", "example/image:v${{ comfyui.release }}"],
            commit=_COMMIT,
            formal_release=None,
        )

    assert [(issue.index, issue.code) for issue in raised.value.issues] == [
        (1, "build.release_unavailable")
    ]


def test_resolution_revalidates_expansion_and_normalized_duplicates() -> None:
    with pytest.raises(PublicationTagError) as raised:
        resolve_publication_tags(
            ["busybox:0.30.2", "docker.io/library/busybox:${{ comfyui.release }}"],
            commit=_COMMIT,
            formal_release="0.30.2",
        )

    assert [(issue.index, issue.code) for issue in raised.value.issues] == [
        (1, "build.duplicate_tag")
    ]

    with pytest.raises(PublicationTagError) as raised:
        resolve_publication_tags(
            ["example/image:x${{ comfyui.release }}"],
            commit=_COMMIT,
            formal_release="1." + "2" * 128,
        )

    assert [(issue.index, issue.code) for issue in raised.value.issues] == [
        (0, "build.invalid_image_reference")
    ]


def test_resolution_requires_full_lowercase_commit_only_when_referenced() -> None:
    assert resolve_publication_tags(
        ["example/image:literal"], commit="unused", formal_release=None
    ) == ("example/image:literal",)

    with pytest.raises(PublicationTagError) as raised:
        resolve_publication_tags(
            ["example/image:${{ comfyui.commit.prefix(12) }}"],
            commit=_COMMIT.upper(),
            formal_release=None,
        )

    assert [(issue.index, issue.code) for issue in raised.value.issues] == [
        (0, "build.invalid_comfyui_commit")
    ]
