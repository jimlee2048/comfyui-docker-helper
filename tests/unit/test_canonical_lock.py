"""Canonical config-lock v1 contracts."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from itertools import permutations

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config.canonical_lock import (
    INVALID_CANONICAL_LOCK_MESSAGE,
    CanonicalLock,
    CanonicalLockError,
    ComfyCliLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    DirectPythonLockEntry,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciLockEntry,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    ProtectedRequirementProjection,
    PyTorchCompatibilityLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    RegistryRequestIdentity,
    compute_request_digest,
    dump_canonical_lock_toml,
    parse_canonical_lock_toml,
)
from comfyui_docker_helper.exact_ledger import COMFYUI_FLOOR_COMMIT

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
DIGEST_C = f"sha256:{'c' * 64}"
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40


def _comfy_cli_request(**changes: str) -> ComfyCliRequestIdentity:
    values = {
        "type": "comfy-cli",
        "package": "comfy-cli",
        "policy": "highest-target-compatible-stable",
        "minimum_version": "1.7.0",
        "environment": "uv-tool:comfy-cli",
        "index_url": "https://pypi.org/simple",
        "python_version": "3.13.14",
        "platform": "linux/amd64",
    }
    values.update(changes)
    return ComfyCliRequestIdentity.model_validate(values)


def _request_digests() -> dict[str, str]:
    return {
        "oci": compute_request_digest(
            OciRequestIdentity(
                type="oci",
                role="cuda-base",
                repository="nvidia/cuda",
                tag="13.0.3-cudnn-devel-ubuntu24.04",
                platform="linux/amd64",
            )
        ),
        "python": compute_request_digest(
            ManagedPythonRequestIdentity(
                type="managed-python",
                version="3.13.14",
                implementation="cpython",
                platform="linux/amd64",
                libc="gnu",
                catalog_descriptor_digest=DIGEST_B,
            )
        ),
        "comfyui": compute_request_digest(
            ComfyUIRequestIdentity(
                type="comfyui",
                repository="https://github.com/Comfy-Org/ComfyUI.git",
                selector="v0.11.0",
            )
        ),
        "cli": compute_request_digest(_comfy_cli_request()),
        "registry": compute_request_digest(
            RegistryRequestIdentity(
                type="registry", id="example-node", selector="latest"
            )
        ),
        "git": compute_request_digest(
            DirectGitRequestIdentity(
                type="git", url="https://example.test/node.git", ref="main"
            )
        ),
        "package": compute_request_digest(
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
                members=[
                    DirectPythonRequestMember(
                        package="torch", extras=[], selector="==2.12.1"
                    ),
                    DirectPythonRequestMember(
                        package="torchvision",
                        extras=["image"],
                        selector="==0.27.1",
                    ),
                ],
            )
        ),
    }


def _lock() -> CanonicalLock:
    digests = _request_digests()
    return CanonicalLock(
        schema_version=1,
        entries=[
            LocalExecutableLockEntry(
                type="local-executable",
                relative_path="custom/hooks/pre.py",
                digest=DIGEST_C,
            ),
            RegistryNodeLockEntry(
                type="registry",
                request_digest=digests["registry"],
                id="example-node",
                version="1.2.3",
            ),
            OciLockEntry(
                type="oci",
                request_digest=digests["oci"],
                role="cuda-base",
                repository="nvidia/cuda",
                tag="13.0.3-cudnn-devel-ubuntu24.04",
                descriptor_digest=DIGEST_A,
                descriptor_kind="index",
                platform="linux/amd64",
            ),
            ManagedPythonLockEntry(
                type="managed-python",
                request_digest=digests["python"],
                version="3.13.14",
                implementation="cpython",
                platform="linux/amd64",
                libc="gnu",
                provider="uv-managed",
                catalog_descriptor_digest=DIGEST_B,
                catalog_key="cpython-3.13.14-linux-x86_64-gnu",
                catalog_url="https://example.test/python.tar.zst",
                pip_version="26.1.2",
                cdh_version="0.5.0",
                cdh_source_digest=DIGEST_C,
                uv_build_version="0.11.28",
            ),
            OfficialComfyUILockEntry(
                type="comfyui",
                request_digest=digests["comfyui"],
                repository="https://github.com/Comfy-Org/ComfyUI.git",
                commit=COMMIT_A,
                formal_release="0.11.0",
            ),
            ComfyCliLockEntry(
                type="comfy-cli",
                request_digest=digests["cli"],
                package="comfy-cli",
                version="1.8.0",
                environment="uv-tool:comfy-cli",
            ),
            DirectGitLockEntry(
                type="git",
                request_digest=digests["git"],
                url="https://example.test/node.git",
                commit=COMMIT_B,
            ),
            DirectPythonLockEntry(
                type="python-package",
                request_digest=digests["package"],
                package="torch",
                extras=[],
                version="2.12.1+cu130",
                environment="application",
            ),
            PyTorchCompatibilityLockEntry(
                type="pytorch-compatibility",
                request_digest=digests["package"],
                environment="application",
                setuptools_specifier="<82",
            ),
            DirectPythonLockEntry(
                type="python-package",
                request_digest=digests["package"],
                package="torchvision",
                extras=["image"],
                version="0.27.1+cu130",
                environment="application",
            ),
        ],
    )


# Canonical lock models preserve deterministic exact identities and strict admission.
def test_python_314_managed_identity_round_trips_exactly() -> None:
    request = ManagedPythonRequestIdentity(
        type="managed-python",
        version="3.14.6",
        implementation="cpython",
        platform="linux/amd64",
        libc="gnu",
        catalog_descriptor_digest=DIGEST_B,
    )
    entry = ManagedPythonLockEntry(
        type="managed-python",
        request_digest=compute_request_digest(request),
        version="3.14.6",
        implementation="cpython",
        platform="linux/amd64",
        libc="gnu",
        provider="uv-managed",
        catalog_descriptor_digest=DIGEST_B,
        catalog_key="cpython-3.14.6-linux-x86_64-gnu",
        catalog_url=(
            "https://releases.astral.sh/github/python-build-standalone/releases/"
            "download/20260623/cpython-3.14.6%2B20260623-x86_64-unknown-"
            "linux-gnu-install_only_stripped.tar.gz"
        ),
        pip_version="26.1.2",
        cdh_version="0.5.0",
        cdh_source_digest=DIGEST_C,
        uv_build_version="0.11.28",
    )

    parsed = parse_canonical_lock_toml(
        dump_canonical_lock_toml(
            CanonicalLock(schema_version=1, entries=[entry])
        ).encode()
    )

    assert parsed.entries == (entry,)


def test_requirements_sidecar_round_trips_as_one_strict_canonical_entry() -> None:
    request = ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository="https://github.com/Comfy-Org/ComfyUI.git",
        commit=COMMIT_A,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path="requirements.txt",
        python_version="3.13.14",
        platform="linux/amd64",
        protected_names=["torch", "torchaudio", "torchvision"],
        protected_policy_digest=DIGEST_B,
    )
    entry = ComfyUIRequirementsLockEntry(
        type="comfyui-requirements",
        request_digest=compute_request_digest(request),
        repository=request.repository,
        commit=request.commit,
        floor_commit=request.floor_commit,
        path=request.path,
        python_version=request.python_version,
        platform=request.platform,
        protected_names=request.protected_names,
        protected_policy_digest=request.protected_policy_digest,
        requirements_digest=DIGEST_C,
        protected=[
            ProtectedRequirementProjection(package="torchaudio", extras=[], selector="")
        ],
    )
    lock = CanonicalLock(schema_version=1, entries=[entry])

    assert parse_canonical_lock_toml(dump_canonical_lock_toml(lock)) == lock
    changed = lock.model_dump(mode="python")
    changed["entries"][0]["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CanonicalLock.model_validate(changed)


def test_complete_lock_round_trips_and_is_byte_deterministic() -> None:
    lock = _lock()

    first = dump_canonical_lock_toml(lock)
    second = dump_canonical_lock_toml(
        CanonicalLock(schema_version=1, entries=list(reversed(lock.entries)))
    )

    assert first == second
    assert dump_canonical_lock_toml(parse_canonical_lock_toml(first)) == first
    assert first.startswith("schema_version = 1\n")
    assert first.count("[[entries]]") == 10


def test_discriminator_selects_every_entry_variant() -> None:
    parsed = parse_canonical_lock_toml(dump_canonical_lock_toml(_lock()))

    assert {type(entry) for entry in parsed.entries} == {
        OciLockEntry,
        ManagedPythonLockEntry,
        OfficialComfyUILockEntry,
        ComfyCliLockEntry,
        RegistryNodeLockEntry,
        DirectGitLockEntry,
        DirectPythonLockEntry,
        PyTorchCompatibilityLockEntry,
        LocalExecutableLockEntry,
    }


def test_every_cohesive_group_member_carries_the_shared_request_digest() -> None:
    packages = [
        entry for entry in _lock().entries if isinstance(entry, DirectPythonLockEntry)
    ]

    assert {entry.package for entry in packages} == {"torch", "torchvision"}
    assert len({entry.request_digest for entry in packages}) == 1


@pytest.mark.parametrize(
    "document",
    [
        "schema_version = 1\nentries = []\nunknown = true\n",
        "schema_version = 2\nentries = []\n",
        "schema_version = 0\nentries = []\n",
        "schema_version = 1\n[[entries]]\ntype = 'oci'\n",
        "schema_version = 1\n[[entries]]\ntype = 'unknown'\n",
        "schema_version = 1\nentries = [\n",
        b"\xff",
    ],
    ids=[
        "extra-root",
        "future-version",
        "old-version",
        "missing-required-fields",
        "unknown-discriminator",
        "invalid-current-toml",
        "invalid-utf8",
    ],
)
def test_invalid_lock_always_uses_one_remove_regenerate_error(
    document: str | bytes,
) -> None:
    with pytest.raises(CanonicalLockError, match=INVALID_CANONICAL_LOCK_MESSAGE):
        parse_canonical_lock_toml(document)


def test_every_model_forbids_extra_fields() -> None:
    data = _lock().model_dump(mode="python")
    data["entries"][0]["unknown"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CanonicalLock.model_validate(data)


def test_required_fields_cannot_be_omitted_from_any_entry() -> None:
    data = _lock().model_dump(mode="python")

    for entry_index, entry in enumerate(data["entries"]):
        for required in entry.keys() - {
            "type",
            "formal_release",
            "resolved_version",
            "setuptools_specifier",
        }:
            invalid = deepcopy(data)
            del invalid["entries"][entry_index][required]
            with pytest.raises(ValidationError, match="Field required"):
                CanonicalLock.model_validate(invalid)


# Request digests encode normalized intent and every cohesive group dimension.
def test_request_digest_changes_only_with_canonical_request_identity() -> None:
    first = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        members=[
            DirectPythonRequestMember(package="numpy", extras=[], selector="<3,>=1")
        ],
    )
    same = DirectPythonRequestIdentity.model_validate(first.model_dump())
    changed = first.model_copy(update={"index_url": "https://index.example.test"})

    assert compute_request_digest(first) == compute_request_digest(same)
    assert compute_request_digest(first) != compute_request_digest(changed)


def test_equivalent_public_selectors_share_normalized_request_digest() -> None:
    prefixed = RegistryRequestIdentity(
        type="registry", id="example-node", selector="v1.2.3"
    )
    canonical = RegistryRequestIdentity(
        type="registry", id="example-node", selector="1.2.3"
    )

    assert prefixed.selector == "1.2.3"
    assert compute_request_digest(prefixed) == compute_request_digest(canonical)


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("environment", "uv-tool:numpy"),
        ("group", "uv-tool"),
        ("python_version", "3.12.13"),
        ("index_url", "https://index.example.test/simple"),
    ],
)
def test_each_python_resolution_dimension_changes_request_digest(
    field: str, changed_value: object
) -> None:
    request = DirectPythonRequestIdentity(
        type="python-group",
        environment="application",
        group="application-extra",
        python_version="3.13.14",
        platform="linux/amd64",
        index_url="https://pypi.org/simple",
        members=[
            DirectPythonRequestMember(package="numpy", extras=[], selector="<3,>=1")
        ],
    )
    data = {**request.model_dump(), field: changed_value}
    if field == "group":
        data["environment"] = "uv-tool:numpy"
    if field == "environment":
        data["group"] = "uv-tool"
    changed = DirectPythonRequestIdentity.model_validate(data)

    assert compute_request_digest(changed) != compute_request_digest(request)


def _pytorch_group() -> PyTorchRequestIdentity:
    return PyTorchRequestIdentity(
        type="pytorch-group",
        environment="application",
        group="pytorch",
        backend="cuda",
        channel="cu130",
        python_version="3.13.14",
        platform="linux/amd64",
        python_index_url="https://pypi.org/simple",
        pytorch_index_url="https://download.pytorch.org/whl/cu130",
        members=[
            DirectPythonRequestMember(package="torch", extras=[], selector="==2.12.1"),
            DirectPythonRequestMember(
                package="torchvision", extras=["image"], selector="==0.27.1"
            ),
        ],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel", "cu129"),
        ("python_index_url", "https://index.example.test/simple"),
        ("pytorch_index_url", "https://download.pytorch.org/whl/cu129"),
        ("python_version", "3.12.13"),
    ],
)
def test_pytorch_request_digest_owns_typed_resolution_dimensions(
    field: str, value: str
) -> None:
    request = _pytorch_group()
    data = {**request.model_dump(), field: value}
    if field == "channel":
        data["pytorch_index_url"] = "https://download.pytorch.org/whl/cu129"
    elif field == "pytorch_index_url":
        data["channel"] = "cu129"
    changed = PyTorchRequestIdentity.model_validate(data)

    assert compute_request_digest(changed) != compute_request_digest(request)


def test_pytorch_request_rejects_channel_source_mismatch() -> None:
    request = _pytorch_group()
    with pytest.raises(ValidationError, match="must end with the derived channel"):
        PyTorchRequestIdentity.model_validate(
            {**request.model_dump(), "channel": "cu129"}
        )


def test_generic_python_group_cannot_impersonate_pytorch_group() -> None:
    data = _pytorch_group().model_dump()
    data.update(type="python-group")
    data.pop("backend")
    data.pop("channel")

    with pytest.raises(ValidationError):
        DirectPythonRequestIdentity.model_validate(data)


@pytest.mark.parametrize(
    "mutate_members",
    [
        lambda members: [
            members[0],
            members[1].model_copy(update={"selector": "<1,>=0.27"}),
        ],
        lambda members: [
            members[0],
            members[1].model_copy(update={"extras": ("image", "video")}),
        ],
        lambda members: [
            *members,
            DirectPythonRequestMember(
                package="torchaudio", extras=[], selector="==2.12.1"
            ),
        ],
        lambda members: [members[0]],
    ],
    ids=["member-selector", "member-extras", "member-added", "member-removed"],
)
def test_any_cohesive_group_membership_change_updates_shared_digest(
    mutate_members: Callable[
        [list[DirectPythonRequestMember]], list[DirectPythonRequestMember]
    ],
) -> None:
    group = _pytorch_group()
    changed_members = mutate_members(group.members)
    changed = PyTorchRequestIdentity.model_validate(
        {**group.model_dump(), "members": changed_members}
    )

    assert compute_request_digest(changed) != compute_request_digest(group)


def test_group_member_order_permutation_property_has_one_digest() -> None:
    initial = _pytorch_group()
    group = PyTorchRequestIdentity.model_validate(
        {
            **initial.model_dump(),
            "members": [
                *initial.members,
                DirectPythonRequestMember(
                    package="torchaudio", extras=[], selector="==2.12.1"
                ),
            ],
        }
    )

    digests = {
        compute_request_digest(
            PyTorchRequestIdentity.model_validate(
                {**group.model_dump(), "members": list(order)}
            )
        )
        for order in permutations(group.members)
    }

    assert len(digests) == 1


def test_group_rejects_duplicate_package_with_different_extras() -> None:
    group = _pytorch_group()
    duplicate = group.members[1].model_copy(update={"extras": ("video",)})

    with pytest.raises(ValidationError, match="each package exactly once"):
        PyTorchRequestIdentity.model_validate(
            {**group.model_dump(), "members": [*group.members, duplicate]}
        )


# Direct-package selectors, extras, environments, and logical keys stay canonical.
@pytest.mark.parametrize("selector", ["===1", "==1.*", "<3,>=1.0rc1"])
def test_direct_python_group_rejects_unsupported_selectors(selector: str) -> None:
    with pytest.raises(ValidationError):
        DirectPythonRequestMember(package="numpy", extras=[], selector=selector)


@pytest.mark.parametrize(
    "selector", ["", "==1.2.3", "!=2,<3,>=1", ">=1", "<3", "~=1.2"]
)
def test_direct_python_group_accepts_internal_pep440_selector_domain(
    selector: str,
) -> None:
    member = DirectPythonRequestMember(package="numpy", extras=[], selector=selector)

    assert member.selector == selector


def test_request_identity_requires_normalized_package_extras_and_environment() -> None:
    common = {
        "type": "python-group",
        "group": "application-extra",
        "python_version": "3.13.14",
        "platform": "linux/amd64",
        "index_url": "https://pypi.org/simple",
    }

    for member, environment in (
        ({"package": "Num_Py", "extras": [], "selector": ""}, "application"),
        (
            {"package": "numpy", "extras": ["z", "a"], "selector": ""},
            "application",
        ),
        ({"package": "numpy", "extras": [], "selector": ""}, "other"),
    ):
        with pytest.raises(ValidationError):
            DirectPythonRequestIdentity(
                **common, environment=environment, members=[member]
            )


def test_lock_rejects_duplicate_logical_identities() -> None:
    lock = _lock()
    duplicate = lock.entries[0].model_copy(update={"digest": DIGEST_A})

    with pytest.raises(ValidationError, match="unique logical identities"):
        CanonicalLock(schema_version=1, entries=[*lock.entries, duplicate])


def test_direct_python_logical_identity_ignores_extras() -> None:
    lock = _lock()
    package = next(
        entry for entry in lock.entries if isinstance(entry, DirectPythonLockEntry)
    )
    duplicate = package.model_copy(update={"extras": ("different",)})

    with pytest.raises(ValidationError, match="unique logical identities"):
        CanonicalLock(schema_version=1, entries=[*lock.entries, duplicate])


@pytest.mark.parametrize(
    ("relative_path", "digest"),
    [
        ("/absolute.py", DIGEST_A),
        ("a/../escape.py", DIGEST_A),
        ("a//b.py", DIGEST_A),
        ("./a.py", DIGEST_A),
        ("a/./b.py", DIGEST_A),
        (".", DIGEST_A),
        ("a\\b.py", DIGEST_A),
        ("a\tb.py", DIGEST_A),
        ("a.py/", DIGEST_A),
        ("a.py", "sha256:ABC"),
    ],
)
def test_local_executable_requires_canonical_path_and_sha256(
    relative_path: str, digest: str
) -> None:
    with pytest.raises(ValidationError):
        LocalExecutableLockEntry(
            type="local-executable", relative_path=relative_path, digest=digest
        )


@pytest.mark.parametrize(
    ("entry_index", "field", "value"),
    [
        (3, "version", "latest"),
        (3, "pip_version", ">=26"),
        (3, "cdh_version", "0.5.0rc1"),
        (5, "version", "latest"),
        (1, "version", ">=1"),
        (7, "version", "<3"),
    ],
    ids=[
        "managed-python-moving",
        "bootstrap-range",
        "cdh-prerelease",
        "comfy-cli-moving",
        "registry-range",
        "direct-python-range",
    ],
)
def test_invalid_current_lock_rejects_non_exact_resolved_versions(
    entry_index: int, field: str, value: str
) -> None:
    data = _lock().model_dump(mode="python")
    data["entries"][entry_index][field] = value

    with pytest.raises(ValidationError):
        CanonicalLock.model_validate(data)


def test_parser_maps_non_exact_current_identity_to_generic_lock_error() -> None:
    document = dump_canonical_lock_toml(_lock()).replace(
        'version = "1.8.0"', 'version = "latest"'
    )

    with pytest.raises(CanonicalLockError, match=INVALID_CANONICAL_LOCK_MESSAGE):
        parse_canonical_lock_toml(document)


# Canonical-lock parsing applies the same safe catalog-key boundary as provider
# admission without constraining safe opaque provider identities.
@pytest.mark.parametrize(
    "catalog_key",
    ["", ".", "..", "../python", "python/key", "python\\key", " key", "key ", "key\n"],
)
def test_managed_python_lock_rejects_unsafe_catalog_key(catalog_key: str) -> None:
    document = dump_canonical_lock_toml(_lock()).replace(
        'catalog_key = "cpython-3.13.14-linux-x86_64-gnu"',
        f"catalog_key = {json.dumps(catalog_key)}",
    )

    with pytest.raises(CanonicalLockError, match=INVALID_CANONICAL_LOCK_MESSAGE):
        parse_canonical_lock_toml(document.encode("utf-8"))


def test_managed_python_lock_preserves_safe_opaque_catalog_key() -> None:
    document = dump_canonical_lock_toml(_lock()).replace(
        'catalog_key = "cpython-3.13.14-linux-x86_64-gnu"',
        'catalog_key = "alternate.catalog+key"',
    )
    parsed = parse_canonical_lock_toml(document.encode("utf-8"))
    python = next(
        item for item in parsed.entries if isinstance(item, ManagedPythonLockEntry)
    )

    assert python.catalog_key == "alternate.catalog+key"


# Domain-specific lock entries preserve exact stable floors and complete versions.
def test_comfy_cli_request_digest_binds_target_index_policy_and_tool_environment() -> (
    None
):
    request = _comfy_cli_request()

    assert request.model_dump(mode="python") == {
        "type": "comfy-cli",
        "package": "comfy-cli",
        "policy": "highest-target-compatible-stable",
        "minimum_version": "1.7.0",
        "environment": "uv-tool:comfy-cli",
        "index_url": "https://pypi.org/simple",
        "python_version": "3.13.14",
        "platform": "linux/amd64",
    }
    assert compute_request_digest(request) != compute_request_digest(
        _comfy_cli_request(index_url="https://packages.example/simple")
    )
    assert compute_request_digest(request) != compute_request_digest(
        _comfy_cli_request(python_version="3.12.13")
    )


@pytest.mark.parametrize("version", ["0.3.99", "0.11.0rc1", "0.11.0.dev1"])
def test_comfyui_formal_release_requires_stable_floor(version: str) -> None:
    data = _lock().model_dump(mode="python")
    comfyui = next(entry for entry in data["entries"] if entry["type"] == "comfyui")
    comfyui["formal_release"] = version

    with pytest.raises(ValidationError, match=r"stable ComfyUI 0.11.0"):
        CanonicalLock.model_validate(data)


@pytest.mark.parametrize(
    "version", ["1.0rc1+vendor", "1.0.dev1+vendor", "1.0+Vendor", "1.0+vendor_1"]
)
def test_direct_python_resolved_version_must_be_canonical_and_stable(
    version: str,
) -> None:
    data = _lock().model_dump(mode="python")
    package = next(
        entry for entry in data["entries"] if entry["type"] == "python-package"
    )
    package["version"] = version

    with pytest.raises(ValidationError, match="stable distribution version"):
        CanonicalLock.model_validate(data)


def test_direct_python_resolved_version_preserves_canonical_local_segment() -> None:
    lock = parse_canonical_lock_toml(dump_canonical_lock_toml(_lock()))
    versions = {
        entry.package: entry.version
        for entry in lock.entries
        if isinstance(entry, DirectPythonLockEntry)
    }

    assert versions == {"torch": "2.12.1+cu130", "torchvision": "0.27.1+cu130"}


@pytest.mark.parametrize("value", [">=70,<82", "<82 ", "latest", ""])
def test_pytorch_compatibility_specifier_must_be_canonical(value: str) -> None:
    entry = next(
        item
        for item in _lock().entries
        if isinstance(item, PyTorchCompatibilityLockEntry)
    )
    with pytest.raises(ValidationError, match="setuptools_specifier"):
        PyTorchCompatibilityLockEntry.model_validate(
            {**entry.model_dump(), "setuptools_specifier": value}
        )


def test_pytorch_compatibility_without_setuptools_requirement_round_trips() -> None:
    lock = _lock()
    compatibility = next(
        item for item in lock.entries if isinstance(item, PyTorchCompatibilityLockEntry)
    )
    entries = tuple(
        compatibility.model_copy(update={"setuptools_specifier": None})
        if item is compatibility
        else item
        for item in lock.entries
    )
    lock = CanonicalLock(schema_version=1, entries=entries)

    parsed = parse_canonical_lock_toml(dump_canonical_lock_toml(lock))

    entry = next(
        item
        for item in parsed.entries
        if isinstance(item, PyTorchCompatibilityLockEntry)
    )
    assert entry.setuptools_specifier is None


def test_uv_oci_requires_exact_resolved_version_and_cuda_forbids_one() -> None:
    cuda = next(item for item in _lock().entries if isinstance(item, OciLockEntry))
    with pytest.raises(ValidationError, match="only uv-tool"):
        OciLockEntry.model_validate(
            {**cuda.model_dump(), "resolved_version": "0.11.28"}
        )
    with pytest.raises(ValidationError, match="only uv-tool"):
        OciLockEntry.model_validate(
            {
                **cuda.model_dump(),
                "role": "uv-tool",
                "repository": "ghcr.io/astral-sh/uv",
                "tag": "latest",
            }
        )


@pytest.mark.parametrize("version", ["1.6.0", "1.8.0rc1", "1.8.0+vendor"])
def test_comfy_cli_lock_requires_stable_public_floor(version: str) -> None:
    digests = _request_digests()

    with pytest.raises(ValidationError):
        ComfyCliLockEntry(
            type="comfy-cli",
            request_digest=digests["cli"],
            package="comfy-cli",
            version=version,
            environment="uv-tool:comfy-cli",
        )


def test_registry_exact_prereleases_remain_valid() -> None:
    digests = _request_digests()
    registry = RegistryNodeLockEntry(
        type="registry",
        request_digest=digests["registry"],
        id="example-node",
        version="1.0.0-rc.1",
    )

    assert registry.version == "1.0.0-rc.1"


@pytest.mark.parametrize("model", ["lock", "request"])
def test_registry_id_rejects_leading_option_marker(model: str) -> None:
    with pytest.raises(ValidationError, match="argv-safe"):
        if model == "lock":
            RegistryNodeLockEntry(
                type="registry",
                request_digest=DIGEST_A,
                id="--help",
                version="1.0.0",
            )
        else:
            RegistryRequestIdentity(type="registry", id="--help", selector="latest")


@pytest.mark.parametrize("model", ["lock", "request"])
@pytest.mark.parametrize("node_id", ["invalid/name", "invalid!name"])
def test_registry_id_requires_valid_project_name(model: str, node_id: str) -> None:
    with pytest.raises(ValidationError, match="valid Registry project name"):
        if model == "lock":
            RegistryNodeLockEntry(
                type="registry",
                request_digest=DIGEST_A,
                id=node_id,
                version="1.0.0",
            )
        else:
            RegistryRequestIdentity(type="registry", id=node_id, selector="latest")


def test_registry_lock_and_request_preserve_raw_valid_id_spelling() -> None:
    entry = RegistryNodeLockEntry(
        type="registry",
        request_digest=DIGEST_A,
        id="Example_Node",
        version="1.0.0",
    )
    request = RegistryRequestIdentity(
        type="registry",
        id="Example_Node",
        selector="latest",
    )

    assert entry.id == "Example_Node"
    assert request.id == "Example_Node"
