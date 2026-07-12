"""Focused M2-T7 contracts for isolated BuildPlan construction and binding."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config.build_plan import (
    BUILD_PLAN_SCHEMA_VERSION,
    BuildPlan,
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
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    OciLockEntry,
    OfficialComfyUILockEntry,
    RegistryNodeLockEntry,
)
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_validation import (
    validate_final_config,
    validate_final_config_structure,
)
from comfyui_docker_helper.exact_ledger import COMFYUI_REPOSITORY, UV_IMAGE_REPOSITORY

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
DIGEST_C = f"sha256:{'c' * 64}"
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40


def final_config(
    *,
    scripts_dir: Path | None = None,
    with_hook: bool = False,
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
    reverse: bool = False,
) -> AcceptedCanonicalLock:
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
            setuptools_version="83.0.0",
            wheel_version="0.47.0",
            cdh_version="0.5.0",
            cdh_source_digest=DIGEST_C,
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
            request_digest=DIGEST_B,
            package="torch",
            extras=[],
            version="2.12.1+cu130",
            environment="application",
        ),
        DirectPythonLockEntry(
            type="python-package",
            request_digest=DIGEST_B,
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
    ]
    if hook_digest is not None:
        entries.append(
            LocalExecutableLockEntry(
                type="local-executable",
                relative_path="custom-node-hooks/hooks/pre.py",
                digest=hook_digest,
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


def test_build_plan_parser_rejects_forged_core_channel() -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    torch = next(
        package
        for package in document["application"]["pytorch"]["packages"]
        if package["name"] == "torch"
    )
    torch["version"] = "2.12.1+cu129"

    with pytest.raises(ValidationError, match="does not match the backend channel"):
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


def test_deferred_fields_are_absent_while_httpx_retries_remains_consumed() -> None:
    plan = construct_build_plan(final_config(), accepted_resolution())
    document = dump_build_plan_json(plan)

    for deferred in (b"uv_tools", b"checksum", b"shutdown_timeout", b"lifecycle"):
        assert deferred not in document
    assert plan.files.downloader.httpx.retries == 3


def test_user_cannot_duplicate_cdh_owned_launch_argument() -> None:
    document = final_config().model_dump(mode="python")
    document["comfyui"]["extra_args"] = ["--disable-auto-launch"]
    config = FinalConfig.model_validate(document)

    diagnostics = validate_final_config(config)

    assert [item.code for item in diagnostics] == ["comfyui.controlled_extra_arg"]
