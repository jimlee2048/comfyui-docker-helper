"""Canonical request graph derivation and immutability contracts."""

from dataclasses import FrozenInstanceError

import pytest
from tests.unit.test_build_plan import accepted_resolution, final_config, request_graph

from comfyui_docker_helper.comfyui_requirements import ComfyUIRequirementsError
from comfyui_docker_helper.config.canonical_lock import (
    OciRequestIdentity,
    PyTorchRequestIdentity,
)
from comfyui_docker_helper.config.canonical_request import CanonicalRequestError
from comfyui_docker_helper.config.final_models import (
    CudaImageDistro,
    CudaImageFlavor,
    FinalConfig,
)


# One immutable request graph owns normalized acquisition intent and diagnostics.
def test_graph_owns_backend_and_complete_pytorch_request_once() -> None:
    graph = request_graph(final_config(), accepted_resolution())
    cuda = graph.request(("oci", "cuda-base"))
    pytorch = graph.request(("pytorch-compatibility", "application"))

    assert isinstance(cuda, OciRequestIdentity)
    assert cuda.tag == "13.0.3-cudnn-devel-ubuntu24.04"
    assert graph.backend.package_channel == "cu130"
    assert isinstance(pytorch, PyTorchRequestIdentity)
    assert pytorch.pytorch_index_url == "https://download.pytorch.org/whl/cu130"
    assert tuple(member.package for member in pytorch.members) == (
        "torch",
        "torchaudio",
        "torchvision",
    )
    assert pytorch.members[2].extras == ("image",)


@pytest.mark.parametrize("python_version", ["3.12.13", "3.13.14", "3.14.6"])
def test_graph_propagates_each_supported_python_target(python_version: str) -> None:
    config = final_config(python_version=python_version)
    graph = request_graph(
        config,
        accepted_resolution(python_version=python_version),
    )
    pytorch = graph.request(("pytorch-compatibility", "application"))

    assert isinstance(pytorch, PyTorchRequestIdentity)
    assert pytorch.python_version == python_version
    assert (
        graph.request(("managed-python", "cpython", "linux/amd64")).version
        == python_version
    )


# Every supported CUDA image selector pair projects into one exact typed OCI
# request, while the package channel remains a function of CUDA version alone.
@pytest.mark.parametrize("image_distro", ["ubuntu22.04", "ubuntu24.04"])
@pytest.mark.parametrize(
    "image_flavor",
    ["base", "runtime", "devel", "cudnn-runtime", "cudnn-devel"],
)
def test_graph_projects_each_cuda_image_selector_pair(
    image_flavor: CudaImageFlavor,
    image_distro: CudaImageDistro,
) -> None:
    document = final_config().model_dump(mode="python")
    document["compute_platform"]["cuda"]["image_flavor"] = image_flavor
    document["compute_platform"]["cuda"]["image_distro"] = image_distro
    graph = request_graph(
        FinalConfig.model_validate(document),
        accepted_resolution(),
    )
    cuda = graph.request(("oci", "cuda-base"))
    pytorch = graph.request(("pytorch-compatibility", "application"))
    expected_tag = f"13.0.3-{image_flavor}-{image_distro}"

    assert cuda == OciRequestIdentity(
        type="oci",
        role="cuda-base",
        repository="nvidia/cuda",
        tag=expected_tag,
        platform="linux/amd64",
    )
    assert graph.backend.base_image == f"nvidia/cuda:{expected_tag}"
    assert graph.backend.package_channel == "cu130"
    assert isinstance(pytorch, PyTorchRequestIdentity)
    assert pytorch.channel == "cu130"
    assert pytorch.pytorch_index_url == "https://download.pytorch.org/whl/cu130"


def test_graph_derives_nondefault_cuda_tag_channel_and_index_once() -> None:
    document = final_config().model_dump(mode="python")
    document["compute_platform"]["cuda"] = {
        "version": "12.9.2",
        "image_flavor": "runtime",
        "image_distro": "ubuntu22.04",
    }
    document["pytorch"]["index_base_url"] = "https://mirror.example.test/pytorch/"
    config = FinalConfig.model_validate(document)
    graph = request_graph(config, accepted_resolution())
    cuda = graph.request(("oci", "cuda-base"))
    pytorch = graph.request(("pytorch-compatibility", "application"))

    assert isinstance(cuda, OciRequestIdentity)
    assert cuda.tag == "12.9.2-runtime-ubuntu22.04"
    assert graph.backend.package_channel == "cu129"
    assert isinstance(pytorch, PyTorchRequestIdentity)
    assert pytorch.channel == "cu129"
    assert pytorch.pytorch_index_url == "https://mirror.example.test/pytorch/cu129"


def test_graph_and_derived_sequences_are_immutable() -> None:
    graph = request_graph(final_config(), accepted_resolution())
    pytorch = graph.request(("pytorch-compatibility", "application"))

    assert isinstance(graph.desired, tuple)
    assert isinstance(pytorch, PyTorchRequestIdentity)
    assert isinstance(pytorch.members, tuple)
    with pytest.raises(FrozenInstanceError):
        graph.config_digest = "sha256:forged"


def test_protected_requirement_conflict_has_stable_canonical_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_merge(*_args, **_kwargs):
        raise ComfyUIRequirementsError("incompatible selectors")

    monkeypatch.setattr(
        "comfyui_docker_helper.config.canonical_request.merge_pytorch_requirements",
        reject_merge,
    )

    with pytest.raises(CanonicalRequestError) as raised:
        request_graph(final_config(), accepted_resolution())

    assert len(raised.value.diagnostics) == 1
    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.path == ("pytorch", "extra_packages")
    assert diagnostic.code == "pytorch.protected_requirement_conflict"
    assert diagnostic.message == "protected PyTorch requirements conflict"
    assert isinstance(raised.value.__cause__, ComfyUIRequirementsError)
