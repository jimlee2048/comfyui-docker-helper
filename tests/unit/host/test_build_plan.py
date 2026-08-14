"""BuildPlan construction and binding contracts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
import tomli_w
from pydantic import ValidationError

from comfyui_docker_helper.config import build_plan as build_plan_module
from comfyui_docker_helper.config import canonical_request as canonical_request_module
from comfyui_docker_helper.config.build_plan import (
    BUILD_PLAN_SCHEMA_VERSION,
    BuildPlan,
    CustomNodesPhase,
    DownloaderCredentialRoutePlan,
    ExactPackagePlan,
    GitCredentialRoutePlan,
    ManifestBinding,
    RuntimePlanningProvenance,
    UvToolPlan,
    build_plan_digest,
    downloader_credential_secret_ids,
    dump_build_plan_json,
    git_credential_secret_ids,
    manifest_binding,
    parse_build_plan_json,
)
from comfyui_docker_helper.config.build_plan import (
    construct_build_plan as _construct_build_plan,
)
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    DirectPythonRequestIdentity,
    LocalFileLockEntry,
    PyTorchRequestIdentity,
    UvToolLockEntry,
    canonical_lock_from_entries,
)
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_validation import (
    validate_final_config_domains,
    validate_final_config_semantics,
    validate_final_config_structure,
)
from comfyui_docker_helper.config.service import load_validate_config_result
from comfyui_docker_helper.exact_ledger import (
    UV_IMAGE_REPOSITORY,
)
from comfyui_docker_helper.version import package_version
from tests.build_plan_support import (
    COMMIT_B,
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    accepted_resolution,
    build_plan,
    canonical_wheel,
    final_config,
    request_graph,
)

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


# BuildPlan projection binds every execution input to admitted immutable authorities.
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


# Serialized node and hook authorities reject unsafe execution combinations.
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


def test_build_plan_parser_rejects_registry_without_manager_or_unique_identity() -> (
    None
):
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["application"]["comfyui"]["manager"] = None
    document["custom_nodes"]["install_manager"] = False

    with pytest.raises(ValidationError, match="Registry nodes require Manager"):
        parse_build_plan_json(json.dumps(document))

    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    duplicate = dict(document["custom_nodes"]["nodes"][0])
    duplicate["id"] = "Registry_Node"
    document["custom_nodes"]["nodes"] = (
        *document["custom_nodes"]["nodes"],
        duplicate,
    )

    with pytest.raises(ValidationError, match="identities must be unique"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_enforces_complete_hook_tree_identity() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["custom_nodes"]["nodes"][0]["pre_install_hooks"] = (
        {"relative_path": "hooks/install.txt", "digest": DIGEST_A},
    )
    with pytest.raises(ValidationError, match=r"must end in \.sh or \.py"):
        parse_build_plan_json(json.dumps(document))

    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["custom_nodes"]["nodes"][0]["pre_install_hooks"] = (
        {"relative_path": "hooks/install.py", "digest": DIGEST_A},
    )
    document["custom_nodes"]["nodes"][1]["post_install_hooks"] = (
        {"relative_path": "hooks/install.py", "digest": DIGEST_B},
    )
    with pytest.raises(ValidationError, match="conflicting digests"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_accepts_reused_build_hook_and_separate_tree_path() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    hook = {"relative_path": "pre-start.d/shared.py", "digest": DIGEST_A}
    document["custom_nodes"]["nodes"][0]["pre_install_hooks"] = (hook,)
    document["custom_nodes"]["nodes"][1]["post_install_hooks"] = (hook,)
    document["runtime"]["hooks"] = (hook,)

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.custom_nodes.nodes[0].pre_install_hooks[0].digest == DIGEST_A
    assert parsed.runtime.hooks[0].relative_path == "pre-start.d/shared.py"


@pytest.mark.parametrize(
    "relative_path",
    ["unknown.d/hook.sh", "pre-start.d/nested/hook.sh"],
)
def test_build_plan_parser_rejects_invalid_runtime_hook_identity(
    relative_path: str,
) -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["runtime"]["hooks"] = (
        {"relative_path": relative_path, "digest": DIGEST_A},
    )

    with pytest.raises(ValidationError, match="phase directory and filename"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_rejects_duplicate_runtime_hook_identity() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    hook = {"relative_path": "stop.d/cleanup.sh", "digest": DIGEST_A}
    document["runtime"]["hooks"] = (hook, hook)

    with pytest.raises(ValidationError, match="runtime hook identities must be unique"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_binds_registry_user_directory_to_comfyui() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["custom_nodes"]["user_directory"] = "/workspace/other/user"

    with pytest.raises(ValidationError, match="user directory does not match"):
        BuildPlan.model_validate(document)


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


# Package ownership remains disjoint across application and isolated tool environments.
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


# Canonical bytes and manifests bind exact config/lock/plan identity deterministically.
def test_plan_bytes_digest_and_lock_order_are_deterministic() -> None:
    first = build_plan(final_config(), accepted_resolution())
    second = build_plan(final_config(), accepted_resolution(reverse=True))

    assert first == second
    assert dump_build_plan_json(first) == dump_build_plan_json(second)
    assert build_plan_digest(first) == build_plan_digest(second)


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
    first_document["files"][0]["target_dir"] = "./models//checkpoints/"
    second_document["files"][0]["target_dir"] = "models/checkpoints"
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


def test_local_file_locator_is_not_serialized_and_slot_depends_only_on_target() -> None:
    first_document = final_config().model_dump(mode="json", exclude_none=True)
    first_document["files"] = [
        {
            "type": "local",
            "path": "/private/first-model.bin",
            "target_dir": "models",
            "filename": "model.bin",
            "content_lock": False,
        }
    ]
    second_document = deepcopy(first_document)
    second_document["files"][0]["path"] = "/other/private-model.bin"
    first_config = validate_final_config_structure(first_document)
    second_config = validate_final_config_structure(second_document)
    resolution = accepted_resolution()

    first_graph = request_graph(first_config, resolution)
    second_graph = request_graph(second_config, resolution)

    assert first_graph == second_graph
    assert "/private/" not in repr(first_graph)
    assert first_graph.files[0].context_path.startswith("build/files/")


def test_local_file_plan_consumes_only_locked_content_identity() -> None:
    document = final_config().model_dump(mode="json", exclude_none=True)
    document["files"] = [
        {
            "type": "local",
            "path": "model.bin",
            "target_dir": "models",
            "filename": "model.bin",
            "content_lock": True,
        }
    ]
    config = validate_final_config_structure(document)
    resolution = accepted_resolution()
    lock = canonical_lock_from_entries(
        [
            *resolution.lock.entries,
            LocalFileLockEntry(
                relative_target="models/model.bin",
                digest=DIGEST_A,
            ),
        ]
    )
    locked_resolution = AcceptedCanonicalLock(lock, (), False, (), ())

    item = build_plan(config, locked_resolution).files.files[0]

    assert item.type == "local"
    assert item.verification == "sha256"
    assert item.digest == DIGEST_A


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


def test_plan_round_trip_is_strict_and_immutable() -> None:
    plan = build_plan(final_config(), accepted_resolution())

    assert parse_build_plan_json(dump_build_plan_json(plan)) == plan
    with pytest.raises(ValidationError, match="frozen"):
        plan.image_config_digest = DIGEST_A

    document = plan.model_dump(mode="json")
    document["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BuildPlan.model_validate(document)


def test_plan_and_manifest_bind_image_config_and_lock_without_requests() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    binding = manifest_binding(plan)
    serialized = dump_build_plan_json(plan)

    assert binding.build_plan_digest == build_plan_digest(plan)
    assert binding.image_config_digest == plan.image_config_digest
    assert binding.lock_digest == plan.lock_digest
    assert ManifestBinding.model_validate_json(binding.model_dump_json()) == binding
    assert b"request_digest" not in serialized
    assert b"config.lock" not in serialized
    assert b"host" not in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    [("tags", ["example:changed"]), ("output", "push")],
)
def test_publication_only_config_change_does_not_change_image_plan(
    field: str, value: object
) -> None:
    config = final_config()
    changed_document = config.model_dump(mode="python")
    changed_document["build"][field] = value
    changed = FinalConfig.model_validate(changed_document)

    first = build_plan(config, accepted_resolution())
    second = build_plan(changed, accepted_resolution())

    assert request_graph(config, accepted_resolution()) == request_graph(
        changed, accepted_resolution()
    )
    assert first == second
    assert dump_build_plan_json(first) == dump_build_plan_json(second)


def test_secret_sources_and_unused_definitions_do_not_change_image_plan() -> None:
    base_document = final_config().model_dump(mode="python")
    base_document["secrets"] = {
        "private_git": {"env": "FIRST_TOKEN"},
        "unused": {"file": "/first/unused"},
    }
    base_document["cdh"]["git"]["credentials"] = [
        {
            "match": "https://EXAMPLE.com:443/team/",
            "username": "token-user",
            "password": {"secret": "private_git"},
        }
    ]
    changed_document = deepcopy(base_document)
    changed_document["secrets"] = {
        "private_git": {"file": "../second-token"},
        "another_unused": {"env": "OTHER_TOKEN"},
    }
    changed_document["cdh"]["git"]["credentials"][0]["match"] = (
        "https://example.com/team"
    )
    first_config = FinalConfig.model_validate(base_document)
    second_config = FinalConfig.model_validate(changed_document)

    first = build_plan(first_config, accepted_resolution())
    second = build_plan(second_config, accepted_resolution())

    assert first.image_config_digest == second.image_config_digest
    assert first == second


def test_git_credential_routes_project_only_safe_ordered_plan_metadata() -> None:
    document = final_config().model_dump(mode="python")
    document["secrets"] = {
        "shared": {"env": "SYNTHETIC_SHARED_TOKEN"},
        "other": {"file": "/synthetic/private-token"},
    }
    document["cdh"]["git"]["credentials"] = [
        {
            "match": "https://EXAMPLE.com:443/team/",
            "username": "first-user",
            "password": {"secret": "shared"},
        },
        {
            "match": "https://example.com/team/subgroup/",
            "username": "second-user",
            "password": {"secret": "shared"},
        },
        {
            "match": "http://git.example.com:80/other/",
            "username": "third-user",
            "password": {"secret": "other"},
        },
    ]

    plan = build_plan(
        FinalConfig.model_validate(document),
        accepted_resolution(),
    )

    assert [
        route.model_dump(mode="python") for route in plan.custom_nodes.git_credentials
    ] == [
        {
            "match": "https://example.com/team",
            "username": "first-user",
            "secret_id": "cdh-git-credential-shared",
        },
        {
            "match": "https://example.com/team/subgroup",
            "username": "second-user",
            "secret_id": "cdh-git-credential-shared",
        },
        {
            "match": "http://git.example.com/other",
            "username": "third-user",
            "secret_id": "cdh-git-credential-other",
        },
    ]
    assert git_credential_secret_ids(plan.custom_nodes) == (
        "cdh-git-credential-shared",
        "cdh-git-credential-other",
    )
    serialized = dump_build_plan_json(plan)
    assert b"SYNTHETIC_SHARED_TOKEN" not in serialized
    assert b"/synthetic/private-token" not in serialized
    assert parse_build_plan_json(serialized) == plan


def test_git_credential_secret_projection_is_inert_without_direct_git_nodes() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    phase = CustomNodesPhase(
        install_manager=plan.custom_nodes.install_manager,
        user_directory=plan.custom_nodes.user_directory,
        nodes=tuple(
            node for node in plan.custom_nodes.nodes if node.type == "registry"
        ),
        git_credentials=(
            GitCredentialRoutePlan(
                match="https://example.com/team",
                username="token-user",
                secret_id="cdh-git-credential-private_git",
            ),
        ),
    )

    assert git_credential_secret_ids(phase) == ()


def test_git_credential_plan_revalidates_protocol_bounds_and_unique_contexts() -> None:
    maximum_username = "é" * 32_762 + "a"
    route = GitCredentialRoutePlan(
        match="https://example.com/team",
        username=maximum_username,
        secret_id="cdh-git-credential-private_git",
    )

    assert len(route.username.encode("utf-8")) == 65_525
    with pytest.raises(ValidationError, match="username is invalid"):
        GitCredentialRoutePlan(
            match=route.match,
            username=maximum_username + "a",
            secret_id=route.secret_id,
        )
    with pytest.raises(ValidationError, match="Secret ID must be canonical"):
        GitCredentialRoutePlan(
            match=route.match,
            username="token-user",
            secret_id="private_git",
        )

    plan = build_plan(final_config(), accepted_resolution())
    with pytest.raises(ValidationError, match="match contexts must be unique"):
        CustomNodesPhase(
            install_manager=plan.custom_nodes.install_manager,
            user_directory=plan.custom_nodes.user_directory,
            nodes=plan.custom_nodes.nodes,
            git_credentials=(route, route),
        )


def test_downloader_credential_routes_project_only_safe_files_metadata() -> None:
    document = final_config().model_dump(mode="python")
    document["secrets"] = {
        "shared": {"env": "SYNTHETIC_MODEL_TOKEN"},
        "other": {"file": "/synthetic/private-token"},
    }
    document["cdh"]["default_downloader"] = "httpx"
    document["cdh"]["downloader"]["credentials"] = [
        {
            "match": "https://EXAMPLE.test:443/",
            "type": "bearer",
            "token": {"secret": "shared"},
        },
        {
            "match": "https://example.test/private/",
            "type": "bearer",
            "token": {"secret": "other"},
        },
    ]

    plan = build_plan(FinalConfig.model_validate(document), accepted_resolution())

    assert [route.model_dump(mode="python") for route in plan.files.credentials] == [
        {
            "match": "https://example.test/",
            "type": "bearer",
            "token": {"secret": "shared"},
            "secret_id": "cdh-downloader-credential-shared",
        },
        {
            "match": "https://example.test/private",
            "type": "bearer",
            "token": {"secret": "other"},
            "secret_id": "cdh-downloader-credential-other",
        },
    ]
    assert downloader_credential_secret_ids(plan.files) == (
        "cdh-downloader-credential-shared",
        "cdh-downloader-credential-other",
    )
    serialized = dump_build_plan_json(plan)
    assert b"SYNTHETIC_MODEL_TOKEN" not in serialized
    assert b"/synthetic/private-token" not in serialized
    assert parse_build_plan_json(serialized) == plan


def test_downloader_secret_locator_is_excluded_but_route_reference_is_identity() -> (
    None
):
    document = final_config().model_dump(mode="python")
    document["secrets"] = {
        "first": {"env": "FIRST_TOKEN"},
        "second": {"env": "SECOND_TOKEN"},
    }
    document["cdh"]["default_downloader"] = "httpx"
    document["cdh"]["downloader"]["credentials"] = [
        {
            "match": "https://EXAMPLE.test:443/",
            "type": "bearer",
            "token": {"secret": "first"},
        }
    ]
    locator_changed = deepcopy(document)
    locator_changed["secrets"]["first"] = {"file": "/other/token"}
    locator_changed["cdh"]["downloader"]["credentials"][0]["match"] = (
        "https://example.test"
    )
    reference_changed = deepcopy(document)
    reference_changed["cdh"]["downloader"]["credentials"][0]["token"] = {
        "secret": "second"
    }

    first = build_plan(FinalConfig.model_validate(document), accepted_resolution())
    same = build_plan(
        FinalConfig.model_validate(locator_changed), accepted_resolution()
    )
    changed = build_plan(
        FinalConfig.model_validate(reference_changed), accepted_resolution()
    )

    assert first == same
    assert first.image_config_digest == same.image_config_digest
    assert first.image_config_digest != changed.image_config_digest
    assert build_plan_digest(first) != build_plan_digest(changed)


def test_downloader_credential_plan_revalidates_routes_and_httpx_requirement() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    route = DownloaderCredentialRoutePlan(
        match="https://unmatched.example/models",
        type="bearer",
        token={"secret": "model_read"},
        secret_id="cdh-downloader-credential-model_read",
    )
    phase = plan.files.model_copy(update={"credentials": (route,)})
    assert downloader_credential_secret_ids(phase) == ()

    with pytest.raises(ValidationError, match="match routes must be unique"):
        type(plan.files)(
            downloader=plan.files.downloader,
            credentials=(route, route),
            default_download_mode=plan.files.default_download_mode,
            download_max_attempts=plan.files.download_max_attempts,
            files=plan.files.files,
        )

    matching = DownloaderCredentialRoutePlan(
        match="https://example.test/",
        type="bearer",
        token=route.token,
        secret_id=route.secret_id,
    )
    with pytest.raises(ValidationError, match="reference and mount ID must agree"):
        DownloaderCredentialRoutePlan(
            match="https://example.test/",
            type="bearer",
            token={"secret": "other"},
            secret_id=route.secret_id,
        )
    with pytest.raises(ValidationError, match="require the HTTPX downloader"):
        type(plan.files)(
            downloader=plan.files.downloader,
            credentials=(matching,),
            default_download_mode=plan.files.default_download_mode,
            download_max_attempts=plan.files.download_max_attempts,
            files=plan.files.files,
        )


@pytest.mark.parametrize("field", ["match", "username", "password"])
def test_effective_git_credential_behavior_changes_image_identity(field: str) -> None:
    document = final_config().model_dump(mode="python")
    document["secrets"] = {
        "first": {"env": "FIRST_TOKEN"},
        "second": {"env": "SECOND_TOKEN"},
    }
    document["cdh"]["git"]["credentials"] = [
        {
            "match": "https://example.com/team/",
            "username": "token-user",
            "password": {"secret": "first"},
        }
    ]
    changed_document = deepcopy(document)
    route = changed_document["cdh"]["git"]["credentials"][0]
    if field == "match":
        route["match"] = "https://example.com/other/"
    elif field == "username":
        route["username"] = "other-user"
    else:
        route["password"] = {"secret": "second"}

    first = build_plan(FinalConfig.model_validate(document), accepted_resolution())
    second = build_plan(
        FinalConfig.model_validate(changed_document), accepted_resolution()
    )

    assert first.image_config_digest != second.image_config_digest


def test_image_config_change_updates_binding_deterministically() -> None:
    config = final_config()
    changed_document = config.model_dump(mode="python")
    changed_document["system"]["env"]["IMAGE_INPUT"] = "changed"
    changed = FinalConfig.model_validate(changed_document)

    first = build_plan(config, accepted_resolution())
    second = build_plan(changed, accepted_resolution())

    assert first.image_config_digest != second.image_config_digest
    assert first.lock_digest == second.lock_digest
    assert build_plan_digest(first) != build_plan_digest(second)


# BuildPlan construction admits only a grouped lock that satisfies the request graph.
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


# An admitted PyTorch group must still preserve its requested backend channel.
def test_build_plan_constructor_rejects_core_channel_mismatch() -> None:
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    data["python"]["package_groups"]["pytorch"]["packages"][0]["version"] = (
        "2.12.1+cu129"
    )
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(data),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    with pytest.raises(ValueError, match="canonical package does not satisfy"):
        build_plan(final_config(), changed)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "Torch", "normalized distribution name"),
        ("name", "torch.core", "normalized distribution name"),
        ("extras", ("Image_Preview",), "sorted, unique, and normalized"),
        ("extras", ("image", "image"), "sorted, unique, and normalized"),
        ("extras", ("preview", "image"), "sorted, unique, and normalized"),
    ],
)
def test_exact_package_plan_rejects_noncanonical_pep503_identity(
    field: str, value: str | tuple[str, ...], message: str
) -> None:
    document = {
        "name": "torch",
        "extras": (),
        "version": "2.12.1+cu130",
        "direct_reference": None,
        "environment": "application",
    }
    document[field] = value

    with pytest.raises(ValidationError, match=message):
        ExactPackagePlan.model_validate(document)


@pytest.mark.parametrize("version", ["1.0rc1", "1.0.dev1", "1.0+cu130"])
def test_user_package_and_tool_plans_accept_canonical_pep440_versions(
    version: str,
) -> None:
    package = ExactPackagePlan(
        name="demo",
        extras=(),
        version=version,
        direct_reference=None,
        environment="application",
    )
    tool = UvToolPlan(
        name="demo-tool",
        extras=(),
        version=version,
        direct_reference=None,
        environment="uv-tool:demo-tool",
    )

    assert package.version == version
    assert tool.version == version


@pytest.mark.parametrize(
    ("model", "document"),
    [
        (
            ExactPackagePlan,
            {
                "name": "demo",
                "extras": (),
                "version": "1.0",
                "direct_reference": "file:///tmp/demo.whl",
                "environment": "application",
            },
        ),
        (
            UvToolPlan,
            {
                "name": "demo-tool",
                "extras": (),
                "version": "1.0",
                "direct_reference": "https://user@example.test/demo.whl",
                "environment": "uv-tool:demo-tool",
            },
        ),
    ],
)
def test_user_package_plans_reject_unadmitted_direct_sources(
    model: type[ExactPackagePlan] | type[UvToolPlan],
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="admitted package source"):
        model.model_validate(document)


def test_build_plan_rejects_protected_pytorch_direct_source() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["application"]["pytorch"]["packages"][0]["direct_reference"] = (
        "https://example.test/torch.whl"
    )

    with pytest.raises(ValidationError, match="protected PyTorch packages"):
        BuildPlan.model_validate(document)


# Parser self-validation rejects semantic forgeries at the execution trust boundary.
def test_build_plan_admission_rejects_reserved_staging_final_leaf() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["files"]["files"][0]["target"] = "/workspace/ComfyUI/models/.cdh-staging"

    with pytest.raises(ValidationError, match="reserved staging filename"):
        BuildPlan.model_validate(document)


def test_build_plan_parser_rejects_forged_release_pip_authority() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["application"]["pip_version"] = "99.0.0"
    document["toolchain"]["python"]["pip_version"] = "99.0.0"

    with pytest.raises(ValidationError, match="pip version does not match"):
        parse_build_plan_json(json.dumps(document))


# Application roots stay distinct while arbitrary absolute non-equal layouts
# remain valid serialized execution plans.
def test_build_plan_parser_rejects_equal_workspace_and_comfyui_paths() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["application"]["paths"]["workspace"] = document["application"]["paths"][
        "comfyui"
    ]

    with pytest.raises(ValidationError, match="paths must be different"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_accepts_alternate_absolute_workspace_layout() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["application"]["paths"]["workspace"] = "/srv/work area"

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.application.paths.workspace == "/srv/work area"


# Runtime launch identity cannot diverge from the admitted application owners.
@pytest.mark.parametrize(
    ("index", "value"),
    [(0, "/usr/bin/python3"), (1, "/tmp/other-main.py")],
)
def test_build_plan_parser_binds_runtime_launch_identity_to_application(
    index: int,
    value: str,
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["runtime"]["launch_command"][index] = value

    with pytest.raises(ValidationError, match="must match the application"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_rejects_forged_python_catalog_binding() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["python"]["catalog_descriptor_digest"] = DIGEST_C

    with pytest.raises(ValidationError, match="catalog is not bound"):
        parse_build_plan_json(json.dumps(document))


# BuildPlan independently reuses the managed-Python catalog path-component
# admission rule before renderer path composition.
@pytest.mark.parametrize("catalog_key", ["..", "../python", "python/key", "key\\name"])
def test_build_plan_parser_rejects_unsafe_python_catalog_key(
    catalog_key: str,
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["python"]["catalog_key"] = catalog_key

    with pytest.raises(ValidationError, match="safe path component"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_preserves_safe_opaque_python_catalog_key() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["python"]["catalog_key"] = "alternate.catalog+key"

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.toolchain.python.catalog_key == "alternate.catalog+key"


# Container uv selection remains independent from release-owned uv-build, while
# cdh's exact Debian provider tag must agree with its observed uv release.
@pytest.mark.parametrize(
    ("tag", "resolved_version"),
    [("debian-slim", "0.11.29"), ("0.11.29-debian-slim", "0.11.29")],
)
def test_build_plan_parser_accepts_locked_uv_image_selector(
    tag: str, resolved_version: str
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    uv_image = document["toolchain"]["uv_image"]
    uv_image["tag"] = tag
    uv_image["resolved_version"] = resolved_version

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.toolchain.uv_image.tag == tag
    assert parsed.toolchain.uv_image.resolved_version == resolved_version


def test_build_plan_parser_rejects_exact_uv_image_version_mismatch() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    uv_image = document["toolchain"]["uv_image"]
    uv_image["tag"] = "0.11.29-debian-slim"
    uv_image["resolved_version"] = "0.11.30"

    with pytest.raises(ValidationError, match="does not match its exact tag"):
        parse_build_plan_json(json.dumps(document))


# Serialized toolchain identity enforces the package support range and exact
# release-owned cdh/uv-build values without a tested-profile allowlist.
@pytest.mark.parametrize("version", ["3.11.9", "3.15.0"])
def test_build_plan_parser_rejects_python_outside_package_support(
    version: str,
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["python"]["version"] = version

    with pytest.raises(ValidationError, match=r">=3\.12,<3\.15"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_accepts_unlisted_python_patch_inside_support() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    version = "3.13.15"
    document["toolchain"]["python"]["version"] = version
    document["application"]["pytorch"]["python_version"] = version
    document["application"]["python_extras"]["python_version"] = version
    document["application"]["comfyui"]["requirements"]["python_version"] = version

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.toolchain.python.version == version


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", "99.0.0", "cdh version does not match"),
        ("wheel_digest", "invalid", "digest must be sha256"),
    ],
)
def test_build_plan_parser_rejects_forged_cdh_wheel_identity(
    field: str, value: str, message: str
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["tool_store"]["cdh"][field] = value

    with pytest.raises(ValidationError, match=message):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_rejects_forged_core_channel() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    torch = next(
        package
        for package in document["application"]["pytorch"]["packages"]
        if package["name"] == "torch"
    )
    torch["version"] = "2.12.1+cu129"

    with pytest.raises(ValidationError, match="does not match the group channel"):
        BuildPlan.model_validate(document)


# Serialized application phases bind protected source ownership to the complete
# backend policy and to exact members of the resolved PyTorch group.
def test_build_plan_parser_rejects_protected_projection_member_without_result() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    packages = document["application"]["pytorch"]["packages"]
    document["application"]["pytorch"]["packages"] = [
        package for package in packages if package["name"] != "torchaudio"
    ]

    with pytest.raises(ValidationError, match="missing exact PyTorch results"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_rejects_cohesively_shrunk_protected_policy() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    requirements = document["application"]["comfyui"]["requirements"]
    requirements["protected_names"] = ["torch"]
    requirements["protected"] = [
        item for item in requirements["protected"] if item["package"] == "torch"
    ]
    document["application"]["pytorch"]["packages"] = [
        package
        for package in document["application"]["pytorch"]["packages"]
        if package["name"] == "torch"
    ]

    with pytest.raises(ValidationError, match="do not match the backend adapter"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_allows_adapter_member_absent_from_upstream_and_config() -> (
    None
):
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    requirements = document["application"]["comfyui"]["requirements"]
    requirements["protected"] = [
        item for item in requirements["protected"] if item["package"] != "torchaudio"
    ]
    document["application"]["pytorch"]["packages"] = [
        package
        for package in document["application"]["pytorch"]["packages"]
        if package["name"] != "torchaudio"
    ]

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.application.comfyui.requirements.protected_names == (
        "torch",
        "torchaudio",
        "torchvision",
    )


def test_build_plan_parser_allows_arbitrary_exact_pytorch_extra() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["application"]["pytorch"]["packages"].append(
        {
            "name": "xformers",
            "extras": (),
            "version": "0.0.35+cu130",
            "direct_reference": None,
            "environment": "application",
        }
    )

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.application.pytorch.packages[-1].name == "xformers"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("group-channel", "index must end"),
        ("group-index", "index must end"),
        ("group-python-index", "generic dependencies"),
        ("toolchain-channel", "target does not match"),
        ("toolchain-python", "target does not match"),
        ("duplicate-package", "packages must be unique"),
        ("case-variant-duplicate", "normalized distribution name"),
        ("missing-torch", "packages must be unique"),
        ("invalid-setuptools", "Invalid specifier"),
    ],
)
def test_build_plan_parser_rejects_cross_field_authority_forgery(
    mutation: str, message: str
) -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    pytorch = document["application"]["pytorch"]
    if mutation == "group-channel":
        pytorch["channel"] = "cu129"
    elif mutation == "group-index":
        pytorch["pytorch_index_url"] = "https://download.pytorch.org/whl/cu129"
    elif mutation == "group-python-index":
        pytorch["python_index_url"] = "https://index.example.test/simple"
    elif mutation == "toolchain-channel":
        document["toolchain"]["pytorch_channel"] = "cu129"
    elif mutation == "toolchain-python":
        document["toolchain"]["python"]["version"] = "3.12.13"
    elif mutation == "duplicate-package":
        pytorch["packages"] = (pytorch["packages"][0], pytorch["packages"][0])
    elif mutation == "case-variant-duplicate":
        duplicate = dict(pytorch["packages"][0])
        duplicate["name"] = "Torch"
        pytorch["packages"] = (*pytorch["packages"], duplicate)
    elif mutation == "missing-torch":
        pytorch["packages"] = (pytorch["packages"][1],)
    else:
        pytorch["setuptools_specifier"] = "latest"

    with pytest.raises(ValidationError, match=message):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("oci-repository", "canonical OCI repository"),
        ("oci-digest", "digest must be sha256"),
        ("application-path", "canonical absolute POSIX path"),
        ("comfyui-repository", "canonical Git source URL"),
        ("registry-id", "argv-safe Registry ID"),
        ("git-target", "canonical absolute POSIX path"),
        ("file-url", "canonical HTTP"),
        ("launch-executable", "canonical absolute POSIX path"),
        ("shutdown-timeout", "must be a finite positive number or -1"),
        ("plan-digest", "digest must be sha256"),
    ],
)
def test_build_plan_parser_rejects_execution_sensitive_scalar_forgery(
    mutation: str, message: str
) -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    if mutation == "oci-repository":
        document["toolchain"]["cuda_image"]["repository"] = "Invalid/Repository"
    elif mutation == "oci-digest":
        document["toolchain"]["uv_image"]["descriptor_digest"] = "bad"
    elif mutation == "application-path":
        document["application"]["paths"]["workspace"] = "relative"
    elif mutation == "comfyui-repository":
        document["application"]["comfyui"]["repository"] = "file:///tmp/source"
    elif mutation == "registry-id":
        document["custom_nodes"]["nodes"][0]["id"] = "-unsafe"
    elif mutation == "git-target":
        document["custom_nodes"]["nodes"][1]["target"] = "../escape"
    elif mutation == "file-url":
        document["files"]["files"][0]["url"] = "file:///tmp/model"
    elif mutation == "launch-executable":
        command = document["runtime"]["launch_command"]
        document["runtime"]["launch_command"] = ("python", *command[1:])
    elif mutation == "shutdown-timeout":
        document["runtime"]["shutdown_timeout"] = "8"
    else:
        document["image_config_digest"] = "bad"

    with pytest.raises(ValidationError, match=message):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("cuda-repository", "cuda-base image repository does not match"),
        ("uv-repository", "uv-tool image repository does not match"),
        ("cuda-tag-grammar", "CUDA image tag must match"),
        ("cuda-derived-channel", "do not match toolchain"),
        ("comfyui-repository", "official ledger"),
        ("comfyui-release-floor", "below the supported floor"),
        ("git-target-sibling", "exact child of ComfyUI custom_nodes"),
        ("git-target-nested", "exact child of ComfyUI custom_nodes"),
        ("duplicate-git-target", "Git node targets must be unique"),
        ("file-target-outside", "strict descendants of ComfyUI"),
        ("duplicate-file-target", "file targets must be unique"),
        ("apt-option", "canonical package identity"),
        ("aria2-option", "canonical aria2 argument"),
        ("ssh-password-control", "must not contain control"),
        ("ssh-public-key", "canonical and unique"),
        ("ssh-public-key-duplicate", "canonical and unique"),
        ("launch-whitespace", "canonical argv values"),
    ],
)
def test_build_plan_rejects_syntactic_but_semantic_authority_forgery(
    mutation: str,
    message: str,
) -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    if mutation == "cuda-repository":
        document["toolchain"]["cuda_image"]["repository"] = "ghcr.io/attacker/cuda"
    elif mutation == "uv-repository":
        document["toolchain"]["uv_image"]["repository"] = "ghcr.io/astral-sh/uv"
    elif mutation == "cuda-tag-grammar":
        document["toolchain"]["cuda_image"]["tag"] = "13.0.3-cudnn-devel-ubuntu20.04"
    elif mutation == "cuda-derived-channel":
        document["toolchain"]["pytorch_channel"] = "cu999"
        document["application"]["pytorch"]["channel"] = "cu999"
        document["application"]["pytorch"]["pytorch_index_url"] = (
            "https://download.pytorch.org/whl/cu999"
        )
        for package in document["application"]["pytorch"]["packages"]:
            if package["name"] in {"torch", "torchvision"}:
                package["version"] = package["version"].replace("+cu130", "+cu999")
    elif mutation == "comfyui-repository":
        document["application"]["comfyui"]["repository"] = (
            "https://github.com/attacker/ComfyUI.git"
        )
    elif mutation == "comfyui-release-floor":
        document["application"]["comfyui"]["formal_release"] = "0.10.0"
    elif mutation == "git-target-sibling":
        document["custom_nodes"]["nodes"][1]["target"] = (
            "/workspace/ComfyUI/plugins/direct-node"
        )
    elif mutation == "git-target-nested":
        document["custom_nodes"]["nodes"][1]["target"] = (
            "/workspace/ComfyUI/custom_nodes/nested/direct-node"
        )
    elif mutation == "duplicate-git-target":
        duplicate = dict(document["custom_nodes"]["nodes"][1])
        duplicate["url"] = "https://example.test/other.git"
        document["custom_nodes"]["nodes"] = (
            *document["custom_nodes"]["nodes"],
            duplicate,
        )
    elif mutation == "file-target-outside":
        document["files"]["files"][0]["target"] = "/workspace/model.safetensors"
    elif mutation == "duplicate-file-target":
        document["files"]["files"] = (
            document["files"]["files"][0],
            document["files"]["files"][0],
        )
    elif mutation == "apt-option":
        document["application"]["os_packages"] = (
            *document["application"]["os_packages"],
            "--allow-unauthenticated",
        )
    elif mutation == "aria2-option":
        document["files"]["downloader"]["aria2"]["min_split_size"] = "--quiet"
    elif mutation == "ssh-password-control":
        document["runtime"]["ssh"]["password"] = "secret\ncommand"
    elif mutation == "ssh-public-key":
        document["runtime"]["ssh"]["pub_keys"] = ("ssh-ed25519 AAAA invalid",)
    elif mutation == "ssh-public-key-duplicate":
        document["runtime"]["ssh"]["pub_keys"] = (
            _VALID_SSH_KEY,
            _VALID_SSH_KEY.rsplit(" ", 1)[0] + " second@example",
        )
    else:
        command = document["runtime"]["launch_command"]
        document["runtime"]["launch_command"] = (*command, "   ")

    with pytest.raises(ValidationError, match=message):
        BuildPlan.model_validate(document)


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


def test_user_cannot_duplicate_cdh_owned_launch_argument() -> None:
    document = final_config().model_dump(mode="python")
    document["comfyui"]["extra_args"] = ["--disable-auto-launch"]
    config = FinalConfig.model_validate(document)

    domains = validate_final_config_domains(config)
    diagnostics = (
        *domains.diagnostics,
        *validate_final_config_semantics(config, domains),
    )

    assert [item.code for item in diagnostics] == ["comfyui.controlled_extra_arg"]
