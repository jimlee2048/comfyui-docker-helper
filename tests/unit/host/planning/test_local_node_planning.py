"""Local custom-node intent, input identity, and serialized admission."""

import hashlib
from dataclasses import replace
from pathlib import PurePosixPath

import pytest
from pydantic import ValidationError
from tests.build_plan_support import (
    accepted_resolution,
    build_plan,
    final_config,
    request_graph,
)

from comfyui_docker_helper.config.authored.validation.structure import (
    validate_final_config_structure,
)
from comfyui_docker_helper.config.evidence.custom_nodes import custom_node_inventory
from comfyui_docker_helper.config.planning.build_plan import BuildPlan
from comfyui_docker_helper.config.planning.canonical_lock import (
    LocalNodeLockEntry,
    LocalTreeLockEntry,
    canonical_entry_key,
    canonical_lock_from_entries,
    dump_canonical_lock_toml,
    parse_canonical_lock_toml,
)
from comfyui_docker_helper.config.planning.inputs.local import LocalTreePlanningInput
from comfyui_docker_helper.config.planning.inputs.local_node import (
    LocalNodePlanningInput,
    local_node_context_path,
)
from comfyui_docker_helper.config.planning.local_tree import (
    LocalTreeInventory,
    local_tree_digest,
)
from comfyui_docker_helper.config.planning.resolver import (
    AcceptedCanonicalLock,
    CanonicalResolutionError,
    LockPolicy,
    reconcile_canonical_lock,
)
from comfyui_docker_helper.filesystem.admission import LocalTreeMember


def config(*, locked=False, source="/private/node"):
    document = final_config().model_dump(mode="json")
    document["comfyui"]["custom_nodes"].append(
        dict(type="local", source=source, target_dir="dev-node", content_lock=locked)
    )
    return validate_final_config_structure(document)


def node_input(*, locked=False):
    inventory = LocalTreeInventory(())
    return LocalNodePlanningInput(
        "dev-node",
        local_node_context_path("dev-node"),
        locked,
        inventory,
        local_tree_digest(inventory) if locked else None,
    )


def resolution_with_node():
    base = accepted_resolution()
    lock = canonical_lock_from_entries(
        [
            *base.lock.entries,
            LocalNodeLockEntry(
                target_dir="dev-node",
                tree_digest=local_tree_digest(LocalTreeInventory(())),
            ),
        ]
    )
    return AcceptedCanonicalLock(lock, (), False, (), ())


def test_source_locator_does_not_affect_intent():
    resolution = accepted_resolution()
    assert request_graph(config(), resolution) == request_graph(
        config(source="../another"), resolution
    )


@pytest.mark.parametrize("locked", [False, True])
def test_local_node_round_trip_and_compact_evidence(locked):
    resolution = resolution_with_node() if locked else accepted_resolution()
    plan = build_plan(
        config(locked=locked),
        resolution,
        local_node_inputs=(node_input(locked=locked),),
    )
    assert BuildPlan.model_validate_json(plan.model_dump_json()) == plan
    node = plan.custom_nodes.nodes[-1]
    assert node.target == "/workspace/ComfyUI/custom_nodes/dev-node"
    assert node.verification == ("sha256" if locked else "unverified-local")
    assert custom_node_inventory((node,)).nodes[0].model_dump() == dict(
        type="local",
        target="dev-node",
        verification="local-directory",
        control="direct-local",
    )
    assert "/private/node" not in plan.model_dump_json()


@pytest.mark.parametrize(
    "case,diagnostic",
    [
        ("missing", "missing local node planning inputs"),
        ("duplicate", "duplicate local node planning input"),
        ("unused", "unused local node planning inputs"),
        ("mode", "local node planning input lock mode does not match request"),
    ],
    ids=["missing", "duplicate", "unused", "mode"],
)
def test_local_input_set_must_match_requests(case, diagnostic):
    item = node_input()
    inputs = (
        ()
        if case == "missing"
        else (item, item)
        if case == "duplicate"
        else (node_input(locked=True),)
        if case == "mode"
        else (
            item,
            replace(
                item,
                target_dir="unused",
                context_path=local_node_context_path("unused"),
            ),
        )
    )
    with pytest.raises(ValueError, match=diagnostic):
        build_plan(config(), accepted_resolution(), local_node_inputs=inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("target", "/elsewhere/dev-node"),
        ("context_path", "build/trees/" + "a" * 64),
        ("tree_digest", "sha256:" + "a" * 64),
    ],
)
def test_plan_rejects_local_authority_forgery(field, value):
    plan = build_plan(
        config(), accepted_resolution(), local_node_inputs=(node_input(),)
    )
    document = plan.model_dump(mode="python")
    document["custom_nodes"]["nodes"][-1][field] = value
    with pytest.raises(ValidationError):
        BuildPlan.model_validate(document)


def test_same_image_target_keeps_file_and_node_slots_and_locks_independent():
    document = config(locked=True).model_dump(mode="json")
    target = "custom_nodes/dev-node"
    document["files"] = [
        dict(type="local", source="/another", target=target, content_lock=True)
    ]
    item = node_input(locked=True)
    tree = LocalTreePlanningInput(
        PurePosixPath(target),
        PurePosixPath("build/trees") / hashlib.sha256(target.encode()).hexdigest(),
        True,
        LocalTreeInventory(()),
        local_tree_digest(LocalTreeInventory(())),
    )
    lock = canonical_lock_from_entries(
        [
            *resolution_with_node().lock.entries,
            LocalTreeLockEntry(
                kind="tree", relative_target=target, tree_digest=tree.tree_digest
            ),
        ]
    )
    plan = build_plan(
        validate_final_config_structure(document),
        AcceptedCanonicalLock(lock, (), False, (), ()),
        local_inputs=(tree,),
        local_node_inputs=(item,),
    )
    assert plan.files.files[0].target == plan.custom_nodes.nodes[-1].target
    assert plan.files.files[0].context_path != plan.custom_nodes.nodes[-1].context_path
    assert canonical_entry_key(lock.custom_nodes.local[0]) == (
        "custom_nodes",
        "local",
        "dev-node",
    )
    assert parse_canonical_lock_toml(dump_canonical_lock_toml(lock)) == lock


class NoAcquisition:
    def acquire(self, *args):
        raise AssertionError("locked local identity must not call providers")


def test_locked_local_identity_is_reconciled_without_provider_calls():
    resolution = resolution_with_node()
    graph = request_graph(config(locked=True), resolution)
    accepted = reconcile_canonical_lock(
        graph.desired,
        existing=resolution.lock,
        acquirer=NoAcquisition(),
        policy=LockPolicy.LOCKED,
        local_node_inputs=(node_input(locked=True),),
        local_node_targets=("dev-node",),
    )
    assert accepted.lock == resolution.lock
    assert accepted.provider_calls == ()
    changed = canonical_lock_from_entries(
        [
            entry.model_copy(update={"tree_digest": "sha256:" + "a" * 64})
            if isinstance(entry, LocalNodeLockEntry)
            else entry
            for entry in resolution.lock.entries
        ]
    )
    with pytest.raises(CanonicalResolutionError):
        reconcile_canonical_lock(
            graph.desired,
            existing=changed,
            acquirer=NoAcquisition(),
            policy=LockPolicy.LOCKED,
            local_node_inputs=(node_input(locked=True),),
            local_node_targets=("dev-node",),
        )


def test_nonempty_locked_node_proves_members_and_parent_structure():
    inventory = LocalTreeInventory(
        (
            LocalTreeMember("pkg", "directory", None, None),
            LocalTreeMember(
                "pkg/__init__.py",
                "file",
                4,
                "sha256:" + hashlib.sha256(b"pass").hexdigest(),
            ),
        )
    )
    item = replace(
        node_input(locked=True),
        inventory=inventory,
        tree_digest=local_tree_digest(inventory),
    )
    lock = canonical_lock_from_entries(
        [
            *accepted_resolution().lock.entries,
            LocalNodeLockEntry(
                target_dir=item.target_dir, tree_digest=item.tree_digest
            ),
        ]
    )
    plan = build_plan(
        config(locked=True),
        AcceptedCanonicalLock(lock, (), False, (), ()),
        local_node_inputs=(item,),
    )
    assert BuildPlan.model_validate_json(plan.model_dump_json()) == plan
    node = plan.custom_nodes.nodes[-1]
    assert tuple(member.relative_path for member in node.members) == (
        "pkg",
        "pkg/__init__.py",
    )
    for missing_parent in (False, True):
        document = plan.model_dump(mode="python")
        raw = document["custom_nodes"]["nodes"][-1]
        if missing_parent:
            raw["members"] = raw["members"][1:]
        else:
            raw["members"][-1]["digest"] = "sha256:" + "a" * 64
        with pytest.raises(
            ValidationError,
            match="parents must be admitted"
            if missing_parent
            else "digest does not match",
        ):
            BuildPlan.model_validate(document)
