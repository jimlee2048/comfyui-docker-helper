"""BuildPlan construction and projection contracts."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path, PurePosixPath

import pytest
import tomli_w
from pydantic import ValidationError
from tests.build_plan_support import (
    COMMIT_B,
    DIGEST_A,
    DIGEST_B,
    accepted_resolution,
    build_plan,
    canonical_wheel,
    final_config,
    request_graph,
)

import comfyui_docker_helper.config.planning.build_plan as build_plan_module
import comfyui_docker_helper.config.planning.request as canonical_request_module
from comfyui_docker_helper.config.authored.service import load_validate_config_result
from comfyui_docker_helper.config.authored.validation.structure import (
    validate_final_config_structure,
)
from comfyui_docker_helper.config.planning.build_plan import (
    BUILD_PLAN_SCHEMA_VERSION,
    BuildPlan,
    LocalTreeMemberPlan,
    LocalTreePlan,
    RuntimePlanningProvenance,
)
from comfyui_docker_helper.config.planning.build_plan import (
    construct_build_plan as _construct_build_plan,
)
from comfyui_docker_helper.config.planning.canonical_lock import (
    CanonicalLock,
    DirectPythonRequestIdentity,
    LocalFileLockEntry,
    LocalTreeLockEntry,
    PyTorchRequestIdentity,
    UvToolLockEntry,
    canonical_lock_from_entries,
)
from comfyui_docker_helper.config.planning.inputs.local import (
    LocalFilePlanningInput,
    LocalTreePlanningInput,
)
from comfyui_docker_helper.config.planning.local_tree import (
    LocalTreeInventory,
    local_tree_digest,
)
from comfyui_docker_helper.config.planning.resolver import AcceptedCanonicalLock
from comfyui_docker_helper.exact_ledger import (
    UV_IMAGE_REPOSITORY,
)
from comfyui_docker_helper.filesystem.admission import LocalTreeMember
from comfyui_docker_helper.version import package_version

_VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "first@example"
)


def test_runtime_and_build_constraint_projections_have_distinct_compatibility() -> None:
    group = build_plan(final_config(), accepted_resolution()).application.pytorch

    assert build_plan_module.managed_runtime_constraints_bytes(group) == (
        b"setuptools<82\ntorch==2.12.1+cu130\n"
        b"torchaudio==2.11.0+cu130\ntorchvision==0.27.1+cu130\n"
    )
    assert build_plan_module.managed_build_constraints_bytes(group) == (
        b"torch==2.12.1+cu130\ntorchaudio==2.11.0+cu130\ntorchvision==0.27.1+cu130\n"
    )


def test_constructor_consumes_exact_authorities_and_orders_values() -> None:
    plan = build_plan(final_config(), accepted_resolution())

    assert plan.schema_version == BUILD_PLAN_SCHEMA_VERSION
    assert plan.toolchain.cuda_image.reference == (
        f"nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04@{DIGEST_A}"
    )
    assert plan.toolchain.uv_image.reference == (
        f"{UV_IMAGE_REPOSITORY}:0.11.28-debian-slim@{DIGEST_B}"
    )
    assert plan.toolchain.python.version == "3.13.14"
    assert plan.toolchain.pytorch_channel == "cu130"
    assert [item.name for item in plan.application.pytorch.packages] == [
        "torch",
        "torchaudio",
        "torchvision",
    ]
    assert plan.application.pytorch.packages[2].requirement == (
        "torchvision[image]==0.27.1+cu130"
    )
    assert plan.application.python_extras is not None
    assert plan.application.python_extras.packages[0].requirement == "numpy==2.3.1"
    assert plan.application.pip_version == "26.1.2"
    manager = plan.application.comfyui.manager
    assert manager is not None
    assert manager.requirements_path == "manager_requirements.txt"
    assert manager.distribution == "comfyui-manager"
    assert manager.import_name == "comfyui_manager"
    assert manager.executable == "/opt/venv/bin/cm-cli"
    assert manager.entrypoint_name == "cm-cli"
    assert manager.import_anchor == (
        "/opt/venv/lib/python3.13/site-packages/comfyui-docker-helper-comfyui.pth"
    )
    assert [item.name for item in plan.runtime.environment] == ["ALPHA", "ZED"]
    assert plan.runtime.shutdown_timeout == 8
    assert plan.runtime.launch_command[-3:] == (
        "--disable-auto-launch",
        "--preview-method",
        "latent2rgb",
    )
    assert not hasattr(plan.custom_nodes.nodes[0], "target")
    assert plan.custom_nodes.user_directory == "/workspace/ComfyUI/user"
    assert plan.custom_nodes.nodes[1].url == "https://example.test/direct.git"
    assert plan.custom_nodes.nodes[1].commit == COMMIT_B
    assert plan.custom_nodes.nodes[1].target.endswith("/custom_nodes/direct-node")
    assert plan.files.files[0].target == (
        "/workspace/ComfyUI/models/checkpoints/model.safetensors"
    )
    assert "--enable-manager" not in plan.runtime.launch_command


def test_constructor_carries_python_314_exact_identity_through_build_plan() -> None:
    plan = build_plan(
        final_config(python_version="3.14.6"),
        accepted_resolution(python_version="3.14.6"),
    )

    assert plan.toolchain.python.version == "3.14.6"
    assert plan.toolchain.python.catalog_key == "cpython-3.14.6-linux-x86_64-gnu"
    assert plan.application.comfyui.requirements.python_version == "3.14.6"
    assert plan.application.pytorch.python_version == "3.14.6"
    assert plan.application.comfyui.manager is not None
    assert plan.application.comfyui.manager.import_anchor == (
        "/opt/venv/lib/python3.14/site-packages/comfyui-docker-helper-comfyui.pth"
    )
    assert plan.toolchain.tool_store.comfy_cli is not None


def test_constructor_projects_application_direct_source_with_locked_version() -> None:
    source = "https://example.test/numpy.whl#sha256=abc"
    config = final_config().model_copy(deep=True)
    config.python.extra_packages = [f"NumPy @ {source}"]
    resolution = accepted_resolution()
    graph = request_graph(config, resolution)
    desired = next(
        item
        for item in graph.desired
        if isinstance(item.request, DirectPythonRequestIdentity)
        and item.request.group == "application-extra"
    )
    document = resolution.lock.model_dump(mode="python")
    document["python"]["package_groups"]["application_extras"].update(
        request_digest=desired.request_digest
    )
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(document),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    plan = build_plan(config, changed)

    assert plan.application.python_extras is not None
    package = plan.application.python_extras.packages[0]
    assert package.version == "2.3.1"
    assert package.direct_reference == source
    assert package.requirement == f"numpy @ {source}"


def test_constructor_projects_pytorch_extra_direct_source() -> None:
    source = "https://example.test/sageattention.whl"
    config = final_config().model_copy(deep=True)
    config.pytorch.extra_packages.append(f"SageAttention @ {source}")
    resolution = accepted_resolution()
    graph = request_graph(config, resolution)
    desired = next(
        item
        for item in graph.desired
        if isinstance(item.request, PyTorchRequestIdentity)
    )
    document = resolution.lock.model_dump(mode="python")
    entry = document["python"]["package_groups"]["pytorch"]
    entry["request_digest"] = desired.request_digest
    entry["packages"] = tuple(
        sorted(
            (
                *entry["packages"],
                {
                    "name": "sageattention",
                    "extras": (),
                    "version": "2.2.0+cu130",
                },
            ),
            key=lambda item: item["name"],
        )
    )
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(document),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    plan = build_plan(config, changed)

    package = next(
        item
        for item in plan.application.pytorch.packages
        if item.name == "sageattention"
    )
    assert package.version == "2.2.0+cu130"
    assert package.direct_reference == source
    assert package.requirement == f"sageattention @ {source}"


def test_request_graph_freezes_one_protected_name_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = final_config()
    resolution = accepted_resolution()
    expected = canonical_request_module.CudaBackendAdapter().protected_requirement_names
    reads = 0

    def read_protected_names(_self):
        nonlocal reads
        reads += 1
        return expected

    monkeypatch.setattr(
        canonical_request_module.CudaBackendAdapter,
        "protected_requirement_names",
        property(read_protected_names),
    )

    graph = request_graph(config, resolution)

    assert reads == 1
    assert graph.protected_requirement_names == expected


def test_build_plan_binds_optional_manager_capability_to_custom_node_intent() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["application"]["comfyui"]["manager"] = None

    with pytest.raises(ValidationError, match="Manager capability does not match"):
        BuildPlan.model_validate(document)

    document["custom_nodes"]["install_manager"] = False
    document["custom_nodes"]["nodes"] = tuple(
        node for node in document["custom_nodes"]["nodes"] if node["type"] == "git"
    )
    disabled = BuildPlan.model_validate(document)

    assert disabled.application.comfyui.manager is None


def test_constructor_projects_isolated_uv_tool_exact_result() -> None:
    plan = build_plan(
        final_config(with_uv_tool=True), accepted_resolution(with_uv_tool=True)
    )

    assert len(plan.toolchain.tool_store.uv_tools) == 1
    tool = plan.toolchain.tool_store.uv_tools[0]
    assert tool.environment == "uv-tool:ruff"
    assert tool.requirement == "ruff==0.15.18"
    assert plan.toolchain.tool_store.cdh.version == package_version()
    assert plan.toolchain.tool_store.cdh.wheel_digest == canonical_wheel().digest


def test_constructor_projects_explicit_prerelease_uv_tool_result() -> None:
    config = final_config(with_uv_tool=True).model_copy(deep=True)
    config.python.uv_tools = ["Ruff==0.16.0rc1"]
    resolution = accepted_resolution(with_uv_tool=True)
    graph = request_graph(config, resolution)
    request = next(
        item
        for item in graph.desired
        if isinstance(item.request, DirectPythonRequestIdentity)
        and item.request.group == "uv-tool"
    )
    document = resolution.lock.model_dump(mode="python")
    tool_entry = next(
        item for item in document["python"]["uv_tools"] if item["name"] == "ruff"
    )
    tool_entry.update(
        request_digest=request.request_digest,
        version="0.16.0rc1",
    )
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(document),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    plan = build_plan(config, changed)

    assert plan.toolchain.tool_store.uv_tools[0].requirement == "ruff==0.16.0rc1"


def test_constructor_projects_uv_tool_direct_source_with_locked_version() -> None:
    source = "git+https://example.test/ruff.git@main"
    config = final_config(with_uv_tool=True).model_copy(deep=True)
    config.python.uv_tools = [f"Ruff @ {source}"]
    resolution = accepted_resolution(with_uv_tool=True)
    graph = request_graph(config, resolution)
    desired = next(
        item
        for item in graph.desired
        if isinstance(item.request, DirectPythonRequestIdentity)
        and item.request.group == "uv-tool"
    )
    document = resolution.lock.model_dump(mode="python")
    tool_entry = next(
        item for item in document["python"]["uv_tools"] if item["name"] == "ruff"
    )
    tool_entry["request_digest"] = desired.request_digest
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(document),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    plan = build_plan(config, changed)

    tool = plan.toolchain.tool_store.uv_tools[0]
    assert tool.version == "0.15.18"
    assert tool.direct_reference == source
    assert tool.requirement == f"ruff @ {source}"


def test_constructor_projects_optional_comfy_cli_only_to_the_tool_store() -> None:
    enabled = build_plan(final_config(), accepted_resolution())
    disabled = build_plan(
        final_config(install_cli=False), accepted_resolution(install_cli=False)
    )

    tool = enabled.toolchain.tool_store.comfy_cli
    assert tool is not None
    assert tool.requirement == "comfy-cli==1.8.0"
    assert tool.environment == "uv-tool:comfy-cli"
    assert tool.executables == ("comfy", "comfy-cli", "comfycli")
    assert disabled.toolchain.tool_store.comfy_cli is None


@pytest.mark.parametrize("group", ["python", "pytorch"])
def test_build_plan_reserves_comfy_cli_from_every_application_group(
    group: str,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    packages = (
        document["application"]["python_extras"]["packages"]
        if group == "python"
        else document["application"]["pytorch"]["packages"]
    )
    packages[-1]["name"] = "comfy-cli"

    with pytest.raises(ValidationError, match="dedicated optional tool"):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    "name", ["torch", "torchvision", "torchaudio", "pip", "setuptools"]
)
def test_build_plan_rejects_python_extra_package_owner_overlap(name: str) -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["application"]["python_extras"]["packages"][0]["name"] = name

    with pytest.raises(ValidationError, match="overlap protected package owners"):
        BuildPlan.model_validate(document)


def test_build_plan_rejects_pytorch_discriminator_for_python_extras() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["application"]["python_extras"]["group"] = "pytorch"

    with pytest.raises(ValidationError, match="application-extra"):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    "requirement",
    [
        "torch==2.12.1",
        "torchvision==0.27.1",
        "torchaudio==2.11.0",
        "pip==26.1.2",
        "setuptools==81.0.0",
    ],
)
def test_constructor_rejects_python_extra_package_owner_overlap_before_consumption(
    requirement: str,
) -> None:
    document = final_config().model_dump(mode="python")
    document["python"]["extra_packages"] = [requirement]
    forged = validate_final_config_structure(document)

    with pytest.raises(ValueError, match="overlap protected package owners"):
        build_plan(forged, accepted_resolution())


def test_constructor_rejects_python_extra_overlap_with_arbitrary_pytorch_extra() -> (
    None
):
    document = final_config().model_dump(mode="python")
    document["python"]["extra_packages"] = ["xformers==0.0.35"]
    document["pytorch"]["extra_packages"].append("XFormers==0.0.35")
    forged = validate_final_config_structure(document)

    with pytest.raises(ValueError, match="overlap protected package owners"):
        build_plan(forged, accepted_resolution())


def test_build_plan_rejects_python_extra_overlap_with_arbitrary_pytorch_member() -> (
    None
):
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    xformers = {
        "name": "xformers",
        "extras": (),
        "version": "0.0.35",
        "direct_reference": None,
        "environment": "application",
    }
    document["application"]["pytorch"]["packages"] = (
        *document["application"]["pytorch"]["packages"],
        xformers,
    )
    document["application"]["python_extras"]["packages"] = (xformers,)

    with pytest.raises(ValidationError, match="overlap protected package owners"):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    "name", ["UV_INDEX", "UV_INDEX_URL", "UV_TOOL_DIR", "PIP_CONSTRAINT"]
)
def test_build_plan_never_inherits_user_package_environment_controls(
    name: str,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["runtime"]["environment"] = ({"name": name, "value": "user-value"},)

    with pytest.raises(ValidationError, match="reserved to cdh image authority"):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    ("base_requirement", "later_requirement", "expected_selector"),
    [
        ("NumPy>=2,<3", "numpy<3,>=2", "<3,>=2"),
        ("NumPy", "numpy", ""),
        ("NumPy~=2.0", "numpy~=2.0", "~=2.0"),
    ],
)
def test_canonical_requirement_spelling_is_stable_from_layered_config_to_plan(
    tmp_path: Path,
    base_requirement: str,
    later_requirement: str,
    expected_selector: str,
) -> None:
    def load_layered(
        stem: str,
        base_requirement: str,
        later_requirement: str,
    ):
        document = final_config().model_dump(mode="json", exclude_none=True)
        document["python"]["extra_packages"] = [base_requirement]
        base = tmp_path / f"{stem}-base.toml"
        later = tmp_path / f"{stem}-later.toml"
        base.write_text(tomli_w.dumps(document))
        later.write_text(f'[python]\nextra_packages = ["{later_requirement}"]\n')
        return load_validate_config_result([base, later])

    first = load_layered("first", base_requirement, later_requirement)
    second = load_layered("second", later_requirement, base_requirement)

    assert first.config.python.extra_packages == [later_requirement]
    assert second.config.python.extra_packages == [base_requirement]

    resolution = accepted_resolution()
    first_graph = request_graph(first.config, resolution)
    second_graph = request_graph(second.config, resolution)
    first_desired = next(
        desired
        for desired in first_graph.desired
        if isinstance(desired.request, DirectPythonRequestIdentity)
        and desired.request.group == "application-extra"
    )
    second_desired = next(
        desired
        for desired in second_graph.desired
        if isinstance(desired.request, DirectPythonRequestIdentity)
        and desired.request.group == "application-extra"
    )
    first_request = first_desired.request
    second_request = second_desired.request

    assert first_request == second_request
    assert first_request.members[0].model_dump(mode="python") == {
        "package": "numpy",
        "extras": (),
        "specifier": expected_selector,
        "direct_reference": None,
    }
    assert first_graph.image_config_digest == second_graph.image_config_digest

    lock_document = resolution.lock.model_dump(mode="python")
    lock_document["python"]["package_groups"]["application_extras"][
        "request_digest"
    ] = first_desired.request_digest
    matching_resolution = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(lock_document),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )
    first_plan = build_plan(first.config, matching_resolution)
    second_plan = build_plan(second.config, matching_resolution)

    assert first_plan == second_plan
    assert first_plan.image_config_digest == first_graph.image_config_digest


def test_runtime_file_directory_spelling_is_canonical_from_request_to_plan() -> None:
    first_document = final_config().model_dump(mode="json", exclude_none=True)
    second_document = deepcopy(first_document)
    first_document["files"][0]["target"] = "./models//checkpoints/./model.safetensors"
    second_document["files"][0]["target"] = "models/checkpoints/model.safetensors"
    first_config = validate_final_config_structure(first_document)
    second_config = validate_final_config_structure(second_document)
    resolution = accepted_resolution()

    first_graph = request_graph(first_config, resolution)
    second_graph = request_graph(second_config, resolution)
    first_plan = build_plan(first_config, resolution)
    second_plan = build_plan(second_config, resolution)

    assert first_graph.files == second_graph.files
    assert first_graph.files[0].target == (
        "/workspace/ComfyUI/models/checkpoints/model.safetensors"
    )
    assert first_graph.image_config_digest == second_graph.image_config_digest
    assert first_plan == second_plan


def test_local_file_mode_does_not_change_image_config_identity() -> None:
    first_document = final_config().model_dump(mode="json", exclude_none=True)
    first_document["files"] = []
    second_document = deepcopy(first_document)
    second_document["cdh"]["local_file_mode"] = "copy"
    first_config = validate_final_config_structure(first_document)
    second_config = validate_final_config_structure(second_document)
    resolution = accepted_resolution()

    first_graph = request_graph(first_config, resolution)
    second_graph = request_graph(second_config, resolution)

    assert first_graph.image_config_digest == second_graph.image_config_digest


def test_local_graph_identity_depends_only_on_target_and_lock_mode() -> None:
    first_document = final_config().model_dump(mode="json", exclude_none=True)
    first_document["files"] = [
        {
            "type": "local",
            "source": "/private/first-model.bin",
            "target": "models/model.bin",
            "content_lock": False,
        }
    ]
    second_document = deepcopy(first_document)
    second_document["files"][0]["source"] = "/other/private-model.bin"
    first_config = validate_final_config_structure(first_document)
    second_config = validate_final_config_structure(second_document)
    resolution = accepted_resolution()

    first_graph = request_graph(first_config, resolution)
    second_graph = request_graph(second_config, resolution)

    assert first_graph == second_graph
    local = first_graph.files[0]
    assert local.type == "local"
    assert local.target == "/workspace/ComfyUI/models/model.bin"
    assert local.relative_target == "models/model.bin"
    assert local.content_lock is False


def test_local_file_plan_consumes_only_locked_content_identity() -> None:
    document = final_config().model_dump(mode="json", exclude_none=True)
    document["files"] = [
        {
            "type": "local",
            "source": "model.bin",
            "target": "models/model.bin",
            "content_lock": True,
        }
    ]
    config = validate_final_config_structure(document)
    resolution = accepted_resolution()
    lock = canonical_lock_from_entries(
        [
            *resolution.lock.entries,
            LocalFileLockEntry(
                kind="file",
                relative_target="models/model.bin",
                digest=DIGEST_A,
            ),
        ]
    )
    locked_resolution = AcceptedCanonicalLock(lock, (), False, (), ())

    context_path = "build/files/" + hashlib.sha256(b"models/model.bin").hexdigest()
    item = build_plan(
        config,
        locked_resolution,
        local_inputs=(
            LocalFilePlanningInput(
                relative_target=PurePosixPath("models/model.bin"),
                context_path=PurePosixPath(context_path),
                content_lock=True,
                digest=DIGEST_A,
            ),
        ),
    ).files.files[0]

    assert item.type == "local"
    assert item.verification == "sha256"
    assert item.digest == DIGEST_A


def test_local_tree_plan_freezes_sorted_structure_without_unlocked_content() -> None:
    document = final_config().model_dump(mode="json", exclude_none=True)
    relative_target = "user/default/workflows"
    document["files"] = [
        {
            "type": "local",
            "source": "workflows",
            "target": relative_target,
            "content_lock": False,
        }
    ]
    config = validate_final_config_structure(document)
    resolution = accepted_resolution()
    inventory = LocalTreeInventory(
        (
            LocalTreeMember("nested", "directory"),
            LocalTreeMember("nested/file.txt", "file"),
        )
    )
    context_path = PurePosixPath(
        "build/trees/" + hashlib.sha256(relative_target.encode("utf-8")).hexdigest()
    )
    admitted = LocalTreePlanningInput(
        relative_target=PurePosixPath(relative_target),
        context_path=context_path,
        content_lock=False,
        inventory=inventory,
        tree_digest=None,
    )

    plan = build_plan(config, resolution, local_inputs=(admitted,))
    item = plan.files.files[0]

    assert isinstance(item, LocalTreePlan)
    assert item.target == "/workspace/ComfyUI/user/default/workflows"
    assert item.context_path == context_path.as_posix()
    assert item.members == (
        LocalTreeMemberPlan(
            relative_path="nested",
            kind="directory",
            size=None,
            digest=None,
        ),
        LocalTreeMemberPlan(
            relative_path="nested/file.txt",
            kind="file",
            size=None,
            digest=None,
        ),
    )
    assert item.tree_digest is None
    assert BuildPlan.model_validate_json(plan.model_dump_json()) == plan


def test_local_tree_plan_root_sentinel_and_locked_aggregate_are_current_v1() -> None:
    document = final_config().model_dump(mode="json", exclude_none=True)
    document["files"] = [
        {
            "type": "local",
            "source": ".",
            "target": ".",
            "content_lock": True,
        }
    ]
    config = validate_final_config_structure(document)
    resolution = accepted_resolution()
    inventory = LocalTreeInventory(())
    tree_digest = local_tree_digest(inventory)
    lock = canonical_lock_from_entries(
        [
            *resolution.lock.entries,
            LocalTreeLockEntry(
                kind="tree",
                relative_target=".",
                tree_digest=tree_digest,
            ),
        ]
    )
    locked_resolution = AcceptedCanonicalLock(lock, (), False, (), ())
    relative_target = PurePosixPath(".")
    admitted = LocalTreePlanningInput(
        relative_target=relative_target,
        context_path=PurePosixPath(
            "build/trees/"
            + hashlib.sha256(relative_target.as_posix().encode("utf-8")).hexdigest()
        ),
        content_lock=True,
        inventory=inventory,
        tree_digest=tree_digest,
    )

    item = build_plan(
        config,
        locked_resolution,
        local_inputs=(admitted,),
    ).files.files[0]

    assert isinstance(item, LocalTreePlan)
    assert item.target == "/workspace/ComfyUI"
    assert item.relative_target == "."
    assert item.members == ()
    assert item.tree_digest == tree_digest


def test_redundant_default_package_and_ssh_key_spelling_do_not_change_plan(
    tmp_path: Path,
) -> None:
    baseline_document = final_config().model_dump(mode="json", exclude_none=True)
    baseline_document["system"]["extra_packages"] = []
    baseline_document["system"]["ssh"]["pub_keys"] = [_VALID_SSH_KEY]
    redundant_document = deepcopy(baseline_document)
    redundant_document["system"]["extra_packages"] = ["bash"]
    redundant_document["system"]["ssh"]["pub_keys"] = [
        " ",
        f"  {_VALID_SSH_KEY}  ",
        _VALID_SSH_KEY.rsplit(" ", 1)[0] + " second@example",
    ]
    baseline_path = tmp_path / "baseline.toml"
    redundant_path = tmp_path / "redundant.toml"
    baseline_path.write_text(tomli_w.dumps(baseline_document))
    redundant_path.write_text(tomli_w.dumps(redundant_document))

    baseline = load_validate_config_result(baseline_path)
    redundant = load_validate_config_result(redundant_path)

    assert [item.code for item in redundant.warnings] == [
        "ssh.redundant_public_key",
        "system.redundant_default_apt_package",
    ]
    assert redundant.config.system.extra_packages == ["bash"]
    assert len(redundant.config.system.ssh.pub_keys) == 3
    assert redundant.domains.apt_packages == baseline.domains.apt_packages == ()
    assert redundant.domains.ssh_public_keys == baseline.domains.ssh_public_keys

    resolution = accepted_resolution()
    baseline_graph = request_graph(baseline.config, resolution)
    redundant_graph = request_graph(redundant.config, resolution)
    assert redundant_graph == baseline_graph
    assert redundant_graph.application.os_packages.count("bash") == 1
    assert redundant_graph.runtime.ssh.pub_keys == (_VALID_SSH_KEY,)

    baseline_plan = build_plan(baseline.config, resolution)
    redundant_plan = build_plan(redundant.config, resolution)
    assert redundant_plan == baseline_plan
    assert redundant_plan.runtime.ssh.pub_keys == (_VALID_SSH_KEY,)


def test_config_lock_identity_mismatch_fails_construction() -> None:
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    data["images"]["cuda"]["tag"] = "12.9.2-cudnn-devel-ubuntu24.04"
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(data),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    with pytest.raises(ValueError, match="CUDA image"):
        build_plan(final_config(), changed)


def test_build_plan_requires_exact_runtime_planning_provenance() -> None:
    config = final_config()
    resolution = accepted_resolution()
    graph = request_graph(config, resolution)

    with pytest.raises(TypeError, match="runtime_provenance"):
        _construct_build_plan(graph, resolution.lock)

    with pytest.raises(ValueError, match="downloader provenance"):
        _construct_build_plan(
            graph,
            resolution.lock,
            runtime_provenance=RuntimePlanningProvenance(
                failure_policy_explicit=False,
                file_downloader_explicit=(),
                file_download_mode_explicit=(False,),
            ),
        )

    with pytest.raises(ValueError, match="download-mode provenance"):
        _construct_build_plan(
            graph,
            resolution.lock,
            runtime_provenance=RuntimePlanningProvenance(
                failure_policy_explicit=False,
                file_downloader_explicit=(False,),
                file_download_mode_explicit=(),
            ),
        )


def test_build_plan_rejects_requirements_source_that_differs_from_graph() -> None:
    config = final_config()
    resolution = accepted_resolution()
    graph = request_graph(config, resolution)
    data = resolution.lock.model_dump(mode="python")
    content = "torch\ntorchvision\n"
    data["comfyui"]["requirements"].update(
        content=content,
        digest=f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
    )
    mismatched_lock = CanonicalLock.model_validate(data)

    with pytest.raises(ValueError, match="lock does not match request graph"):
        _construct_build_plan(
            graph,
            mismatched_lock,
            runtime_provenance=RuntimePlanningProvenance(
                failure_policy_explicit=False,
                file_downloader_explicit=(False,),
                file_download_mode_explicit=(False,),
            ),
        )


def test_unused_lock_identity_is_rejected() -> None:
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    data["python"]["uv_tools"] += (
        UvToolLockEntry(
            request_digest=DIGEST_A,
            name="unused-tool",
            extras=(),
            version="1.0.0",
        ).model_dump(mode="python"),
    )
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(data),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    with pytest.raises(ValueError, match="unused identities"):
        build_plan(final_config(), changed)
