"""Focused platform/backend planning-domain contracts."""

from typing import Any

import pytest

from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_planning import (
    CudaBackendAdapter,
    CudaVersion,
    FinalPlanningError,
    TargetPlatform,
    build_final_planning_domain,
)
from comfyui_docker_helper.config.final_validation import (
    FinalConfigError,
    validate_final_config_structure,
)
from comfyui_docker_helper.exact_ledger import (
    BASE_IMAGE_DISTRO,
    CUDA_IMAGE_FLAVOR,
    CUDA_IMAGE_REPOSITORY,
    CUDA_VERSION,
    DEFAULT_MANAGED_PYTHON_VERSION,
    FALLBACK_MANAGED_PYTHON_VERSION,
    PYTORCH_CHANNEL,
    PYTORCH_VERSION,
    TARGET_PLATFORM,
    TORCHVISION_VERSION,
)


def _document() -> dict[str, Any]:
    return {
        "compute_platform": {"type": "cuda", "cuda": {"version": CUDA_VERSION}},
        "python": {"version": DEFAULT_MANAGED_PYTHON_VERSION},
        "pytorch": {
            "version": PYTORCH_VERSION,
            "extra_packages": [f"torchvision=={TORCHVISION_VERSION}"],
        },
        "build": {"platforms": [TARGET_PLATFORM]},
        "comfyui": {"version": "0.11.0", "install_manager": False},
    }


def _config(document: dict[str, Any] | None = None) -> FinalConfig:
    return validate_final_config_structure(document or _document())


def test_exact_ledger_baseline_derives_one_complete_group() -> None:
    planning = build_final_planning_domain(_config())

    assert planning.target_platforms == (TargetPlatform.LINUX_AMD64,)
    assert planning.backend.version.value == CUDA_VERSION
    assert planning.backend.version.patch == "3"
    assert planning.backend.target_platform is TargetPlatform.LINUX_AMD64
    assert planning.backend.package_channel == PYTORCH_CHANNEL
    assert planning.backend.base_image == (
        f"{CUDA_IMAGE_REPOSITORY}:{CUDA_VERSION}-{CUDA_IMAGE_FLAVOR}-"
        f"{BASE_IMAGE_DISTRO}"
    )
    assert planning.pytorch_group.python_version == DEFAULT_MANAGED_PYTHON_VERSION
    assert planning.pytorch_group.target_platform is TargetPlatform.LINUX_AMD64
    assert planning.pytorch_group.package_channel == PYTORCH_CHANNEL
    assert planning.pytorch_group.index_url == (
        f"https://download.pytorch.org/whl/{PYTORCH_CHANNEL}"
    )
    assert [request.name for request in planning.pytorch_group.requirements] == [
        "torch",
        "torchvision",
    ]
    assert [request.selector for request in planning.pytorch_group.requirements] == [
        f"=={PYTORCH_VERSION}",
        f"=={TORCHVISION_VERSION}",
    ]


@pytest.mark.parametrize(
    "python_version",
    [DEFAULT_MANAGED_PYTHON_VERSION, FALLBACK_MANAGED_PYTHON_VERSION],
)
def test_exact_python_profiles_propagate_same_complete_cu130_group(
    python_version: str,
) -> None:
    document = _document()
    document["python"]["version"] = python_version
    document["pytorch"]["extra_packages"].append("xFormers>=0.0.30,<0.1")

    group = build_final_planning_domain(_config(document)).pytorch_group

    assert group.python_version == python_version
    assert group.target_platform is TargetPlatform.LINUX_AMD64
    assert group.package_channel == PYTORCH_CHANNEL
    assert [request.name for request in group.requirements] == [
        "torch",
        "torchvision",
        "xformers",
    ]
    assert group.requirements[0].selector == f"=={PYTORCH_VERSION}"
    assert group.requirements[1].selector == f"=={TORCHVISION_VERSION}"
    assert group.requirements[2].selector == "<0.1,>=0.0.30"


def test_custom_cuda_version_is_structurally_planned_without_support_status() -> None:
    document = _document()
    document["compute_platform"]["cuda"]["version"] = "12.9.2"
    document["python"]["version"] = FALLBACK_MANAGED_PYTHON_VERSION
    document["pytorch"]["index_base_url"] = "https://mirror.example.test/wheels/"

    planning = build_final_planning_domain(_config(document))

    assert planning.pytorch_group.python_version == FALLBACK_MANAGED_PYTHON_VERSION
    assert planning.backend.package_channel == "cu129"
    assert planning.backend.base_image == (
        f"{CUDA_IMAGE_REPOSITORY}:12.9.2-{CUDA_IMAGE_FLAVOR}-{BASE_IMAGE_DISTRO}"
    )
    assert planning.pytorch_group.package_channel == "cu129"
    assert planning.pytorch_group.index_url == (
        "https://mirror.example.test/wheels/cu129"
    )
    assert not hasattr(planning, "support_status")
    assert not hasattr(planning.backend, "support_status")


def test_complete_group_preserves_declared_order_and_normalization() -> None:
    document = _document()
    document["pytorch"]["extra_packages"] = [
        "TorchVision[Video]>=0.27,<0.28",
        "xFormers",
    ]

    group = build_final_planning_domain(_config(document)).pytorch_group

    assert [request.name for request in group.requirements] == [
        "torch",
        "torchvision",
        "xformers",
    ]
    assert group.requirements[1].extras == ("video",)
    assert group.requirements[1].selector == "<0.28,>=0.27"
    assert group.requirements[2].selector == ""


def test_group_extra_aliases_are_pep685_deduplicated_and_deterministic() -> None:
    document = _document()
    document["pytorch"]["extra_packages"] = [
        "TorchVision[z_extra,Foo_Bar,foo-bar,FOO.BAR]>=0.27,<0.28",
        "xFormers",
    ]
    config = _config(document)

    first = build_final_planning_domain(config).pytorch_group
    second = build_final_planning_domain(config).pytorch_group

    assert first == second
    assert first.requirements[1].extras == ("foo-bar", "z-extra")
    assert [request.name for request in first.requirements] == [
        "torch",
        "torchvision",
        "xformers",
    ]


def test_custom_index_is_normalized_once_with_derived_channel() -> None:
    document = _document()
    document["pytorch"]["index_base_url"] = "https://example.test/pytorch/"

    group = build_final_planning_domain(_config(document)).pytorch_group

    assert group.index_url == f"https://example.test/pytorch/{PYTORCH_CHANNEL}"


@pytest.mark.parametrize("version", ["13", "13.x", "13.0.3-rc1", " 13.0.3"])
def test_invalid_cuda_stops_before_backend_derivation(version: str) -> None:
    document = _document()
    document["compute_platform"]["cuda"]["version"] = version

    with pytest.raises(FinalPlanningError) as raised:
        build_final_planning_domain(_config(document))

    assert [item.code for item in raised.value.diagnostics] == [
        "compute_platform.invalid_cuda_version"
    ]


def test_only_linux_amd64_is_structurally_accepted() -> None:
    document = _document()
    document["build"]["platforms"] = ["linux/arm64"]

    with pytest.raises(FinalConfigError) as raised:
        _config(document)

    assert raised.value.diagnostics[0].path == ("build", "platforms", 0)
    assert raised.value.diagnostics[0].code == "schema.literal_error"


def test_only_cuda_backend_is_structurally_accepted() -> None:
    document = _document()
    document["compute_platform"]["type"] = "rocm"

    with pytest.raises(FinalConfigError) as raised:
        _config(document)

    assert raised.value.diagnostics[0].path == ("compute_platform", "type")
    assert raised.value.diagnostics[0].code == "schema.literal_error"


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("compute_platform.cuda", "image_flavor"),
        ("compute_platform.cuda", "image_distro"),
        ("pytorch", "wheel_cuda_version"),
    ],
)
def test_no_parallel_backend_or_image_knobs_are_accepted(
    section: str,
    field: str,
) -> None:
    document = _document()
    target = document
    for part in section.split("."):
        target = target[part]
    target[field] = "parallel-authority"

    with pytest.raises(FinalConfigError) as raised:
        _config(document)

    assert raised.value.diagnostics[0].code == "schema.extra_forbidden"


def test_duplicate_platform_and_package_diagnostics_are_deterministic() -> None:
    document = _document()
    document["build"]["platforms"] = [TARGET_PLATFORM, TARGET_PLATFORM]
    document["pytorch"]["extra_packages"] = [
        f"torchvision=={TORCHVISION_VERSION}",
        f"TorchVision=={TORCHVISION_VERSION}",
        f"torch=={PYTORCH_VERSION}",
    ]

    with pytest.raises(FinalPlanningError) as raised:
        build_final_planning_domain(_config(document))

    assert [(item.path, item.code) for item in raised.value.diagnostics] == [
        (("build", "platforms", 1), "build.duplicate_platform"),
        (("pytorch", "extra_packages", 1), "python.duplicate_package_owner"),
        (("pytorch", "extra_packages", 2), "python.duplicate_package_owner"),
    ]


def test_backend_adapter_is_pure_and_propagates_exact_target() -> None:
    adapter = CudaBackendAdapter()

    version = CudaVersion.from_validated(CUDA_VERSION)
    first = adapter.derive(version, TargetPlatform.LINUX_AMD64)
    second = adapter.derive(version, TargetPlatform.LINUX_AMD64)

    assert first == second
    assert first.target_platform is TargetPlatform.LINUX_AMD64
