"""Final manifest schema, binding, omission, and renderer-order contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config.evidence.manifest import (
    ComfyCliEvidence,
    DistributionVersionEvidence,
    FinalManifest,
    HttpFileEvidence,
    InventoryDistribution,
    LocalFileEvidence,
    LocalTreeEvidence,
    SetuptoolsEvidence,
    ToolEnvironmentEvidence,
    VersionEvidence,
    dump_final_manifest,
    parse_final_manifest,
)
from comfyui_docker_helper.config.planning.build_plan import (
    BuildPlan,
    LocalFilePlan,
    validate_absolute_file_target,
)
from comfyui_docker_helper.rendering.final_renderer import (
    render_build_plan_dockerfile,
)
from tests.build_plan_support import accepted_resolution, build_plan, final_config
from tests.final_manifest_support import manifest_for_plan


# Canonical bytes retain exact local versions without promoting observations.
def test_manifest_round_trip_is_canonical_observational_and_strict() -> None:
    plan = build_plan(
        final_config(with_uv_tool=True), accepted_resolution(with_uv_tool=True)
    )
    manifest = manifest_for_plan(plan)

    content = dump_final_manifest(manifest)

    assert content.endswith(b"\n") and content.count(b"\n") == 1
    assert parse_final_manifest(content) == manifest
    assert [
        (tool.name, tool.environment, tool.direct.intended)
        for tool in manifest.toolchain.uv_tools
    ] == [("ruff", "uv-tool:ruff", "0.15.18")]
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


def test_local_tree_evidence_is_strict_and_compact() -> None:
    unlocked = LocalTreeEvidence(
        type="local",
        kind="tree",
        target="/workspace/ComfyUI/user/default/workflows",
        verification="unverified-local",
    )
    locked = LocalTreeEvidence(
        type="local",
        kind="tree",
        target="/workspace/ComfyUI/user/default/workflows",
        verification="sha256",
        observed_tree_digest="sha256:"
        "bfc5b459d61053042f6cc32617c7c26524963209696bbc6297794722dcabc95d",
    )

    assert unlocked.model_dump(exclude_none=True) == {
        "target": "/workspace/ComfyUI/user/default/workflows",
        "type": "local",
        "kind": "tree",
        "verification": "unverified-local",
    }
    assert locked.model_dump(exclude_none=True) == {
        "target": "/workspace/ComfyUI/user/default/workflows",
        "type": "local",
        "kind": "tree",
        "verification": "sha256",
        "observed_tree_digest": "sha256:"
        "bfc5b459d61053042f6cc32617c7c26524963209696bbc6297794722dcabc95d",
    }


@pytest.mark.parametrize(
    ("verification", "observed", "message"),
    [
        ("sha256", None, "requires an observed digest"),
        (
            "sha256",
            "sha256:invalid",
            "digest must be sha256",
        ),
        (
            "unverified-local",
            "sha256:" + "a" * 64,
            "unverified local tree evidence must omit content digests",
        ),
    ],
    ids=["sha256-missing-digest", "invalid-digest", "unverified-with-digest"],
)
def test_local_tree_evidence_enforces_verification_digest_schema(
    verification: str,
    observed: str | None,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        LocalTreeEvidence(
            type="local",
            kind="tree",
            target="/workspace/ComfyUI/user/default/workflows",
            verification=verification,
            observed_tree_digest=observed,
        )


@pytest.mark.parametrize("source_type", ["http", "local"])
def test_single_file_evidence_records_only_verified_observed_identity(
    source_type: str,
) -> None:
    identity = {"type": source_type, "target": "/workspace/ComfyUI/models/model.bin"}
    if source_type == "http":
        identity["url"] = "https://example.test/model.bin"
        model = HttpFileEvidence
        unverified = "unverified-moving"
    else:
        identity["kind"] = "file"
        model = LocalFileEvidence
        unverified = "unverified-local"
    digest = "sha256:" + "a" * 64
    verified = model.model_validate(
        {**identity, "verification": "sha256", "observed_checksum": digest}
    )
    moving = model.model_validate({**identity, "verification": unverified})

    assert verified.model_dump(exclude_none=True) == {
        **identity,
        "verification": "sha256",
        "observed_checksum": digest,
    }
    assert moving.model_dump(exclude_none=True) == {
        **identity,
        "verification": unverified,
    }
    with pytest.raises(ValidationError, match="requires an observed checksum"):
        model.model_validate({**identity, "verification": "sha256"})
    with pytest.raises(ValidationError, match="must omit content checksums"):
        model.model_validate(
            {**identity, "verification": unverified, "observed_checksum": digest}
        )


def test_file_target_authority_is_shared_by_plan_and_manifest() -> None:
    reserved = "/workspace/ComfyUI/.cdh-staging/model.bin"
    with pytest.raises(ValueError, match="reserved staging"):
        validate_absolute_file_target(reserved)
    with pytest.raises(ValidationError, match="reserved staging"):
        LocalFilePlan(
            type="local",
            kind="file",
            target=reserved,
            context_path="build/files/" + "a" * 64,
            verification="unverified-local",
            digest=None,
        )
    with pytest.raises(ValidationError, match="reserved staging"):
        LocalTreeEvidence(
            type="local",
            kind="tree",
            target=reserved,
            verification="unverified-local",
        )

    double_slash = "//workspace/ComfyUI/models/model.bin"
    assert validate_absolute_file_target(double_slash) == double_slash
    assert (
        LocalTreeEvidence(
            type="local",
            kind="tree",
            target=double_slash,
            verification="unverified-local",
        ).target
        == double_slash
    )


def test_local_file_and_tree_rows_are_discriminated_by_kind() -> None:
    document = manifest_for_plan(
        build_plan(final_config(), accepted_resolution())
    ).model_dump(mode="python")
    document["files"] = (
        {
            "type": "local",
            "kind": "file",
            "target": "/workspace/ComfyUI/models/model.bin",
            "verification": "unverified-local",
        },
        {
            "type": "local",
            "kind": "tree",
            "target": "/workspace/ComfyUI/user/default/workflows",
            "verification": "unverified-local",
        },
    )

    manifest = FinalManifest.model_validate(document)

    assert isinstance(manifest.files[0], LocalFileEvidence)
    assert isinstance(manifest.files[1], LocalTreeEvidence)


# User package evidence admits complete versions; managed tool evidence stays stable.
@pytest.mark.parametrize("version", ["1.0rc1", "1.0.dev1", "1.0+cuda"])
def test_user_distribution_evidence_accepts_canonical_complete_pep440(
    version: str,
) -> None:
    direct = DistributionVersionEvidence(intended=version, observed=version)
    tool = ToolEnvironmentEvidence(
        name="configured-tool",
        environment="uv-tool:configured-tool",
        direct=direct,
        inventory=(InventoryDistribution(name="configured-tool", version=version),),
        dependency_check="passed",
    )

    assert tool.direct == direct


def test_user_distribution_evidence_retains_exact_equality() -> None:
    with pytest.raises(ValidationError, match="intended and observed versions"):
        DistributionVersionEvidence(intended="1.0rc1", observed="1.0rc2")

    with pytest.raises(
        ValidationError, match="canonical exact stable distribution version"
    ):
        ComfyCliEvidence(
            name="comfy-cli",
            environment="uv-tool:comfy-cli",
            direct={"intended": "1.8.0rc1", "observed": "1.8.0rc1"},
            inventory=(InventoryDistribution(name="comfy-cli", version="1.8.0rc1"),),
            dependency_check="passed",
            entrypoints=("comfy", "comfy-cli", "comfycli"),
        )


def test_application_direct_evidence_accepts_locked_prerelease_result() -> None:
    document = manifest_for_plan(
        build_plan(final_config(), accepted_resolution())
    ).model_dump(mode="python")
    application = document["application"]
    direct = next(
        identity for name, identity in application["direct_packages"] if name == "numpy"
    )
    direct.update(intended="2.4.0rc1", observed="2.4.0rc1")
    inventory = next(
        item for item in application["inventory"] if item["name"] == "numpy"
    )
    inventory["version"] = "2.4.0rc1"

    manifest = FinalManifest.model_validate(document)

    numpy = dict(manifest.application.direct_packages)["numpy"]
    assert numpy.intended == "2.4.0rc1"


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
    runtime_index = next(
        index for index, line in enumerate(lines) if line == "# Runtime entrypoint"
    )
    assert runtime_index > manifest_index
    assert lines[runtime_index + 1 :] == [
        "STOPSIGNAL SIGTERM",
        'ENTRYPOINT ["/usr/bin/tini", "--", "/opt/uv/bin/cdh", '
        '"container", "runtime", "serve"]',
    ]
