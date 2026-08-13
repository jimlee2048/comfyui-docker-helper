"""Strict exact-ComfyUI requirements projection and merge contracts."""

from __future__ import annotations

import pytest

from comfyui_docker_helper.comfyui_requirements import (
    CUDA_PROTECTED_REQUIREMENTS,
    ComfyUIRequirementsError,
    merge_pytorch_requirements,
    parse_comfyui_requirements,
    parse_manager_requirements,
    target_marker_environment,
)
from comfyui_docker_helper.config.canonical_lock import DirectPythonRequestMember


def _parse(content: bytes, *, python_version: str = "3.13.14"):
    return parse_comfyui_requirements(
        content,
        python_version=python_version,
        platform="linux/amd64",
        machine="x86_64",
        protected_names=CUDA_PROTECTED_REQUIREMENTS,
    )


# Requirements projection preserves target markers, protected ownership, and
# source safety.
def test_projection_evaluates_target_markers_and_filters_every_protected_row() -> None:
    parsed = _parse(
        b"""
# root requirements
torch
TorchVision[image]>=0.27; python_version >= "3.13"
torchaudio; python_version < "3.14"
torchaudio[io]; python_version >= "3.14"
numpy>=1.25
"""
    )

    assert [item.model_dump(mode="json") for item in parsed.protected] == [
        {
            "package": "torch",
            "extras": [],
            "specifier": "",
            "direct_reference": None,
        },
        {
            "package": "torchaudio",
            "extras": [],
            "specifier": "",
            "direct_reference": None,
        },
        {
            "package": "torchvision",
            "extras": ["image"],
            "specifier": ">=0.27",
            "direct_reference": None,
        },
    ]
    assert parsed.ordinary == ("numpy>=1.25",)
    assert parsed.digest.startswith("sha256:")


@pytest.mark.parametrize(
    "row",
    [
        "--index-url https://poison.test/simple",
        "--extra-index-url=https://poison.test/simple",
        "-e git+https://poison.test/repo.git#egg=torch",
        "torch @ https://poison.test/torch.whl",
        "git+https://poison.test/repo.git#egg=torch",
        "not a requirement ???",
    ],
)
def test_parser_rejects_source_changing_direct_and_non_pep508_rows(row: str) -> None:
    with pytest.raises(ComfyUIRequirementsError, match="line 1"):
        _parse(f"{row}\n".encode())


def test_merge_unions_extras_conjoins_selectors_and_treats_bare_as_neutral() -> None:
    merged = merge_pytorch_requirements(
        DirectPythonRequestMember(package="torch", extras=[], specifier="==2.12.1"),
        (
            DirectPythonRequestMember(package="torch", extras=["dynamo"], specifier=""),
            DirectPythonRequestMember(
                package="torchvision", extras=[], specifier=">=0.27"
            ),
            DirectPythonRequestMember(package="torchaudio", extras=[], specifier=""),
        ),
        (
            DirectPythonRequestMember(
                package="torchvision", extras=["image"], specifier="<0.28"
            ),
            DirectPythonRequestMember(
                package="torchaudio", extras=["io"], specifier="==2.11.0"
            ),
        ),
    )

    assert [item.model_dump(mode="json") for item in merged] == [
        {
            "package": "torch",
            "extras": ["dynamo"],
            "specifier": "==2.12.1",
            "direct_reference": None,
        },
        {
            "package": "torchaudio",
            "extras": ["io"],
            "specifier": "==2.11.0",
            "direct_reference": None,
        },
        {
            "package": "torchvision",
            "extras": ["image"],
            "specifier": "<0.28,>=0.27",
            "direct_reference": None,
        },
    ]


@pytest.mark.parametrize(
    "selectors",
    [
        ("==2.11.0", "==2.12.0"),
        ("==2.11.0", ">=2.12"),
        ("==2.11.0", "!=2.11.0"),
    ],
)
def test_merge_rejects_exact_conflicts(selectors: tuple[str, str]) -> None:
    with pytest.raises(ComfyUIRequirementsError, match=r"conflict|incompatible"):
        merge_pytorch_requirements(
            DirectPythonRequestMember(package="torch", extras=[], specifier="==2.12.1"),
            (
                DirectPythonRequestMember(
                    package="torchaudio", extras=[], specifier=selectors[0]
                ),
            ),
            (
                DirectPythonRequestMember(
                    package="torchaudio", extras=[], specifier=selectors[1]
                ),
            ),
        )


def test_merge_preserves_one_nonprotected_direct_source_exactly() -> None:
    direct = DirectPythonRequestMember(
        package="sageattention",
        extras=["torch"],
        specifier="",
        direct_reference="https://example.test/sageattention.whl#sha256=abc",
    )

    merged = merge_pytorch_requirements(
        DirectPythonRequestMember(package="torch", extras=[], specifier="==2.12.1"),
        (),
        (direct,),
    )

    assert merged == (
        direct,
        DirectPythonRequestMember(package="torch", extras=[], specifier="==2.12.1"),
    )


def test_merge_rejects_protected_direct_source() -> None:
    direct = DirectPythonRequestMember(
        package="torch",
        extras=[],
        specifier="",
        direct_reference="https://example.test/torch.whl",
    )

    with pytest.raises(
        ComfyUIRequirementsError,
        match="protected PyTorch requirements must use the managed index source",
    ):
        merge_pytorch_requirements(
            DirectPythonRequestMember(package="torch", extras=[], specifier="==2.12.1"),
            (),
            (direct,),
        )


def test_target_marker_environment_is_complete_and_host_independent() -> None:
    assert target_marker_environment("3.13.14", "linux/amd64", "x86_64") == {
        "implementation_name": "cpython",
        "implementation_version": "3.13.14",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": "Linux",
        "platform_version": "",
        "python_full_version": "3.13.14",
        "python_version": "3.13",
        "sys_platform": "linux",
        "extra": "",
    }


def test_manager_parser_projects_exact_checkout_owned_distribution() -> None:
    parsed = parse_manager_requirements(
        b"""
# exact checkout declaration
comfyui_manager==4.0.5
packaging>=24; python_version >= "3.13"
ignored==1; python_version < "3.13"
""",
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
    )

    assert parsed.rows == (
        "comfyui_manager==4.0.5",
        'packaging>=24; python_version >= "3.13"',
        'ignored==1; python_version < "3.13"',
    )
    assert parsed.digest.startswith("sha256:")
    assert [(item.package, item.specifier) for item in parsed.active] == [
        ("comfyui-manager", "==4.0.5"),
        ("packaging", ">=24"),
    ]
    assert parsed.manager_version == "4.0.5"


@pytest.mark.parametrize(
    "content, message",
    [
        (b"--index-url https://poison.test/simple\n", "changes package sources"),
        (b"comfyui_manager @ https://poison.test/manager.whl\n", "direct source"),
        (b"comfyui_manager>=4\n", "exact checkout-owned version"),
        (b"comfyui_manager==v4.0.5\n", "canonical exact"),
        (b"requests==2\n", "exactly one active comfyui-manager"),
        (
            b"comfyui_manager==4.0.5\nComfyUI-Manager==4.0.5\n",
            "duplicate target package",
        ),
    ],
)
def test_manager_parser_rejects_unowned_or_ambiguous_requirements(
    content: bytes, message: str
) -> None:
    with pytest.raises(ComfyUIRequirementsError, match=message):
        parse_manager_requirements(
            content,
            python_version="3.13.14",
            platform="linux/amd64",
            machine="x86_64",
        )


def test_manager_parser_accepts_checkout_owned_prerelease_pin() -> None:
    parsed = parse_manager_requirements(
        b"comfyui_manager==4.1b8\n",
        python_version="3.13.14",
        platform="linux/amd64",
        machine="x86_64",
    )

    assert parsed.manager_version == "4.1b8"
