"""Grouped request-key and release-binding contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_request import (
    DesiredResolution,
    PlanningReleaseInputs,
    SelectorStability,
    request_keys,
    request_stability,
    uv_provider_tag,
)

DIGEST = f"sha256:{'a' * 64}"
COMMIT = "1" * 40


# Every request domain maps to its semantic atomic reconciliation key.
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
            resolver_descriptor_digest=DIGEST,
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
            resolver_descriptor_digest=DIGEST,
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
            resolver_descriptor_digest=DIGEST,
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
        resolver_descriptor_digest=DIGEST,
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


# Exact source identity changes only with an upstream source coordinate.
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "https://github.com/Comfy-Org/ComfyUI-fork.git"),
        ("commit", "2" * 40),
        ("floor_commit", "3" * 40),
    ],
)
def test_requirements_source_coordinates_bind_request_digest(
    field: str,
    value: str,
) -> None:
    values = dict(
        type="comfyui-requirements",
        repository="https://github.com/Comfy-Org/ComfyUI.git",
        commit=COMMIT,
        floor_commit=COMMIT,
        path="requirements.txt",
    )
    current = ComfyUIRequirementsRequestIdentity(**values)
    changed = ComfyUIRequirementsRequestIdentity(**{**values, field: value})

    assert compute_request_digest(current) != compute_request_digest(changed)
    assert request_keys(current) == (("comfyui", "requirements"),)
    assert request_stability(current) is SelectorStability.EXACT


def test_requirements_source_path_is_the_literal_root_requirements_file() -> None:
    with pytest.raises(ValidationError):
        ComfyUIRequirementsRequestIdentity(
            type="comfyui-requirements",
            repository="https://github.com/Comfy-Org/ComfyUI.git",
            commit=COMMIT,
            floor_commit=COMMIT,
            path="nested/requirements.txt",
        )


def test_release_inputs_bind_wheel_without_affecting_resolution_keys() -> None:
    release = PlanningReleaseInputs(
        pip_version="26.1.2",
        cdh_version="0.5.0",
        cdh_wheel_digest=DIGEST,
    )

    assert release == PlanningReleaseInputs("26.1.2", "0.5.0", DIGEST)


@pytest.mark.parametrize(
    ("selector", "tag"),
    [("0.11.28", "0.11.28-debian-slim"), ("latest", "debian-slim")],
)
def test_uv_release_selector_maps_to_owned_provider_family(
    selector: str, tag: str
) -> None:
    assert uv_provider_tag(selector) == tag


# Every uv-produced result changes identity when its executable descriptor changes.
def test_uv_descriptor_digest_invalidates_every_uv_backed_request_domain() -> None:
    def requests(digest: str):
        direct_member = DirectPythonRequestMember(
            package="packaging", extras=(), selector="==26.2"
        )
        return (
            ManagedPythonRequestIdentity(
                type="managed-python",
                version="3.13.14",
                implementation="cpython",
                platform="linux/amd64",
                libc="gnu",
                catalog_descriptor_digest=digest,
            ),
            DirectPythonRequestIdentity(
                type="python-group",
                environment="application",
                group="application-extra",
                python_version="3.13.14",
                platform="linux/amd64",
                index_url="https://pypi.org/simple",
                resolver_descriptor_digest=digest,
                members=(direct_member,),
            ),
            DirectPythonRequestIdentity(
                type="python-group",
                environment="uv-tool:packaging",
                group="uv-tool",
                python_version="3.13.14",
                platform="linux/amd64",
                index_url="https://pypi.org/simple",
                resolver_descriptor_digest=digest,
                members=(direct_member,),
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
                resolver_descriptor_digest=digest,
                members=(
                    DirectPythonRequestMember(
                        package="torch", extras=(), selector="==2.12.1"
                    ),
                ),
            ),
            ComfyCliRequestIdentity(
                type="comfy-cli",
                package="comfy-cli",
                policy="highest-target-compatible-stable",
                minimum_version="1.7.0",
                environment="uv-tool:comfy-cli",
                index_url="https://pypi.org/simple",
                python_version="3.13.14",
                platform="linux/amd64",
                resolver_descriptor_digest=digest,
            ),
        )

    current = requests(DIGEST)
    changed = requests(f"sha256:{'b' * 64}")

    assert all(
        compute_request_digest(before) != compute_request_digest(after)
        for before, after in zip(current, changed, strict=True)
    )


def test_moving_and_exact_stability_remain_group_scoped() -> None:
    exact = DirectGitRequestIdentity(
        type="git", url="https://example.test/node.git", ref=COMMIT
    )
    moving = DirectGitRequestIdentity(
        type="git", url="https://example.test/node.git", ref="main"
    )

    assert request_stability(exact) is SelectorStability.EXACT
    assert request_stability(moving) is SelectorStability.MOVING


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("==1", SelectorStability.EXACT),
        ("==1,>=1", SelectorStability.EXACT),
        ("", SelectorStability.MOVING),
        (">=1", SelectorStability.MOVING),
        ("!=1", SelectorStability.MOVING),
        ("~=1.2", SelectorStability.MOVING),
    ],
)
def test_direct_python_stability_uses_admitted_exact_selector_semantics(
    selector: str,
    expected: SelectorStability,
) -> None:
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST,
        members=(
            DirectPythonRequestMember(
                package="demo",
                extras=(),
                selector=selector,
            ),
        ),
    )

    assert request_stability(request) is expected


@pytest.mark.parametrize("selector", ["==1,==2", "!=1,==1", "==1,<1"])
def test_direct_python_request_rejects_conflicting_exact_selectors(
    selector: str,
) -> None:
    with pytest.raises(ValidationError):
        DirectPythonRequestMember(package="demo", extras=(), selector=selector)
