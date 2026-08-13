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
    CanonicalRequestError,
    DesiredResolution,
    PlanningReleaseInputs,
    SelectorStability,
    request_keys,
    request_stability,
    uv_provider_tag,
)
from tests.build_plan_support import accepted_resolution, final_config, request_graph

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
                    package="torch", extras=(), specifier="==2.12.1"
                ),
                DirectPythonRequestMember(
                    package="torchvision", extras=(), specifier="==0.27.1"
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
                    package="numpy", extras=(), specifier="<3,>=2"
                ),
                DirectPythonRequestMember(
                    package="pillow", extras=(), specifier="<12,>=11"
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
                    package="ruff", extras=(), specifier="<0.16,>=0.15"
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
            package="packaging", extras=(), specifier="==26.2"
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
                        package="torch", extras=(), specifier="==2.12.1"
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
                specifier=selector,
            ),
        ),
    )

    assert request_stability(request) is expected


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("==1,==2", SelectorStability.MOVING),
        ("!=1,==1", SelectorStability.EXACT),
        ("<1,==1", SelectorStability.EXACT),
    ],
)
def test_direct_python_request_does_not_pre_solve_standard_selectors(
    selector: str,
    expected: SelectorStability,
) -> None:
    member = DirectPythonRequestMember(package="demo", extras=(), specifier=selector)
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST,
        members=(member,),
    )

    assert request_stability(request) is expected


def test_direct_source_is_marker_free_moving_request_identity() -> None:
    first_source = "https://example.test/demo.whl#sha256=abc"
    member = DirectPythonRequestMember(
        package="demo",
        extras=("cli",),
        specifier="",
        direct_reference=first_source,
    )
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        resolver_descriptor_digest=DIGEST,
        members=(member,),
    )
    changed = request.model_copy(
        update={
            "members": (
                member.model_copy(
                    update={"direct_reference": "https://example.test/demo-v2.whl"}
                ),
            )
        }
    )

    assert member.resolver_requirement == f"demo[cli] @ {first_source}"
    assert request_stability(request) is SelectorStability.MOVING
    assert compute_request_digest(request) != compute_request_digest(changed)


def test_direct_member_rejects_a_combined_specifier_and_source() -> None:
    with pytest.raises(ValidationError):
        DirectPythonRequestMember(
            package="demo",
            extras=(),
            specifier=">=1",
            direct_reference="https://example.test/demo.whl",
        )


@pytest.mark.parametrize(
    "direct_reference",
    [
        "file:///tmp/demo.whl",
        "ssh://example.test/demo.git",
        "https://user@example.test/demo.whl",
        "https://example.test/demo wheel.whl",
    ],
)
def test_direct_member_reuses_public_source_admission(
    direct_reference: str,
) -> None:
    with pytest.raises(ValidationError):
        DirectPythonRequestMember(
            package="demo",
            extras=(),
            specifier="",
            direct_reference=direct_reference,
        )


@pytest.mark.parametrize(
    ("group", "field", "request_group"),
    [
        ("python", "extra_packages", "application-extra"),
        ("python", "uv_tools", "uv-tool"),
        ("pytorch", "extra_packages", "pytorch"),
    ],
)
def test_active_direct_source_enters_each_user_request_field(
    group: str,
    field: str,
    request_group: str,
) -> None:
    config = final_config().model_copy(deep=True)
    source = "https://example.test/source-demo.whl#sha256=abc"
    setattr(getattr(config, group), field, [f"SourceDemo[CLI] @ {source}"])

    graph = request_graph(config, accepted_resolution())
    request = next(
        item.request
        for item in graph.desired
        if isinstance(
            item.request, (DirectPythonRequestIdentity, PyTorchRequestIdentity)
        )
        and item.request.group == request_group
    )
    member = next(item for item in request.members if item.package == "sourcedemo")

    assert member.specifier == ""
    assert member.direct_reference == source
    assert member.resolver_requirement == f"sourcedemo[cli] @ {source}"


def test_protected_pytorch_direct_source_fails_before_resolution() -> None:
    config = final_config().model_copy(deep=True)
    config.pytorch.extra_packages = [
        "torch @ https://example.test/torch.whl",
    ]

    with pytest.raises(CanonicalRequestError) as raised:
        request_graph(config, accepted_resolution())

    assert [item.code for item in raised.value.diagnostics] == [
        "pytorch.protected_requirement_conflict"
    ]


@pytest.mark.parametrize(
    ("group", "field"),
    [
        ("python", "extra_packages"),
        ("python", "uv_tools"),
        ("pytorch", "extra_packages"),
    ],
)
def test_inactive_requirement_changes_image_identity_without_entering_requests(
    group: str,
    field: str,
) -> None:
    baseline = final_config()
    configured = baseline.model_copy(deep=True)
    values = getattr(getattr(configured, group), field)
    values.append('inactive-demo; python_version < "3.13"')
    resolution = accepted_resolution()

    baseline_graph = request_graph(baseline, resolution)
    configured_graph = request_graph(configured, resolution)

    assert baseline_graph.image_config_digest != configured_graph.image_config_digest
    assert all(
        not isinstance(item.request, DirectPythonRequestIdentity)
        or all(member.package != "inactive-demo" for member in item.request.members)
        for item in configured_graph.desired
    )


def test_distinct_true_markers_share_one_active_request_identity() -> None:
    first = final_config().model_copy(deep=True)
    second = final_config().model_copy(deep=True)
    first.python.extra_packages = ['NumPy>=2,<3; python_version >= "3.12"']
    second.python.extra_packages = ['NumPy>=2,<3; platform_system == "Linux"']
    resolution = accepted_resolution()

    first_graph = request_graph(first, resolution)
    second_graph = request_graph(second, resolution)
    first_request = next(
        item.request
        for item in first_graph.desired
        if isinstance(item.request, DirectPythonRequestIdentity)
        and item.request.group == "application-extra"
    )
    second_request = next(
        item.request
        for item in second_graph.desired
        if isinstance(item.request, DirectPythonRequestIdentity)
        and item.request.group == "application-extra"
    )

    assert first_request == second_request
    assert first_graph.image_config_digest != second_graph.image_config_digest
