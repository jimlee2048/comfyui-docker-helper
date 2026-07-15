"""BuildPlan construction and binding contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.comfyui_requirements import (
    CUDA_PROTECTED_REQUIREMENTS,
    merge_pytorch_requirements,
    protected_policy_digest,
)
from comfyui_docker_helper.config.build_plan import (
    BUILD_PLAN_SCHEMA_VERSION,
    BuildPlan,
    ExactPackagePlan,
    RuntimePlanningProvenance,
    build_plan_digest,
    dump_build_plan_json,
    manifest_binding,
    parse_build_plan_json,
    parse_manifest_binding_json,
)
from comfyui_docker_helper.config.build_plan import (
    construct_build_plan as _construct_build_plan,
)
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    ComfyCliLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectGitLockEntry,
    DirectPythonLockEntry,
    DirectPythonRequestMember,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    OciLockEntry,
    OfficialComfyUILockEntry,
    ProtectedRequirementProjection,
    PyTorchCompatibilityLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    canonical_entry_key,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_request import (
    CanonicalRequestGraph,
    build_canonical_request_graph,
)
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_validation import (
    validate_direct_requirement,
    validate_final_config,
    validate_final_config_structure,
)
from comfyui_docker_helper.exact_ledger import (
    COMFY_CLI_MINIMUM_VERSION,
    COMFYUI_FLOOR_COMMIT,
    COMFYUI_REPOSITORY,
    UV_IMAGE_REPOSITORY,
)
from comfyui_docker_helper.host.planning_authority import planning_release_inputs
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
    install_cli: bool = True,
    python_version: str = "3.13.14",
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
                "version": python_version,
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
                "version": "0.11.0",
                "install_cli": install_cli,
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
    install_cli: bool = True,
    reverse: bool = False,
    python_version: str = "3.13.14",
) -> AcceptedCanonicalLock:
    config = final_config(
        with_uv_tool=with_uv_tool,
        install_cli=install_cli,
        python_version=python_version,
    )
    configured_members: list[DirectPythonRequestMember] = []
    for index, value in enumerate(config.pytorch.extra_packages):
        diagnostics = []
        normalized = validate_direct_requirement(
            value, ("pytorch", "requirements", index), diagnostics
        )
        assert normalized is not None and not diagnostics
        configured_members.append(
            DirectPythonRequestMember(
                package=normalized.name,
                extras=list(normalized.extras),
                selector=normalized.specifier,
            )
        )
    upstream = (
        DirectPythonRequestMember(package="torch", extras=[], selector=""),
        DirectPythonRequestMember(package="torchaudio", extras=[], selector=""),
        DirectPythonRequestMember(package="torchvision", extras=[], selector=""),
    )
    pytorch_members = list(
        merge_pytorch_requirements(
            DirectPythonRequestMember(
                package="torch", extras=[], selector=f"=={config.pytorch.version}"
            ),
            upstream,
            tuple(configured_members),
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
            upstream_protected=[
                ProtectedRequirementProjection(
                    package=item.package,
                    extras=item.extras,
                    selector=item.selector,
                )
                for item in upstream
            ],
            members=pytorch_members,
        )
    )
    names = tuple(sorted(CUDA_PROTECTED_REQUIREMENTS))
    requirements_request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository=COMFYUI_REPOSITORY,
        commit=COMMIT_A,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
        python_version=config.python.version,
        platform="linux/amd64",
        protected_names=list(names),
        protected_policy_digest=protected_policy_digest(names),
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
            version=python_version,
            implementation="cpython",
            platform="linux/amd64",
            libc="gnu",
            provider="uv-managed",
            catalog_descriptor_digest=DIGEST_B,
            catalog_key=f"cpython-{python_version}-linux-x86_64-gnu",
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
            formal_release="0.11.0",
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
        DirectPythonLockEntry(
            type="python-package",
            request_digest=pytorch_digest,
            package="torchaudio",
            extras=[],
            version="2.11.0+cu130",
            environment="application",
        ),
        ComfyUIRequirementsLockEntry(
            type="comfyui-requirements",
            request_digest=compute_request_digest(requirements_request),
            repository=COMFYUI_REPOSITORY,
            commit=COMMIT_A,
            floor_commit=COMFYUI_FLOOR_COMMIT,
            path="requirements.txt",
            python_version=config.python.version,
            platform="linux/amd64",
            protected_names=list(names),
            protected_policy_digest=protected_policy_digest(names),
            requirements_digest=DIGEST_C,
            protected=[
                ProtectedRequirementProjection(
                    package=item.package,
                    extras=item.extras,
                    selector=item.selector,
                )
                for item in upstream
            ],
        ),
    ]
    if install_cli:
        cli_request = ComfyCliRequestIdentity(
            type="comfy-cli",
            package="comfy-cli",
            policy="highest-target-compatible-stable",
            minimum_version=COMFY_CLI_MINIMUM_VERSION,
            environment="uv-tool:comfy-cli",
            index_url=config.python.index_url,
            python_version=config.python.version,
            platform="linux/amd64",
        )
        entries.insert(
            4,
            ComfyCliLockEntry(
                type="comfy-cli",
                request_digest=compute_request_digest(cli_request),
                package="comfy-cli",
                version="1.8.0",
                environment="uv-tool:comfy-cli",
            ),
        )
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
    staged = {canonical_entry_key(entry): entry for entry in entries}
    graph = build_canonical_request_graph(
        config,
        release=planning_release_inputs(config.python.version),
        uv_descriptor_digest=DIGEST_B,
        comfyui_entry=staged[("comfyui", COMFYUI_REPOSITORY)],
        requirements_entry=staged[("comfyui-requirements", COMFYUI_REPOSITORY)],
    )
    digest_by_key = {
        key: desired.request_digest for desired in graph.desired for key in desired.keys
    }
    entries = [
        type(entry).model_validate(
            {
                **entry.model_dump(mode="python"),
                **(
                    {"request_digest": digest_by_key[canonical_entry_key(entry)]}
                    if hasattr(entry, "request_digest")
                    else {}
                ),
            }
        )
        for entry in entries
    ]
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


def request_graph(
    config: FinalConfig, resolution: AcceptedCanonicalLock
) -> CanonicalRequestGraph:
    entries = {canonical_entry_key(entry): entry for entry in resolution.lock.entries}
    return build_canonical_request_graph(
        config,
        release=planning_release_inputs(config.python.version),
        uv_descriptor_digest=entries[("oci", "uv-tool")].descriptor_digest,
        comfyui_entry=entries[("comfyui", COMFYUI_REPOSITORY)],
        requirements_entry=entries[("comfyui-requirements", COMFYUI_REPOSITORY)],
    )


def build_plan(
    config: FinalConfig,
    resolution: AcceptedCanonicalLock,
    **kwargs,
) -> BuildPlan:
    provenance = kwargs.pop(
        "runtime_provenance",
        RuntimePlanningProvenance(
            failure_policy_explicit=False,
            file_downloader_explicit=(False,) * len(config.files),
            file_download_mode_explicit=(False,) * len(config.files),
        ),
    )
    return _construct_build_plan(
        request_graph(config, resolution),
        resolution.lock,
        runtime_provenance=provenance,
        **kwargs,
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
    assert plan.application.inventory_path == (
        "/opt/cdh/build/application-inventory.txt"
    )
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
    assert plan.runtime.launch_command[-3:] == (
        "--disable-auto-launch",
        "--preview-method",
        "latent2rgb",
    )
    assert not hasattr(plan.custom_nodes.nodes[0], "target")
    assert plan.custom_nodes.user_directory == "/workspace/ComfyUI/user"
    assert plan.custom_nodes.custom_node_inventory == (
        "/opt/cdh/build/custom-node-inventory.json"
    )
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
    document["custom_nodes"]["nodes"][0]["pre_install"] = (
        {"relative_path": "hooks/install.txt", "digest": DIGEST_A},
    )
    with pytest.raises(ValidationError, match=r"must end in \.sh or \.py"):
        parse_build_plan_json(json.dumps(document))

    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["custom_nodes"]["nodes"][0]["pre_install"] = (
        {"relative_path": "hooks/install.py", "digest": DIGEST_A},
    )
    document["custom_nodes"]["nodes"][1]["post_install"] = (
        {"relative_path": "hooks/install.py", "digest": DIGEST_B},
    )
    with pytest.raises(ValidationError, match="conflicting digests"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_accepts_reused_custom_hook_and_separate_tree_path() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    hook = {"relative_path": "pre-start.d/shared.py", "digest": DIGEST_A}
    document["custom_nodes"]["nodes"][0]["pre_install"] = (hook,)
    document["custom_nodes"]["nodes"][1]["post_install"] = (hook,)
    document["runtime"]["hooks"] = (hook,)

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.custom_nodes.nodes[0].pre_install[0].digest == DIGEST_A
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
    assert plan.toolchain.tool_store.cdh_closure


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
    assert parse_manifest_binding_json(binding.model_dump_json()) == binding
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


@pytest.mark.parametrize(
    ("entry_index", "field", "value", "message"),
    [
        (0, "tag", "12.9.2-cudnn-devel-ubuntu24.04", "CUDA image"),
        (1, "tag", "latest", "uv image"),
        (2, "version", "3.12.13", "managed Python"),
        (3, "formal_release", "0.12.0", "ComfyUI identity"),
        (4, "request_digest", DIGEST_B, "comfy-cli identity"),
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
        build_plan(final_config(), changed)


# Construction rejects mismatched cohesive group and source-routing authorities.
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
        build_plan(final_config(), changed)


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
        build_plan(final_config(), changed)


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
    ("field", "message"),
    [
        ("cdh_version", "cdh version does not match"),
        ("uv_build_version", "uv-build version does not match"),
    ],
)
def test_build_plan_parser_rejects_forged_release_owned_python_input(
    field: str, message: str
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["python"][field] = "99.0.0"

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
    requirements["protected_policy_digest"] = protected_policy_digest(("torch",))
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


def test_build_plan_parser_rejects_wrong_protected_policy_digest() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["application"]["comfyui"]["requirements"]["protected_policy_digest"] = (
        DIGEST_C
    )

    with pytest.raises(ValidationError, match="digest does not match"):
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


def test_unused_lock_identity_is_rejected() -> None:
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    data["entries"] += (
        LocalExecutableLockEntry(
            type="local-executable",
            relative_path="unused.sh",
            digest=DIGEST_A,
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

    diagnostics = validate_final_config(config)

    assert [item.code for item in diagnostics] == ["comfyui.controlled_extra_arg"]
