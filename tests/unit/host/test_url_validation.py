"""Shared URL and runtime-target validation contracts."""

import pytest

from comfyui_docker_helper.config.url_validation import (
    validate_relative_file_directory,
)


@pytest.mark.parametrize(
    ("authored", "canonical"),
    [
        (".", "."),
        ("./", "."),
        ("models/", "models"),
        ("models//checkpoints", "models/checkpoints"),
        ("./models/./checkpoints/", "models/checkpoints"),
        (r"models\checkpoints", r"models\checkpoints"),
    ],
)
def test_relative_file_directory_normalizes_safe_posix_spellings(
    authored: str,
    canonical: str,
) -> None:
    result = validate_relative_file_directory(authored)

    assert result.code is None
    assert result.path is not None
    assert result.path.as_posix() == canonical


@pytest.mark.parametrize(
    ("authored", "code"),
    [
        ("", "empty_directory"),
        ("/models", "absolute_directory"),
        ("..", "parent_directory_segment"),
        ("models/../checkpoints", "parent_directory_segment"),
        ("models\x00checkpoints", "control_character"),
    ],
)
def test_relative_file_directory_rejects_unsafe_authored_paths(
    authored: str,
    code: str,
) -> None:
    result = validate_relative_file_directory(authored)

    assert result.path is None
    assert result.code == code
