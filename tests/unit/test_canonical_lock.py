"""Strict domain-grouped canonical lock schema contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config.canonical_lock import (
    INVALID_CANONICAL_LOCK_MESSAGE,
    ApplicationExtrasLockEntry,
    BuildHookLockEntry,
    CanonicalLockError,
    ComfyUIRequirementsLockEntry,
    CudaImageLockEntry,
    DirectGitLockEntry,
    ManagedPythonLockEntry,
    OfficialComfyUILockEntry,
    PyTorchLockEntry,
    RegistryNodeLockEntry,
    ResolvedPythonPackage,
    RoutedPyTorchRequirement,
    RuntimeHookLockEntry,
    UvImageLockEntry,
    UvToolLockEntry,
    canonical_entry_key,
    canonical_lock_from_entries,
    dump_canonical_lock_toml,
    parse_canonical_lock_toml,
)

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
DIGEST_C = f"sha256:{'c' * 64}"
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40


def _entries():
    return [
        CudaImageLockEntry(
            request_digest=DIGEST_A,
            repository="nvidia/cuda",
            tag="13.0.3-cudnn-devel-ubuntu24.04",
            digest=DIGEST_B,
            kind="index",
            platform="linux/amd64",
        ),
        UvImageLockEntry(
            request_digest=DIGEST_B,
            repository="ghcr.io/astral-sh/uv",
            tag="0.11.28",
            digest=DIGEST_C,
            kind="index",
            platform="linux/amd64",
            observed_version="0.11.28",
        ),
        ManagedPythonLockEntry(
            request_digest=DIGEST_C,
            version="3.13.14",
            platform="linux/amd64",
            libc="gnu",
            catalog_digest=DIGEST_C,
            artifact_key="cpython-3.13.14-linux-x86_64-gnu",
            artifact_url="https://example.test/python.tar.zst",
        ),
        OfficialComfyUILockEntry(
            request_digest=DIGEST_A,
            repository="https://github.com/Comfy-Org/ComfyUI.git",
            commit=COMMIT_A,
            formal_release="0.11.0",
        ),
        ComfyUIRequirementsLockEntry(
            request_digest=DIGEST_B,
            digest=DIGEST_C,
            pytorch=(
                RoutedPyTorchRequirement(name="torchaudio", extras=(), specifier=""),
            ),
        ),
        PyTorchLockEntry(
            request_digest=DIGEST_C,
            setuptools_specifier="<82",
            packages=(
                ResolvedPythonPackage(name="torch", extras=(), version="2.12.1+cu130"),
                ResolvedPythonPackage(
                    name="torchvision", extras=("image",), version="0.27.1+cu130"
                ),
            ),
        ),
        ApplicationExtrasLockEntry(
            request_digest=DIGEST_A,
            packages=(ResolvedPythonPackage(name="numpy", extras=(), version="2.4.1"),),
        ),
        UvToolLockEntry(
            request_digest=DIGEST_B,
            name="comfy-cli",
            extras=(),
            version="1.8.0",
        ),
        RegistryNodeLockEntry(
            request_digest=DIGEST_C, id="example-node", version="1.2.3"
        ),
        DirectGitLockEntry(
            request_digest=DIGEST_A,
            url="https://example.test/node.git",
            commit=COMMIT_B,
        ),
        BuildHookLockEntry(relative_path="common/setup.sh", digest=DIGEST_B),
        RuntimeHookLockEntry(relative_path="pre-start.d/service.sh", digest=DIGEST_C),
    ]


def _lock():
    return canonical_lock_from_entries(_entries())


# Canonical lock bytes and reconciliation keys remain deterministic across
# complete grouped identities.
def test_complete_grouped_lock_round_trips_with_deterministic_bytes() -> None:
    first = dump_canonical_lock_toml(_lock())
    second = dump_canonical_lock_toml(
        canonical_lock_from_entries(list(reversed(_entries())))
    )

    assert first == second
    assert dump_canonical_lock_toml(parse_canonical_lock_toml(first)) == first
    assert "[images.cuda]" in first
    assert "[python.interpreter]" in first
    assert "[[python.package_groups.pytorch.packages]]" in first
    assert "[[python.uv_tools]]" in first
    assert "[comfyui.requirements]" in first
    assert "[[custom_nodes.registry]]" in first
    assert "[[hooks.build]]" in first
    assert "[[hooks.runtime]]" in first


def test_atomic_groups_expose_one_logical_reconciliation_key() -> None:
    keys = {canonical_entry_key(entry) for entry in _lock().entries}

    assert ("python", "package_groups", "pytorch") in keys
    assert ("python", "package_groups", "application_extras") in keys
    assert ("python", "uv_tools", "comfy-cli") in keys
    assert ("comfyui", "requirements") in keys
    assert ("hooks", "build", "common/setup.sh") in keys


# Parsed domains retain only their owning external identity and canonical
# sorted, unique package projections.
def test_interpreter_result_contains_only_external_artifact_identity() -> None:
    document = dump_canonical_lock_toml(_lock())
    parsed = parse_canonical_lock_toml(document)

    assert parsed.python.interpreter.artifact_key.startswith("cpython-3.13.14-")
    assert parsed.python.interpreter.catalog_digest == DIGEST_C


def test_comfyui_requirements_projection_is_sorted_and_unique() -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        ComfyUIRequirementsLockEntry(
            request_digest=DIGEST_A,
            digest=DIGEST_B,
            pytorch=(
                RoutedPyTorchRequirement(name="torchvision", extras=(), specifier=""),
                RoutedPyTorchRequirement(name="torch", extras=(), specifier=""),
            ),
        )


def test_python_group_requires_sorted_unique_packages() -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        ApplicationExtrasLockEntry(
            request_digest=DIGEST_A,
            packages=(
                ResolvedPythonPackage(name="zipp", extras=(), version="3.23.0"),
                ResolvedPythonPackage(name="numpy", extras=(), version="2.4.1"),
            ),
        )


# Strict parsing rejects schema drift with stable public diagnostics, and parsed
# lock models remain immutable.
def test_grouped_parser_rejects_unknown_current_fields_with_stable_error() -> None:
    document = dump_canonical_lock_toml(_lock()).replace(
        "[images.cuda]\n", "[images.cuda]\nunknown = true\n"
    )

    with pytest.raises(CanonicalLockError, match=INVALID_CANONICAL_LOCK_MESSAGE):
        parse_canonical_lock_toml(document)


def test_grouped_parser_rejects_missing_required_domain_with_stable_error() -> None:
    document = dump_canonical_lock_toml(_lock()).replace(
        "[python.interpreter]", "[python.missing_interpreter]"
    )

    with pytest.raises(CanonicalLockError, match=INVALID_CANONICAL_LOCK_MESSAGE):
        parse_canonical_lock_toml(document)


def test_grouped_models_are_frozen() -> None:
    lock = _lock()

    with pytest.raises(ValidationError, match="frozen"):
        lock.python.interpreter.version = "3.14.6"
