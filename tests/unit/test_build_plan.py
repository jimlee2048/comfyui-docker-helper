"""BuildPlan construction and binding contracts."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError
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

from comfyui_docker_helper.config import canonical_request as canonical_request_module
from comfyui_docker_helper.config.build_plan import (
    BUILD_PLAN_SCHEMA_VERSION,
    BuildPlan,
    ExactPackagePlan,
    ManifestBinding,
    RuntimePlanningProvenance,
    build_plan_digest,
    dump_build_plan_json,
    manifest_binding,
    parse_build_plan_json,
)
from comfyui_docker_helper.config.build_plan import (
    construct_build_plan as _construct_build_plan,
)
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    UvToolLockEntry,
)
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_validation import (
    validate_final_config_domains,
    validate_final_config_semantics,
    validate_final_config_structure,
)
from comfyui_docker_helper.exact_ledger import (
    UV_IMAGE_REPOSITORY,
)


# BuildPlan projection binds every execution input to admitted immutable authorities.
def test_constructor_consumes_exact_authorities_and_orders_values() -> None:
    plan = build_plan(final_config(), accepted_resolution())

    assert plan.schema_version == BUILD_PLAN_SCHEMA_VERSION
    assert plan.toolchain.cuda_image.reference == (
        f"nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04@{DIGEST_A}"
    )
    assert plan.toolchain.uv_image.reference == (
        f"{UV_IMAGE_REPOSITORY}:0.11.28@{DIGEST_B}"
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
    assert plan.toolchain.tool_store.cdh.wheel_digest == canonical_wheel().digest


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


def test_plan_round_trip_is_strict_and_immutable() -> None:
    plan = build_plan(final_config(), accepted_resolution())

    assert parse_build_plan_json(dump_build_plan_json(plan)) == plan
    with pytest.raises(ValidationError, match="frozen"):
        plan.config_digest = DIGEST_A

    document = plan.model_dump(mode="json")
    document["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BuildPlan.model_validate(document)


def test_plan_and_manifest_bind_config_lock_and_plan_without_request_digests() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    binding = manifest_binding(plan)
    serialized = dump_build_plan_json(plan)

    assert binding.build_plan_digest == build_plan_digest(plan)
    assert binding.config_digest == plan.config_digest
    assert binding.lock_digest == plan.lock_digest
    assert ManifestBinding.model_validate_json(binding.model_dump_json()) == binding
    assert b"request_digest" not in serialized
    assert b"config.lock" not in serialized
    assert b"host" not in serialized


def test_execution_only_config_change_updates_binding_deterministically() -> None:
    config = final_config()
    changed_document = config.model_dump(mode="python")
    changed_document["build"]["tags"] = ["example:changed"]
    changed = FinalConfig.model_validate(changed_document)

    first = build_plan(config, accepted_resolution())
    second = build_plan(changed, accepted_resolution())

    assert first.config_digest != second.config_digest
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
        "environment": "application",
    }
    document[field] = value

    with pytest.raises(ValidationError, match=message):
        ExactPackagePlan.model_validate(document)


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
# an exact image tag must still agree with its observed resolved version.
@pytest.mark.parametrize(
    ("tag", "resolved_version"),
    [("latest", "0.11.29"), ("0.11.29", "0.11.29")],
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
    uv_image["tag"] = "0.11.29"
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
        document["config_digest"] = "bad"

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
        document["toolchain"]["uv_image"]["repository"] = "ghcr.io/attacker/uv"
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
