"""Checksum schema and single-authority projection tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.unit.test_build_plan import (
    accepted_resolution,
    build_plan,
    canonical_wheel,
    final_config,
    request_graph,
)

from comfyui_docker_helper.config.build_plan import BuildPlan, dump_build_plan_json
from comfyui_docker_helper.config.canonical_lock import dump_canonical_lock_toml
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.rendering.final_materializer import materialize_build_plan
from comfyui_docker_helper.rendering.final_renderer import render_build_plan_dockerfile

UPPER_CHECKSUM = f"sha256:{'AB' * 32}"
CANONICAL_CHECKSUM = UPPER_CHECKSUM.lower()


def _config_with_checksum(value: object) -> FinalConfig:
    document = final_config().model_dump(mode="python")
    document["files"][0]["checksum"] = value
    return FinalConfig.model_validate(document)


# Public validation accepts only exact SHA-256 syntax and canonicalizes hex case.
def test_public_checksum_normalizes_hex_case() -> None:
    config = _config_with_checksum(UPPER_CHECKSUM)

    assert config.files[0].checksum == CANONICAL_CHECKSUM


@pytest.mark.parametrize(
    "value",
    [
        "",
        " sha256:" + "a" * 64,
        "sha256:" + "a" * 64 + " ",
        "sha256:aa aa",
        "SHA256:" + "a" * 64,
        "sha-256:" + "a" * 64,
        "sha256::" + "a" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:" + "g" * 64,
        123,
    ],
)
def test_public_checksum_rejects_noncanonical_domain_input(value: object) -> None:
    with pytest.raises(ValidationError):
        _config_with_checksum(value)


def test_checksum_projects_to_request_and_build_plan_but_not_lock() -> None:
    config = _config_with_checksum(UPPER_CHECKSUM)
    resolution = accepted_resolution()
    graph = request_graph(config, resolution)
    plan = build_plan(config, resolution)

    assert graph.files[0].checksum == CANONICAL_CHECKSUM
    assert plan.files.files[0].checksum == CANONICAL_CHECKSUM
    assert CANONICAL_CHECKSUM.encode() in dump_build_plan_json(plan)
    assert "checksum" not in dump_canonical_lock_toml(resolution.lock)


def test_build_plan_admission_requires_canonical_lowercase_checksum() -> None:
    plan = build_plan(_config_with_checksum(UPPER_CHECKSUM), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["files"]["files"][0]["checksum"] = UPPER_CHECKSUM

    with pytest.raises(ValidationError, match="canonical"):
        BuildPlan.model_validate(document)


def test_build_plan_admission_rejects_reserved_staging_final_leaf() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["files"]["files"][0]["target"] = "/workspace/ComfyUI/models/.cdh-staging"

    with pytest.raises(ValidationError, match="reserved staging filename"):
        BuildPlan.model_validate(document)


def test_materialization_projects_checksum_to_runtime_and_real_build_consumer(
    tmp_path: Path,
) -> None:
    plan = build_plan(_config_with_checksum(UPPER_CHECKSUM), accepted_resolution())
    output = tmp_path / "context"
    output.mkdir()

    materialize_build_plan(plan, output, canonical_wheel=canonical_wheel())

    runtime = tomllib.loads((output / "runtime/config.toml").read_text())
    assert runtime["files"][0]["checksum"] == CANONICAL_CHECKSUM
    dockerfile = (output / "Dockerfile").read_text()
    assert dockerfile.count("container download-files") == 1
    assert dockerfile.index("container install-custom-nodes") < dockerfile.index(
        "container download-files"
    )


def test_renderer_omits_build_download_command_when_no_files() -> None:
    document = final_config().model_dump(mode="python")
    document["files"] = []
    plan = build_plan(FinalConfig.model_validate(document), accepted_resolution())

    assert "container download-files" not in render_build_plan_dockerfile(plan)


def test_readme_documents_trusted_digest() -> None:
    root = Path("README.md").read_text()

    assert 'checksum = "sha256:<64 hexadecimal digits>"' in root
    assert "does not fetch or infer a digest" in root
    assert "download_failure_policy` applies only at runtime" in root
