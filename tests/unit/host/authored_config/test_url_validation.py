"""Shared URL and direct-target validation contracts."""

import pytest

from comfyui_docker_helper.config.validation.runtime_files import (
    validate_relative_file_target,
)


@pytest.mark.parametrize(
    ("authored", "canonical"),
    [
        (".", "."),
        ("models", "models"),
        ("models//checkpoints", "models/checkpoints"),
        ("./models/./checkpoints", "models/checkpoints"),
        ("nested/model.bin", "nested/model.bin"),
        ("models/.WH.model.bin", "models/.WH.model.bin"),
    ],
)
def test_direct_file_target_normalizes_safe_posix_spellings(
    authored: str,
    canonical: str,
) -> None:
    result = validate_relative_file_target(authored)

    assert result.code is None
    assert result.path is not None
    assert result.path.as_posix() == canonical


@pytest.mark.parametrize(
    ("authored", "code"),
    [
        ("", "empty_target"),
        ("/models", "absolute_target"),
        ("..", "parent_target_segment"),
        ("models/../checkpoints", "parent_target_segment"),
        ("models\x00checkpoints", "control_character"),
        (r"models\checkpoints", "backslash"),
        ("models/", "trailing_slash"),
        ("models/.cdh-staging/model.bin", "reserved_target_component"),
        ("models/.wh.model.bin", "reserved_target_component"),
    ],
)
def test_direct_file_target_rejects_unsafe_authored_paths(
    authored: str,
    code: str,
) -> None:
    result = validate_relative_file_target(authored)

    assert result.path is None
    assert result.code == code


def test_exact_file_target_rejects_the_local_directory_root_sentinel() -> None:
    result = validate_relative_file_target(".", allow_root=False)

    assert result.path is None
    assert result.code == "root_target"
