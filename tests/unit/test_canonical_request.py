"""Grouped request-key and release-binding contracts."""

from __future__ import annotations

from comfyui_docker_helper.config.canonical_lock import (
    ComfyCliRequestIdentity,
    ComfyUIRequirementsRequestIdentity,
    DirectGitRequestIdentity,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    PyTorchRequestIdentity,
    RegistryRequestIdentity,
    RequirementsRoutingPolicy,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_request import (
    DesiredResolution,
    PlanningReleaseInputs,
    SelectorStability,
    request_keys,
    request_stability,
)

DIGEST = f"sha256:{'a' * 64}"
COMMIT = "1" * 40


def _routing_policy(*, revision: int = 1) -> RequirementsRoutingPolicy:
    return RequirementsRoutingPolicy(
        revision=revision,
        routed_names=("torch", "torchaudio", "torchvision"),
        syntax="pep508",
        markers="packaging-target-environment",
        normalization="pep503-names-pep508-extras",
        merge="specifier-intersection-extra-union",
        sources="reject-options-and-direct-urls",
    )


def test_fixed_domains_define_one_atomic_key_per_resolution() -> None:
    requests = (
        OciRequestIdentity(
            type="oci",
            role="cuda-base",
            repository="nvidia/cuda",
            tag="13.0.3-cudnn-devel-ubuntu24.04",
            platform="linux/amd64",
        ),
        ManagedPythonRequestIdentity(
            type="managed-python",
            version="3.13.14",
            implementation="cpython",
            platform="linux/amd64",
            libc="gnu",
            catalog_descriptor_digest=DIGEST,
        ),
        PyTorchRequestIdentity(
            type="pytorch-group",
            environment="application",
            group="pytorch",
            backend="cuda",
            channel="cu130",
            python_version="3.13.14",
            platform="linux/amd64",
            python_index_url="https://pypi.org/simple",
            pytorch_index_url="https://download.pytorch.org/whl/cu130",
            members=(
                DirectPythonRequestMember(
                    package="torch", extras=(), selector="==2.12.1"
                ),
                DirectPythonRequestMember(
                    package="torchvision", extras=(), selector="==0.27.1"
                ),
            ),
        ),
        DirectPythonRequestIdentity(
            type="python-group",
            environment="application",
            group="application-extra",
            python_version="3.13.14",
            platform="linux/amd64",
            index_url="https://pypi.org/simple",
            members=(
                DirectPythonRequestMember(
                    package="numpy", extras=(), selector="<3,>=2"
                ),
                DirectPythonRequestMember(
                    package="pillow", extras=(), selector="<12,>=11"
                ),
            ),
        ),
        DirectPythonRequestIdentity(
            type="python-group",
            environment="uv-tool:ruff",
            group="uv-tool",
            python_version="3.13.14",
            platform="linux/amd64",
            index_url="https://pypi.org/simple",
            members=(
                DirectPythonRequestMember(
                    package="ruff", extras=(), selector="<0.16,>=0.15"
                ),
            ),
        ),
    )

    assert tuple(request_keys(request)[0] for request in requests) == (
        ("images", "cuda"),
        ("python", "interpreter"),
        ("python", "package_groups", "pytorch"),
        ("python", "package_groups", "application_extras"),
        ("python", "uv_tools", "ruff"),
    )
    assert all(len(DesiredResolution(request).keys) == 1 for request in requests)


def test_non_python_domains_use_semantic_grouped_keys() -> None:
    cli = ComfyCliRequestIdentity(
        type="comfy-cli",
        package="comfy-cli",
        policy="highest-target-compatible-stable",
        minimum_version="1.7.0",
        environment="uv-tool:comfy-cli",
        index_url="https://pypi.org/simple",
        python_version="3.13.14",
        platform="linux/amd64",
    )
    registry = RegistryRequestIdentity(
        type="registry", id="example-node", selector="latest"
    )
    git = DirectGitRequestIdentity(
        type="git", url="https://example.test/node.git", ref="main"
    )

    assert request_keys(cli) == (("python", "uv_tools", "comfy-cli"),)
    assert request_keys(registry) == (("custom_nodes", "registry", "example-node"),)
    assert request_keys(git) == (
        ("custom_nodes", "git", "https://example.test/node.git"),
    )


def test_complete_routing_policy_is_bound_only_through_request_digest() -> None:
    values = dict(
        type="comfyui-requirements",
        repository="https://github.com/Comfy-Org/ComfyUI.git",
        commit=COMMIT,
        floor_commit=COMMIT,
        path="requirements.txt",
        python_version="3.13.14",
        platform="linux/amd64",
    )
    current = ComfyUIRequirementsRequestIdentity(
        **values, routing_policy=_routing_policy()
    )
    changed = ComfyUIRequirementsRequestIdentity(
        **values,
        routing_policy=RequirementsRoutingPolicy(
            **{
                **_routing_policy().model_dump(),
                "sources": "reject-options-and-direct-urls",
                "routed_names": ("torch", "torchvision"),
            }
        ),
    )

    assert current.protected_names == ("torch", "torchaudio", "torchvision")
    assert compute_request_digest(current) != compute_request_digest(changed)
    assert request_keys(current) == (("comfyui", "requirements"),)


def test_release_inputs_bind_wheel_without_affecting_resolution_keys() -> None:
    release = PlanningReleaseInputs(
        pip_version="26.1.2",
        cdh_version="0.5.0",
        cdh_wheel_digest=DIGEST,
    )

    assert release == PlanningReleaseInputs("26.1.2", "0.5.0", DIGEST)


def test_moving_and_exact_stability_remain_group_scoped() -> None:
    exact = DirectGitRequestIdentity(
        type="git", url="https://example.test/node.git", ref=COMMIT
    )
    moving = DirectGitRequestIdentity(
        type="git", url="https://example.test/node.git", ref="main"
    )

    assert request_stability(exact) is SelectorStability.EXACT
    assert request_stability(moving) is SelectorStability.MOVING
