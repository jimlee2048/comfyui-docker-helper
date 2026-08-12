"""Narrow shared factories for tests that consume a complete BuildPlan."""

from __future__ import annotations

import hashlib
from pathlib import Path

from comfyui_docker_helper.comfyui_requirements import merge_pytorch_requirements
from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    RuntimePlanningProvenance,
    construct_build_plan,
)
from comfyui_docker_helper.config.canonical_lock import (
    ApplicationExtrasLockEntry,
    BuildHookLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequirementsLockEntry,
    CudaImageLockEntry,
    DirectGitLockEntry,
    DirectPythonRequestMember,
    ManagedPythonLockEntry,
    OfficialComfyUILockEntry,
    ProtectedRequirementProjection,
    PyTorchLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    ResolvedPythonPackage,
    UvImageLockEntry,
    UvToolLockEntry,
    canonical_entry_key,
    canonical_lock_from_entries,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_request import (
    CanonicalRequestGraph,
    build_canonical_request_graph,
    comfyui_requirements_request,
)
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_validation import (
    validate_direct_requirement,
    validate_final_config_domains,
    validate_final_config_semantics,
    validate_final_config_structure,
)
from comfyui_docker_helper.exact_ledger import (
    COMFY_CLI_MINIMUM_VERSION,
    COMFYUI_REPOSITORY,
    UV_IMAGE_REPOSITORY,
)
from comfyui_docker_helper.host.planning_authority import planning_release_inputs
from comfyui_docker_helper.release_artifacts import CanonicalWheel
from comfyui_docker_helper.version import package_version

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
DIGEST_C = f"sha256:{'c' * 64}"
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40
CANONICAL_WHEEL_CONTENT = b"canonical cdh wheel test artifact"


def canonical_wheel() -> CanonicalWheel:
    version = package_version()
    return CanonicalWheel(
        filename=f"comfyui_docker_helper-{version}-py3-none-any.whl",
        version=version,
        digest=(f"sha256:{hashlib.sha256(CANONICAL_WHEEL_CONTENT).hexdigest()}"),
        content=CANONICAL_WHEEL_CONTENT,
    )


def final_config(
    *,
    build_hooks_dir: Path | None = None,
    with_hook: bool = False,
    with_uv_tool: bool = False,
    install_cli: bool = True,
    python_version: str = "3.13.14",
    shutdown_timeout: int | float = 8,
) -> FinalConfig:
    registry_node: dict[str, object] = {
        "type": "registry",
        "id": "registry-node",
        "version": "1.2.3",
    }
    if with_hook:
        registry_node["pre_install_hooks"] = ["hooks/pre.py"]
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
            "cdh": {"shutdown_timeout": shutdown_timeout},
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
                    "type": "http",
                    "url": "https://example.test/model.safetensors",
                    "target_dir": "models/checkpoints",
                    "filename": "model.safetensors",
                }
            ],
        }
    )
    domains = validate_final_config_domains(config, build_hooks_dir=build_hooks_dir)
    assert (
        *domains.diagnostics,
        *validate_final_config_semantics(config, domains),
    ) == ()
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
            resolver_descriptor_digest=DIGEST_B,
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
    comfyui_entry = OfficialComfyUILockEntry(
        request_digest=DIGEST_A,
        repository=COMFYUI_REPOSITORY,
        commit=COMMIT_A,
        formal_release="0.11.0",
    )
    requirements_request = comfyui_requirements_request(comfyui_entry)
    requirements_content = b"torch\ntorchaudio\ntorchvision\n"
    entries = [
        CudaImageLockEntry(
            request_digest=DIGEST_A,
            repository="nvidia/cuda",
            tag="13.0.3-cudnn-devel-ubuntu24.04",
            digest=DIGEST_A,
            kind="index",
            platform="linux/amd64",
        ),
        UvImageLockEntry(
            request_digest=DIGEST_B,
            repository=UV_IMAGE_REPOSITORY,
            tag="0.11.28-debian-slim",
            digest=DIGEST_B,
            kind="index",
            platform="linux/amd64",
            observed_version="0.11.28",
        ),
        ManagedPythonLockEntry(
            request_digest=DIGEST_A,
            version=python_version,
            platform="linux/amd64",
            libc="gnu",
            catalog_digest=DIGEST_B,
            artifact_key=f"cpython-{python_version}-linux-x86_64-gnu",
            artifact_url="https://example.test/python.tar.zst",
        ),
        comfyui_entry,
        ApplicationExtrasLockEntry(
            request_digest=DIGEST_A,
            packages=(ResolvedPythonPackage(name="numpy", extras=(), version="2.3.1"),),
        ),
        PyTorchLockEntry(
            request_digest=pytorch_digest,
            setuptools_specifier="<82",
            packages=(
                ResolvedPythonPackage(name="torch", extras=(), version="2.12.1+cu130"),
                ResolvedPythonPackage(
                    name="torchaudio", extras=(), version="2.11.0+cu130"
                ),
                ResolvedPythonPackage(
                    name="torchvision",
                    extras=("image",),
                    version="0.27.1+cu130",
                ),
            ),
        ),
        RegistryNodeLockEntry(
            request_digest=DIGEST_A,
            id="registry-node",
            version="1.2.3",
        ),
        DirectGitLockEntry(
            request_digest=DIGEST_A,
            url="https://example.test/direct.git",
            commit=COMMIT_B,
        ),
        ComfyUIRequirementsLockEntry(
            request_digest=compute_request_digest(requirements_request),
            digest=(f"sha256:{hashlib.sha256(requirements_content).hexdigest()}"),
            content=requirements_content.decode("utf-8"),
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
            resolver_descriptor_digest=DIGEST_B,
        )
        entries.insert(
            4,
            UvToolLockEntry(
                request_digest=compute_request_digest(cli_request),
                name="comfy-cli",
                extras=(),
                version="1.8.0",
            ),
        )
    if hook_digest is not None:
        entries.append(
            BuildHookLockEntry(
                relative_path="hooks/pre.py",
                digest=hook_digest,
            )
        )
    if with_uv_tool:
        entries.append(
            UvToolLockEntry(
                request_digest=DIGEST_C,
                name="ruff",
                extras=(),
                version="0.15.18",
            )
        )
    staged = {canonical_entry_key(entry): entry for entry in entries}
    graph = build_canonical_request_graph(
        config,
        domains=validate_final_config_domains(config),
        release=planning_release_inputs(canonical_wheel()),
        uv_descriptor_digest=DIGEST_B,
        comfyui_entry=staged[("comfyui",)],
        requirements_entry=staged[("comfyui", "requirements")],
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
    lock = canonical_lock_from_entries(entries)
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
        domains=validate_final_config_domains(config),
        release=planning_release_inputs(canonical_wheel()),
        uv_descriptor_digest=entries[("images", "uv")].digest,
        comfyui_entry=entries[("comfyui",)],
        requirements_entry=entries[("comfyui", "requirements")],
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
            file_downloader_explicit=(False,)
            * sum(item.type == "http" for item in config.files),
            file_download_mode_explicit=(False,)
            * sum(item.type == "http" for item in config.files),
        ),
    )
    return construct_build_plan(
        request_graph(config, resolution),
        resolution.lock,
        runtime_provenance=provenance,
        **kwargs,
    )
