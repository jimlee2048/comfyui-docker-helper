"""Focused BuildPlan construction and binding contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config.build_plan import (
    BUILD_PLAN_SCHEMA_VERSION,
    BuildPlan,
    ExactPackagePlan,
    build_plan_digest,
    construct_build_plan,
    dump_build_plan_json,
    manifest_binding,
    parse_build_plan_json,
    parse_manifest_binding_json,
)
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    ComfyCliLockEntry,
    DirectGitLockEntry,
    DirectPythonLockEntry,
    DirectPythonRequestMember,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    OciLockEntry,
    OfficialComfyUILockEntry,
    PyTorchCompatibilityLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_validation import (
    validate_direct_requirement,
    validate_final_config,
    validate_final_config_structure,
)
from comfyui_docker_helper.exact_ledger import COMFYUI_REPOSITORY, UV_IMAGE_REPOSITORY
from comfyui_docker_helper.release_artifacts import release_source_digest

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
DIGEST_C = f"sha256:{'c' * 64}"
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40


def final_config(
    *,
    scripts_dir: Path | None = None,
    with_hook: bool = False,
    with_uv_tool: bool = False,
) -> FinalConfig:
    registry_node: dict[str, object] = {
        "type": "registry",
        "id": "registry-node",
        "version": "1.2.3",
    }
    if with_hook:
        registry_node["pre_install_scripts"] = ["hooks/pre.py"]
    config = validate_final_config_structure(
        {
            "compute_platform": {"type": "cuda", "cuda": {"version": "13.0.3"}},
            "system": {
                "workspace": "/workspace",
                "extra_packages": ["ffmpeg"],
                "env": {"ZED": "last", "ALPHA": "first"},
                "ssh": {"enable": True, "port": 2222, "pub_keys": []},
            },
            "python": {
                "version": "3.13.14",
                "uv_version": "0.11.28",
                "extra_packages": ["NumPy>=2,<3"],
                "uv_tools": ["Ruff>=0.15,<0.16"] if with_uv_tool else [],
            },
            "pytorch": {
                "version": "2.12.1",
                "extra_packages": ["TorchVision[image]>=0.27,<0.28"],
            },
            "build": {
                "tags": ["example:test"],
                "output": "load",
                "platforms": ["linux/amd64"],
            },
            "comfyui": {
                "version": "0.4.0",
                "cli_version": "1.5.3",
                "install_manager": True,
                "extra_args": ["--preview-method", "latent2rgb"],
                "custom_nodes": [
                    registry_node,
                    {
                        "type": "git",
                        "url": "https://example.test/direct.git",
                        "ref": COMMIT_B,
                        "target_dir": "direct-node",
                    },
                ],
            },
            "files": [
                {
                    "url": "https://example.test/model.safetensors",
                    "dir": "models/checkpoints",
                    "filename": "model.safetensors",
                    "overwrite": True,
                }
            ],
        }
    )
    assert validate_final_config(config, scripts_dir=scripts_dir) == ()
    return config


def accepted_resolution(
    *,
    hook_digest: str | None = None,
    with_uv_tool: bool = False,
    reverse: bool = False,
) -> AcceptedCanonicalLock:
    config = final_config(with_uv_tool=with_uv_tool)
    pytorch_requirements = [
        f"torch=={config.pytorch.version}",
        *config.pytorch.extra_packages,
    ]
    pytorch_members: list[DirectPythonRequestMember] = []
    for index, value in enumerate(pytorch_requirements):
        diagnostics = []
        normalized = validate_direct_requirement(
            value, ("pytorch", "requirements", index), diagnostics
        )
        assert normalized is not None and not diagnostics
        pytorch_members.append(
            DirectPythonRequestMember(
                package=normalized.name,
                extras=list(normalized.extras),
                selector=normalized.specifier,
            )
        )
    pytorch_digest = compute_request_digest(
        PyTorchRequestIdentity(
            type="pytorch-group",
            environment="application",
            group="pytorch",
            backend="cuda",
            channel="cu130",
            python_version=config.python.version,
            platform="linux/amd64",
            python_index_url=config.python.index_url,
            pytorch_index_url=(f"{config.pytorch.index_base_url.rstrip('/')}/cu130"),
            members=pytorch_members,
        )
    )
    entries = [
        OciLockEntry(
            type="oci",
            request_digest=DIGEST_A,
            role="cuda-base",
            repository="nvidia/cuda",
            tag="13.0.3-cudnn-devel-ubuntu24.04",
            descriptor_digest=DIGEST_A,
            descriptor_kind="index",
            platform="linux/amd64",
        ),
        OciLockEntry(
            type="oci",
            request_digest=DIGEST_B,
            role="uv-tool",
            repository=UV_IMAGE_REPOSITORY,
            tag="0.11.28",
            descriptor_digest=DIGEST_B,
            descriptor_kind="index",
            platform="linux/amd64",
            resolved_version="0.11.28",
        ),
        ManagedPythonLockEntry(
            type="managed-python",
            request_digest=DIGEST_A,
            version="3.13.14",
            implementation="cpython",
            platform="linux/amd64",
            libc="gnu",
            provider="uv-managed",
            catalog_descriptor_digest=DIGEST_B,
            catalog_key="cpython-3.13.14-linux-x86_64-gnu",
            catalog_url="https://example.test/python.tar.zst",
            pip_version="26.1.2",
            cdh_version="0.5.0",
            cdh_source_digest=release_source_digest(),
            uv_build_version="0.11.28",
        ),
        OfficialComfyUILockEntry(
            type="comfyui",
            request_digest=DIGEST_A,
            repository=COMFYUI_REPOSITORY,
            commit=COMMIT_A,
            formal_release="0.4.0",
        ),
        ComfyCliLockEntry(
            type="comfy-cli",
            request_digest=DIGEST_A,
            package="comfy-cli",
            version="1.5.3",
            environment="application",
        ),
        DirectPythonLockEntry(
            type="python-package",
            request_digest=DIGEST_A,
            package="numpy",
            extras=[],
            version="2.3.1",
            environment="application",
        ),
        DirectPythonLockEntry(
            type="python-package",
            request_digest=pytorch_digest,
            package="torch",
            extras=[],
            version="2.12.1+cu130",
            environment="application",
        ),
        DirectPythonLockEntry(
            type="python-package",
            request_digest=pytorch_digest,
            package="torchvision",
            extras=["image"],
            version="0.27.1+cu130",
            environment="application",
        ),
        RegistryNodeLockEntry(
            type="registry",
            request_digest=DIGEST_A,
            id="registry-node",
            version="1.2.3",
        ),
        DirectGitLockEntry(
            type="git",
            request_digest=DIGEST_A,
            url="https://example.test/direct.git",
            commit=COMMIT_B,
        ),
        PyTorchCompatibilityLockEntry(
            type="pytorch-compatibility",
            request_digest=pytorch_digest,
            environment="application",
            setuptools_specifier="<82",
        ),
    ]
    if hook_digest is not None:
        entries.append(
            LocalExecutableLockEntry(
                type="local-executable",
                relative_path="custom-node-hooks/hooks/pre.py",
                digest=hook_digest,
            )
        )
    if with_uv_tool:
        entries.append(
            DirectPythonLockEntry(
                type="python-package",
                request_digest=DIGEST_C,
                package="ruff",
                extras=[],
                version="0.15.18",
                environment="uv-tool:ruff",
            )
        )
    if reverse:
        entries.reverse()
    lock = CanonicalLock(schema_version=1, entries=entries)
    return AcceptedCanonicalLock(
        lock=lock,
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )


def test_constructor_consumes_exact_authorities_and_orders_values() -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())

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
        "torchvision",
    ]
    assert plan.application.pytorch.packages[1].requirement == (
        "torchvision[image]==0.27.1+cu130"
    )
    assert plan.application.python_extras is not None
    assert plan.application.python_extras.packages[0].requirement == "numpy==2.3.1"
    assert [item.name for item in plan.runtime.environment] == ["ALPHA", "ZED"]
    assert plan.runtime.launch_command[-3:] == (
        "--disable-auto-launch",
        "--preview-method",
        "latent2rgb",
    )
    assert not hasattr(plan.custom_nodes.nodes[0], "target")
    assert plan.custom_nodes.nodes[1].target.endswith("/custom_nodes/direct-node")
    assert plan.files.files[0].target == (
        "/workspace/ComfyUI/models/checkpoints/model.safetensors"
    )


def test_constructor_projects_isolated_uv_tool_exact_result() -> None:
    plan = construct_build_plan(
        final_config(with_uv_tool=True), accepted_resolution(with_uv_tool=True)
    )

    assert len(plan.toolchain.tool_store.uv_tools) == 1
    tool = plan.toolchain.tool_store.uv_tools[0]
    assert tool.environment == "uv-tool:ruff"
    assert tool.requirement == "ruff==0.15.18"
    assert plan.toolchain.tool_store.cdh_closure


def test_plan_bytes_digest_and_lock_order_are_deterministic() -> None:
    first = construct_build_plan(final_config(), accepted_resolution())
    second = construct_build_plan(final_config(), accepted_resolution(reverse=True))

    assert first == second
    assert dump_build_plan_json(first) == dump_build_plan_json(second)
    assert build_plan_digest(first) == build_plan_digest(second)


def test_plan_round_trip_is_strict_and_immutable() -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())

    assert parse_build_plan_json(dump_build_plan_json(plan)) == plan
    with pytest.raises(ValidationError, match="frozen"):
        plan.config_digest = DIGEST_A

    document = plan.model_dump(mode="json")
    document["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BuildPlan.model_validate(document)


def test_plan_and_manifest_bind_config_lock_and_plan_without_request_digests() -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    binding = manifest_binding(plan)
    serialized = dump_build_plan_json(plan)

    assert binding.build_plan_digest == build_plan_digest(plan)
    assert binding.config_digest == plan.config_digest
    assert binding.lock_digest == plan.lock_digest
    assert parse_manifest_binding_json(binding.model_dump_json()) == binding
    assert b"request_digest" not in serialized
    assert b"config.lock" not in serialized
    assert b"host" not in serialized


def test_execution_only_config_change_updates_binding_deterministically() -> None:
    config = final_config()
    changed_document = config.model_dump(mode="python")
    changed_document["build"]["tags"] = ["example:changed"]
    changed = FinalConfig.model_validate(changed_document)

    first = construct_build_plan(config, accepted_resolution())
    second = construct_build_plan(changed, accepted_resolution())

    assert first.config_digest != second.config_digest
    assert first.lock_digest == second.lock_digest
    assert build_plan_digest(first) != build_plan_digest(second)


@pytest.mark.parametrize(
    ("entry_index", "field", "value", "message"),
    [
        (0, "tag", "12.9.2-cudnn-devel-ubuntu24.04", "CUDA image"),
        (1, "tag", "latest", "uv image"),
        (2, "version", "3.12.13", "managed Python"),
        (3, "formal_release", "0.5.0", "ComfyUI identity"),
        (4, "version", "1.5.4", "comfy-cli identity"),
        (6, "version", "2.11.0+cu130", "satisfy torch"),
        (8, "version", "1.2.4", "Registry identity"),
        (9, "commit", "3" * 40, "Git identity"),
    ],
)
def test_config_lock_identity_mismatches_fail_construction(
    entry_index: int,
    field: str,
    value: str,
    message: str,
) -> None:
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    data["entries"][entry_index][field] = value
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(data),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    with pytest.raises(ValueError, match=message):
        construct_build_plan(final_config(), changed)


@pytest.mark.parametrize("entry_index", [6, 7])
@pytest.mark.parametrize("version", ["2.12.1", "2.12.1+cpu", "2.12.1+cu129"])
def test_build_plan_constructor_rejects_core_channel_mismatch(
    entry_index: int, version: str
) -> None:
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    data["entries"][entry_index]["version"] = (
        version if entry_index == 6 else version.replace("2.12.1", "0.27.1")
    )
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(data),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    with pytest.raises(ValueError, match="does not match PyTorch channel"):
        construct_build_plan(final_config(), changed)


@pytest.mark.parametrize("entry_index", [6, 7, 10])
def test_build_plan_constructor_rejects_split_pytorch_request_digest(
    entry_index: int,
) -> None:
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    data["entries"][entry_index]["request_digest"] = DIGEST_C
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(data),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    with pytest.raises(ValueError, match="must share one request digest"):
        construct_build_plan(final_config(), changed)


def test_build_plan_constructor_rejects_cohesive_forged_pytorch_request_digest() -> (
    None
):
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    for entry_index in (6, 7, 10):
        data["entries"][entry_index]["request_digest"] = DIGEST_C
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(data),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    with pytest.raises(ValueError, match="must share one request digest"):
        construct_build_plan(final_config(), changed)


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


def test_build_plan_parser_rejects_forged_core_channel() -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    torch = next(
        package
        for package in document["application"]["pytorch"]["packages"]
        if package["name"] == "torch"
    )
    torch["version"] = "2.12.1+cu129"

    with pytest.raises(ValidationError, match="does not match the group channel"):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("group-channel", "index must end"),
        ("group-index", "index must end"),
        ("group-python-index", "generic dependencies"),
        ("toolchain-channel", "target does not match"),
        ("toolchain-python", "target does not match"),
        ("uv-resolved-version", "resolved version does not match"),
        ("duplicate-package", "packages must be unique"),
        ("case-variant-duplicate", "normalized distribution name"),
        ("missing-torch", "packages must be unique"),
        ("invalid-setuptools", "Invalid specifier"),
    ],
)
def test_build_plan_parser_rejects_cross_field_authority_forgery(
    mutation: str, message: str
) -> None:
    document = construct_build_plan(final_config(), accepted_resolution()).model_dump(
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
    elif mutation == "uv-resolved-version":
        document["toolchain"]["uv_image"]["resolved_version"] = "0.11.29"
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


def test_unused_lock_identity_is_rejected() -> None:
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    data["entries"].append(
        LocalExecutableLockEntry(
            type="local-executable",
            relative_path="unused.sh",
            digest=DIGEST_A,
        ).model_dump(mode="python")
    )
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(data),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    with pytest.raises(ValueError, match="unused identities"):
        construct_build_plan(final_config(), changed)


def test_active_uv_tools_and_remaining_deferred_fields_are_unambiguous() -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    document = dump_build_plan_json(plan)

    assert plan.toolchain.tool_store.uv_tools == ()
    assert b"uv_tools" in document
    for deferred in (b"checksum", b"shutdown_timeout", b"lifecycle"):
        assert deferred not in document
    assert plan.files.downloader.httpx.retries == 3


def test_user_cannot_duplicate_cdh_owned_launch_argument() -> None:
    document = final_config().model_dump(mode="python")
    document["comfyui"]["extra_args"] = ["--disable-auto-launch"]
    config = FinalConfig.model_validate(document)

    diagnostics = validate_final_config(config)

    assert [item.code for item in diagnostics] == ["comfyui.controlled_extra_arg"]
