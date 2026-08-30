"""Authenticated BuildPlan admission for Linux image helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from comfyui_docker_helper.config.planning.build_plan import (
    BuildPlan,
    LocalFilePlan,
    build_plan_digest,
    dump_build_plan_json,
    manifest_binding,
)
from comfyui_docker_helper.container.build.admission import (
    BuildPlanInputAdmission,
    FinalManifestLocalFileInput,
    FinalManifestLocalTreeInput,
    LocalTreeMemberInput,
    LocalTreeNormalizationInput,
)
from tests.build_plan_support import accepted_resolution, build_plan, final_config


def _write_plan(path: Path) -> tuple[BuildPlan, str]:
    plan = build_plan(final_config(), accepted_resolution())
    path.write_bytes(dump_build_plan_json(plan))
    return plan, build_plan_digest(plan)


def _plan_with_local_file(*, locked: bool) -> BuildPlan:
    plan = build_plan(final_config(), accepted_resolution())
    digest = f"sha256:{'a' * 64}" if locked else None
    relative_target = "models/model.bin"
    local = LocalFilePlan(
        type="local",
        kind="file",
        target="/workspace/ComfyUI/models/model.bin",
        relative_target=relative_target,
        context_path=(
            "build/files/" + hashlib.sha256(relative_target.encode("utf-8")).hexdigest()
        ),
        verification="sha256" if locked else "unverified-local",
        digest=digest,
    )
    return plan.model_copy(
        update={"files": plan.files.model_copy(update={"files": (local,)})}
    )


def test_admission_projects_authenticated_plan_for_container_consumers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "build-plan.json"
    plan, digest = _write_plan(path)

    admission = BuildPlanInputAdmission.from_path(
        path,
        expected_build_plan_digest=digest,
    )

    assert admission.comfyui_install() == (plan.application, plan.toolchain)
    assert admission.custom_node_install() == (
        plan.custom_nodes,
        plan.application,
    )
    assert admission.git_credential_routes() == plan.custom_nodes.git_credentials
    assert admission.file_downloads() == (
        plan.files,
        plan.application.paths.comfyui,
    )


def test_admission_projects_only_plan_selected_tree_paths_and_kinds() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    relative_target = "user/default/workflows"
    tree = {
        "type": "local",
        "kind": "tree",
        "target": f"{plan.application.paths.comfyui}/{relative_target}",
        "relative_target": relative_target,
        "context_path": (
            "build/trees/" + hashlib.sha256(relative_target.encode()).hexdigest()
        ),
        "verification": "unverified-local",
        "members": (
            {
                "relative_path": "nested",
                "kind": "directory",
                "size": None,
                "digest": None,
            },
            {
                "relative_path": "nested/file.bin",
                "kind": "file",
                "size": None,
                "digest": None,
            },
        ),
        "tree_digest": None,
    }
    document = plan.model_dump(mode="python")
    document["files"]["files"] = (tree,)
    plan = BuildPlan.model_validate(document)

    projected, root = BuildPlanInputAdmission(plan).local_trees()

    assert root == plan.application.paths.comfyui
    assert projected == (
        LocalTreeNormalizationInput(
            target=f"{root}/{relative_target}",
            members=(
                LocalTreeMemberInput("nested", "directory"),
                LocalTreeMemberInput("nested/file.bin", "file"),
            ),
        ),
    )


@pytest.mark.parametrize("locked", [True, False], ids=["locked", "unlocked"])
def test_final_projection_retains_local_identity_without_context_locator(
    locked: bool,
) -> None:
    plan = _plan_with_local_file(locked=locked)

    projected = BuildPlanInputAdmission(plan).final_manifest().files

    assert projected == (
        FinalManifestLocalFileInput(
            type="local",
            target="/workspace/ComfyUI/models/model.bin",
            verification="sha256" if locked else "unverified-local",
            digest=f"sha256:{'a' * 64}" if locked else None,
        ),
    )


def test_final_projection_keeps_tree_records_without_manifest_inventory() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    relative_target = "user/default/workflows"
    tree = {
        "type": "local",
        "kind": "tree",
        "target": f"/workspace/ComfyUI/{relative_target}",
        "relative_target": relative_target,
        "context_path": (
            "build/trees/" + hashlib.sha256(relative_target.encode()).hexdigest()
        ),
        "verification": "sha256",
        "members": (
            {
                "relative_path": "nested",
                "kind": "directory",
                "size": None,
                "digest": None,
            },
            {
                "relative_path": "nested/file.bin",
                "kind": "file",
                "size": 3,
                "digest": "sha256:"
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            },
        ),
        "tree_digest": "sha256:"
        "47e5919a75ebe3e3ddaa8b1ecee1da39b8dbc65865673c6884ebc3622832db24",
    }
    document = plan.model_dump(mode="python")
    document["files"]["files"] = (tree,)
    plan = BuildPlan.model_validate(document)

    projection = BuildPlanInputAdmission(plan).final_manifest()

    assert projection.binding == manifest_binding(plan)
    projected = projection.files

    assert projected == (
        FinalManifestLocalTreeInput(
            type="local",
            kind="tree",
            target=f"/workspace/ComfyUI/{relative_target}",
            verification="sha256",
            members=(
                LocalTreeMemberInput("nested", "directory"),
                LocalTreeMemberInput("nested/file.bin", "file"),
            ),
            intended_tree_digest="sha256:"
            "47e5919a75ebe3e3ddaa8b1ecee1da39b8dbc65865673c6884ebc3622832db24",
        ),
    )


def test_admission_rejects_changed_plan_under_literal_digest(tmp_path: Path) -> None:
    path = tmp_path / "build-plan.json"
    _plan, digest = _write_plan(path)
    document = json.loads(path.read_bytes())
    document["runtime"]["environment"][0]["value"] = "changed"
    path.write_text(json.dumps(document))

    with pytest.raises(ValueError, match="expected digest"):
        BuildPlanInputAdmission.from_path(
            path,
            expected_build_plan_digest=digest,
        )


def test_admission_rejects_leaf_and_ancestor_symlinks(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir(mode=0o700)
    path = context / "build-plan.json"
    _plan, digest = _write_plan(path)

    leaf_link = tmp_path / "build-plan-link.json"
    leaf_link.symlink_to(path)
    with pytest.raises(ValueError, match="could not read canonical BuildPlan"):
        BuildPlanInputAdmission.from_path(
            leaf_link,
            expected_build_plan_digest=digest,
        )

    ancestor_link = tmp_path / "context-link"
    ancestor_link.symlink_to(context, target_is_directory=True)
    with pytest.raises(ValueError, match="could not read canonical BuildPlan"):
        BuildPlanInputAdmission.from_path(
            ancestor_link / "build-plan.json",
            expected_build_plan_digest=digest,
        )
