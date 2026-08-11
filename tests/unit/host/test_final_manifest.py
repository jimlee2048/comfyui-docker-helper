"""Final manifest schema, binding, omission, and renderer-order contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config.build_plan import BuildPlan
from comfyui_docker_helper.config.final_manifest import (
    FinalManifest,
    InventoryDistribution,
    SetuptoolsEvidence,
    VersionEvidence,
    dump_final_manifest,
    parse_final_manifest,
)
from comfyui_docker_helper.rendering.final_renderer import (
    render_build_plan_dockerfile,
)
from tests.build_plan_support import accepted_resolution, build_plan, final_config
from tests.final_manifest_support import manifest_for_plan


# Canonical bytes retain exact local versions without promoting observations.
def test_manifest_round_trip_is_canonical_observational_and_strict() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    manifest = manifest_for_plan(plan)

    content = dump_final_manifest(manifest)

    assert content.endswith(b"\n") and content.count(b"\n") == 1
    assert parse_final_manifest(content) == manifest
    assert dump_final_manifest(parse_final_manifest(content)) == content
    assert b'"observed":"2.11.0+cu130"' in content
    assert b"timestamp" not in content
    assert b"/home/" not in content

    document = json.loads(content)
    document["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FinalManifest.model_validate(document)


# Disabled comfy-cli omits the capability instead of inventing an absent version.
def test_manifest_omits_disabled_comfy_cli_capability() -> None:
    plan = build_plan(
        final_config(install_cli=False),
        accepted_resolution(install_cli=False),
    )

    content = dump_final_manifest(manifest_for_plan(plan))

    assert b'"comfy_cli"' not in content
    assert json.loads(content)["application"]["manager"]["enabled"] is True


# Schema validators reject identity drift and a false setuptools compatibility claim.
def test_manifest_rejects_intended_observed_identity_mismatch() -> None:
    with pytest.raises(ValidationError, match="intended and observed versions"):
        VersionEvidence(intended="2.12.1+cu130", observed="2.12.1+cpu")

    with pytest.raises(ValidationError, match="does not satisfy compatibility"):
        SetuptoolsEvidence(compatibility="<82", observed="82.0.0")


@pytest.mark.parametrize("version", ["1.0rc1", "1.0.dev1", "1.0+cuda"])
def test_factual_inventory_accepts_canonical_complete_pep440(version: str) -> None:
    evidence = InventoryDistribution(name="observed-package", version=version)

    assert evidence.version == version


@pytest.mark.parametrize(
    ("contradiction", "message"),
    [
        ("setuptools", "does not match setuptools"),
        ("manager-version", "does not match Manager"),
        ("manager-disabled", "disabled Manager must be absent"),
    ],
)
def test_application_evidence_rejects_inventory_contradictions(
    contradiction: str,
    message: str,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = manifest_for_plan(plan).model_dump(mode="python")
    application = document["application"]
    if contradiction == "setuptools":
        application["setuptools"]["observed"] = "80.0.0"
    elif contradiction == "manager-version":
        application["manager"]["version"] = {
            "intended": "4.0.4",
            "observed": "4.0.4",
        }
    else:
        application["manager"] = {"enabled": False, "observed": "absent"}

    with pytest.raises(ValidationError, match=message):
        FinalManifest.model_validate(document)


def test_application_evidence_requires_the_exact_conditional_probe_checks() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = manifest_for_plan(plan).model_dump(mode="python")
    document["application"]["final_probe"]["checks"] = (
        "torch-import",
        "torch-cpu-tensor",
        "comfyui-folder-paths-import",
        "comfyui-comfy-import",
    )

    with pytest.raises(ValidationError, match="do not match the application intent"):
        FinalManifest.model_validate(document)


# The manifest command is the last filesystem mutation in every rendered build.
@pytest.mark.parametrize("with_files", [False, True])
def test_renderer_places_manifest_emission_after_every_build_mutation(
    with_files: bool,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    if not with_files:
        document = plan.model_dump(mode="python")
        document["files"]["files"] = ()
        plan = BuildPlan.model_validate(document)

    lines = render_build_plan_dockerfile(plan).splitlines()

    manifest_index = next(
        index for index, line in enumerate(lines) if "emit-final-manifest" in line
    )
    assert sum("emit-final-manifest" in line for line in lines) == 1
    assert manifest_index > next(
        index for index, line in enumerate(lines) if "install-custom-nodes" in line
    )
    if with_files:
        assert manifest_index > next(
            index for index, line in enumerate(lines) if "download-files" in line
        )
    assert lines[manifest_index + 1 :] == [
        "STOPSIGNAL SIGTERM",
        'ENTRYPOINT ["/usr/bin/tini", "--", "/opt/uv/bin/cdh", '
        '"container", "runtime", "serve"]',
    ]
