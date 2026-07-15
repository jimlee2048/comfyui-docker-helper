"""Strict exact-ComfyUI requirements projection and merge contracts."""

from __future__ import annotations

import pytest

from comfyui_docker_helper.comfyui_requirements import (
    CUDA_PROTECTED_REQUIREMENTS,
    ComfyUIRequirementsError,
    merge_pytorch_requirements,
    parse_comfyui_requirements,
    parse_manager_requirements,
    protected_policy_digest,
)
from comfyui_docker_helper.config.canonical_lock import DirectPythonRequestMember


def _parse(content: bytes, *, python_version: str = "3.13.14"):
    return parse_comfyui_requirements(
        content,
        python_version=python_version,
        platform="linux/amd64",
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
        {"package": "torch", "extras": [], "selector": ""},
        {"package": "torchaudio", "extras": [], "selector": ""},
        {
            "package": "torchvision",
            "extras": ["image"],
            "selector": ">=0.27",
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
        DirectPythonRequestMember(package="torch", extras=[], selector="==2.12.1"),
        (
            DirectPythonRequestMember(package="torch", extras=["dynamo"], selector=""),
            DirectPythonRequestMember(
                package="torchvision", extras=[], selector=">=0.27"
            ),
            DirectPythonRequestMember(package="torchaudio", extras=[], selector=""),
        ),
        (
            DirectPythonRequestMember(
                package="torchvision", extras=["image"], selector="<0.28"
            ),
            DirectPythonRequestMember(
                package="torchaudio", extras=["io"], selector="==2.11.0"
            ),
        ),
    )

    assert [item.model_dump(mode="json") for item in merged] == [
        {
            "package": "torch",
            "extras": ["dynamo"],
            "selector": "==2.12.1",
        },
        {
            "package": "torchaudio",
            "extras": ["io"],
            "selector": "==2.11.0",
        },
        {
            "package": "torchvision",
            "extras": ["image"],
            "selector": "<0.28,>=0.27",
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
            DirectPythonRequestMember(package="torch", extras=[], selector="==2.12.1"),
            (
                DirectPythonRequestMember(
                    package="torchaudio", extras=[], selector=selectors[0]
                ),
            ),
            (
                DirectPythonRequestMember(
                    package="torchaudio", extras=[], selector=selectors[1]
                ),
            ),
        )


def test_policy_digest_binds_sorted_names_and_policy_version() -> None:
    assert protected_policy_digest(
        ("torchvision", "torch", "torchaudio")
    ) == protected_policy_digest(CUDA_PROTECTED_REQUIREMENTS)


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
        )


def test_manager_parser_accepts_checkout_owned_prerelease_pin() -> None:
    parsed = parse_manager_requirements(
        b"comfyui_manager==4.1b8\n",
        python_version="3.13.14",
        platform="linux/amd64",
    )

    assert parsed.manager_version == "4.1b8"
