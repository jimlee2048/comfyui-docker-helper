"""Canonical render-service modes and atomic context contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    CanonicalLockEntry,
    ComfyCliLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    DirectPythonLockEntry,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciLockEntry,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    ProtectedRequirementProjection,
    PythonGroupRequestIdentity,
    PyTorchCompatibilityLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    RegistryRequestIdentity,
    compute_request_digest,
    dump_canonical_lock_toml,
    parse_canonical_lock_toml,
)
from comfyui_docker_helper.config.canonical_request import CanonicalRequestError
from comfyui_docker_helper.config.canonical_resolver import (
    AcquiredCanonicalEntries,
    CanonicalAcquisitionError,
    LockPolicy,
    ReconcilePurpose,
)
from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.runtime_config import load_runtime_config
from comfyui_docker_helper.container.runtime_files import (
    build_runtime_file_plan,
    runtime_downloader_settings,
)
from comfyui_docker_helper.container.runtime_hooks import discover_runtime_hooks
from comfyui_docker_helper.exact_ledger import COMFYUI_REPOSITORY
from comfyui_docker_helper.host.canonical_acquisition import (
    LocalExecutableEntryAcquirer,
)
from comfyui_docker_helper.host.identity_providers import (
    FilesystemLocalExecutableIdentityProvider,
)
from comfyui_docker_helper.host.planning_authority import (
    CachingCanonicalAcquirer,
    managed_python_release_inputs,
)
from comfyui_docker_helper.host.render_service import (
    HostRenderServiceError,
    PlanningOptions,
    prepare_render_context,
)

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
DIGEST_C = f"sha256:{'c' * 64}"
COMMIT = "1" * 40


def _config(
    *,
    with_uv_tool: bool = False,
    install_cli: bool = True,
    install_manager: bool = False,
    image_flavor: str = "cudnn-devel",
    image_distro: str = "ubuntu24.04",
) -> str:
    uv_tools = 'uv_tools = ["ruff>=0.15,<0.16"]' if with_uv_tool else ""
    return f"""
[compute_platform]
type = "cuda"
[compute_platform.cuda]
version = "13.0.3"
image_flavor = "{image_flavor}"
image_distro = "{image_distro}"
[python]
version = "3.13.14"
uv_version = "0.11.28"
{uv_tools}
[pytorch]
version = "2.12.1"
extra_packages = ["torchvision==0.27.1"]
[comfyui]
version = "0.11.0"
install_cli = {str(install_cli).lower()}
install_manager = {str(install_manager).lower()}
[build]
tags = ["example:test"]
platforms = ["linux/amd64"]
"""


@dataclass
class FakeAcquirer:
    calls: list[str] = field(default_factory=list)

    def acquire(self, request, request_digest: str) -> AcquiredCanonicalEntries:
        self.calls.append(request.type)
        entries: tuple[CanonicalLockEntry, ...]
        if isinstance(request, OciRequestIdentity):
            entries = (
                OciLockEntry(
                    type="oci",
                    request_digest=request_digest,
                    role=request.role,
                    repository=request.repository,
                    tag=request.tag,
                    descriptor_digest=(
                        DIGEST_B if request.role == "uv-tool" else DIGEST_A
                    ),
                    descriptor_kind="index",
                    platform=request.platform,
                    resolved_version=("0.11.28" if request.role == "uv-tool" else None),
                ),
            )
        elif isinstance(request, ManagedPythonRequestIdentity):
            release = managed_python_release_inputs()
            entries = (
                ManagedPythonLockEntry(
                    type="managed-python",
                    request_digest=request_digest,
                    version=request.version,
                    implementation=request.implementation,
                    platform=request.platform,
                    libc=request.libc,
                    provider="uv-managed",
                    catalog_descriptor_digest=request.catalog_descriptor_digest,
                    catalog_key="cpython-3.13.14-linux-x86_64-gnu",
                    catalog_url="https://example.test/python.tar.zst",
                    pip_version=release.pip_version,
                    cdh_version=release.cdh_version,
                    cdh_source_digest=release.cdh_source_digest,
                    uv_build_version=release.uv_build_version,
                ),
            )
        elif isinstance(request, ComfyUIRequestIdentity):
            entries = (
                OfficialComfyUILockEntry(
                    type="comfyui",
                    request_digest=request_digest,
                    repository=request.repository,
                    commit=COMMIT,
                    formal_release="0.11.0",
                ),
            )
        elif isinstance(request, ComfyUIRequirementsRequestIdentity):
            content = b"torch\ntorchvision\ntorchaudio\n"
            entries = (
                ComfyUIRequirementsLockEntry(
                    type="comfyui-requirements",
                    request_digest=request_digest,
                    repository=request.repository,
                    commit=request.commit,
                    floor_commit=request.floor_commit,
                    path=request.path,
                    python_version=request.python_version,
                    platform=request.platform,
                    protected_names=request.protected_names,
                    protected_policy_digest=request.protected_policy_digest,
                    requirements_digest=(
                        f"sha256:{hashlib.sha256(content).hexdigest()}"
                    ),
                    protected=[
                        ProtectedRequirementProjection(
                            package=name, extras=[], selector=""
                        )
                        for name in ("torch", "torchaudio", "torchvision")
                    ],
                ),
            )
        elif isinstance(request, ComfyCliRequestIdentity):
            entries = (
                ComfyCliLockEntry(
                    type="comfy-cli",
                    request_digest=request_digest,
                    package="comfy-cli",
                    version="1.8.0",
                    environment=request.environment,
                ),
            )
        elif isinstance(request, RegistryRequestIdentity):
            entries = (
                RegistryNodeLockEntry(
                    type="registry",
                    request_digest=request_digest,
                    id=request.id,
                    version="1.0.0",
                ),
            )
        elif isinstance(request, DirectGitRequestIdentity):
            entries = (
                DirectGitLockEntry(
                    type="git",
                    request_digest=request_digest,
                    url=request.url,
                    commit=COMMIT,
                ),
            )
        elif isinstance(request, PythonGroupRequestIdentity):
            versions = {
                "torch": "2.12.1+cu130",
                "torchaudio": "2.11.0+cu130",
                "torchvision": "0.27.1+cu130",
                "ruff": "0.15.18",
            }
            entries = tuple(
                DirectPythonLockEntry(
                    type="python-package",
                    request_digest=request_digest,
                    package=member.package,
                    extras=member.extras,
                    version=versions[member.package],
                    environment=request.environment,
                )
                for member in request.members
            )
            if isinstance(request, PyTorchRequestIdentity):
                entries = (
                    *entries,
                    PyTorchCompatibilityLockEntry(
                        type="pytorch-compatibility",
                        request_digest=request_digest,
                        environment="application",
                        setuptools_specifier="<82",
                    ),
                )
        else:  # pragma: no cover
            raise AssertionError(request)
        return AcquiredCanonicalEntries(entries, True)


# Host rendering reconciles once and atomically publishes one canonical context.
def test_active_uv_tool_flows_from_config_through_lock_plan_and_dockerfile(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config(with_uv_tool=True))
    output = tmp_path / "output"
    fake = FakeAcquirer()

    _prepare(config, output, fake)

    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    tool_entry = next(
        entry
        for entry in lock.entries
        if isinstance(entry, DirectPythonLockEntry)
        and entry.environment == "uv-tool:ruff"
    )
    plan = json.loads((output / "build-plan.json").read_bytes())
    assert tool_entry.version == "0.15.18"
    assert plan["toolchain"]["tool_store"]["uv_tools"] == [
        {
            "environment": "uv-tool:ruff",
            "extras": [],
            "name": "ruff",
            "version": "0.15.18",
        }
    ]
    assert plan["toolchain"]["tool_store"]["comfy_cli"] == {
        "environment": "uv-tool:comfy-cli",
        "executables": ["comfy", "comfy-cli", "comfycli"],
        "inventory_path": "/opt/cdh/build/comfy-cli-inventory.txt",
        "name": "comfy-cli",
        "version": "1.8.0",
    }
    assert plan["application"]["comfyui"]["manager"] is None
    dockerfile = (output / "Dockerfile").read_text()
    assert "uv --no-config tool install" in dockerfile
    assert "ruff==0.15.18" in dockerfile


def test_checkout_owned_manager_capability_flows_only_to_application_phase(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config(install_cli=False, install_manager=True))
    output = tmp_path / "output"

    _prepare(config, output, FakeAcquirer())

    plan = json.loads((output / "build-plan.json").read_bytes())
    manager = plan["application"]["comfyui"]["manager"]
    assert manager == {
        "distribution": "comfyui-manager",
        "entrypoint_name": "cm-cli",
        "executable": "/opt/venv/bin/cm-cli",
        "import_anchor": (
            "/opt/venv/lib/python3.13/site-packages/comfyui-docker-helper-comfyui.pth"
        ),
        "import_name": "comfyui_manager",
        "requirements_path": "manager_requirements.txt",
    }
    assert plan["toolchain"]["tool_store"]["comfy_cli"] is None
    assert plan["custom_nodes"]["user_directory"] == "/workspace/ComfyUI/user"
    assert plan["custom_nodes"]["custom_node_inventory"] == (
        "/opt/cdh/build/custom-node-inventory.json"
    )
    assert "--enable-manager" not in plan["runtime"]["launch_command"]
    phase = json.loads((output / "phases/application.json").read_bytes())
    assert phase["payload"]["comfyui"]["manager"] == manager


def test_rendered_context_preserves_raw_git_locator_from_lock_to_plan(
    tmp_path: Path,
) -> None:
    locator = "ssh://Git@Example.invalid:22/Org/Node.git"
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f'''
[[comfyui.custom_nodes]]
type = "git"
url = "{locator}"
ref = "main"
target_dir = "direct"
'''
    )
    output = tmp_path / "output"

    _prepare(config, output, FakeAcquirer())

    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    locked = next(
        entry for entry in lock.entries if isinstance(entry, DirectGitLockEntry)
    )
    plan = json.loads((output / "build-plan.json").read_bytes())
    planned = plan["custom_nodes"]["nodes"][0]
    phase = json.loads((output / "phases/custom-nodes.json").read_bytes())

    assert locked.url == locator
    assert locked.commit == COMMIT
    assert planned["url"] == locator
    assert planned["commit"] == locked.commit
    assert phase["payload"]["nodes"][0] == planned


@dataclass
class NoLocalInputs:
    def acquire(self, request):  # pragma: no cover - config has no hooks
        raise AssertionError(request)


def _prepare(
    config: Path,
    output: Path,
    fake: FakeAcquirer,
    *,
    options: PlanningOptions | None = None,
    overwrite: bool = False,
    hooks_dir: Path | None = None,
    working_directory: Path | None = None,
    scripts_dir: Path | str = "./scripts",
):
    return prepare_render_context(
        config,
        output,
        scripts_dir=scripts_dir,
        acquirer=CachingCanonicalAcquirer(fake),
        local_acquirer=LocalExecutableEntryAcquirer(
            FilesystemLocalExecutableIdentityProvider()
        ),
        options=options,
        overwrite=overwrite,
        hooks_dir=hooks_dir,
        working_directory=working_directory,
    )


def _write_cross_dependent_incompatible_uv_lock(output: Path) -> None:
    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    data = lock.model_dump(mode="python")
    for entry in data["entries"]:
        if entry["type"] == "oci" and entry["role"] == "uv-tool":
            entry["repository"] = "registry.example.test/wrong/uv"
            entry["tag"] = "wrong"
            entry["descriptor_digest"] = DIGEST_C
        elif entry["type"] == "managed-python":
            entry["catalog_descriptor_digest"] = DIGEST_C
            request = ManagedPythonRequestIdentity(
                type="managed-python",
                version=entry["version"],
                implementation=entry["implementation"],
                platform=entry["platform"],
                libc=entry["libc"],
                catalog_descriptor_digest=DIGEST_C,
            )
            entry["request_digest"] = compute_request_digest(request)
    malformed = CanonicalLock.model_validate(data)
    (output / "config.lock.toml").write_text(dump_canonical_lock_toml(malformed))


@pytest.mark.parametrize(
    ("options", "policy", "purpose", "writes"),
    [
        (PlanningOptions(), LockPolicy.DEFAULT, ReconcilePurpose.APPLY, True),
        (
            PlanningOptions(locked=True),
            LockPolicy.LOCKED,
            ReconcilePurpose.APPLY,
            False,
        ),
        (
            PlanningOptions(upgrade_lock=True),
            LockPolicy.UPGRADE,
            ReconcilePurpose.APPLY,
            True,
        ),
        (
            PlanningOptions(check=True),
            LockPolicy.DEFAULT,
            ReconcilePurpose.CHECK,
            False,
        ),
        (
            PlanningOptions(dry_run=True),
            LockPolicy.DEFAULT,
            ReconcilePurpose.DRY_RUN,
            False,
        ),
        (
            PlanningOptions(locked=True, dry_run=True),
            LockPolicy.LOCKED,
            ReconcilePurpose.DRY_RUN,
            False,
        ),
        (
            PlanningOptions(upgrade_lock=True, dry_run=True),
            LockPolicy.UPGRADE,
            ReconcilePurpose.DRY_RUN,
            False,
        ),
    ],
)
def test_planning_options_map_public_modes_to_policy_purpose_and_writes(
    options: PlanningOptions,
    policy: LockPolicy,
    purpose: ReconcilePurpose,
    writes: bool,
) -> None:
    assert options.policy is policy
    assert options.purpose is purpose
    assert options.writes is writes


@pytest.mark.parametrize(
    "options",
    [
        {"locked": True, "upgrade_lock": True},
        {"check": True, "locked": True},
        {"check": True, "upgrade_lock": True},
        {"check": True, "dry_run": True},
    ],
)
def test_planning_options_reject_conflicting_public_modes(
    options: dict[str, bool],
) -> None:
    with pytest.raises(HostRenderServiceError) as raised:
        PlanningOptions(**options)

    assert raised.value.diagnostics[0].code == "render.options_conflict"


def test_request_diagnostic_is_adapted_without_unexpected_exception_handling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())

    diagnostic = Diagnostic(
        path=("pytorch", "extra_packages"),
        code="pytorch.protected_requirement_conflict",
        message="protected PyTorch requirements conflict",
    )

    def reject_request(*_args, **_kwargs):
        raise CanonicalRequestError((diagnostic,))

    monkeypatch.setattr(
        "comfyui_docker_helper.host.render_service.build_canonical_request_graph",
        reject_request,
    )

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, tmp_path / "context", FakeAcquirer())

    assert raised.value.diagnostics == (diagnostic,)
    assert isinstance(raised.value.__cause__, CanonicalRequestError)


# Public planning modes reconcile exact identities and control publication atomically.
def test_default_writes_canonical_context_and_second_default_reuses_lock(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    first_fake = FakeAcquirer()

    prepared = _prepare(config, output, first_fake)

    assert prepared.plan.toolchain.platform == "linux/amd64"
    assert (output / "build-plan.json").is_file()
    assert (output / "phases/application.json").is_file()
    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    assert lock.schema_version == 1
    assert not (output / "config.toml").exists()
    assert first_fake.calls

    before = _tree(output)
    second_fake = FakeAcquirer()
    _prepare(config, output, second_fake, overwrite=True)
    assert second_fake.calls == []
    assert _tree(output) == before


@pytest.mark.parametrize(
    ("selector_overrides", "expected_tag"),
    [
        ({"image_distro": "ubuntu22.04"}, "13.0.3-cudnn-devel-ubuntu22.04"),
        ({"image_flavor": "runtime"}, "13.0.3-runtime-ubuntu24.04"),
    ],
)
def test_cuda_selector_change_reconciles_one_exact_oci_identity(
    tmp_path: Path,
    selector_overrides: dict[str, str],
    expected_tag: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    initial_lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    initial_cuda = next(
        entry
        for entry in initial_lock.entries
        if isinstance(entry, OciLockEntry) and entry.role == "cuda-base"
    )
    config.write_text(_config(**selector_overrides))

    locked_fake = FakeAcquirer()
    with pytest.raises(HostRenderServiceError) as locked:
        _prepare(
            config,
            output,
            locked_fake,
            options=PlanningOptions(locked=True),
            overwrite=True,
        )
    assert locked_fake.calls == []
    assert [item.code for item in locked.value.diagnostics] == ["lock.locked_mismatch"]

    update_fake = FakeAcquirer()
    prepared = _prepare(config, output, update_fake, overwrite=True)
    assert update_fake.calls == ["oci"]
    updated_lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    updated_cuda = next(
        entry
        for entry in updated_lock.entries
        if isinstance(entry, OciLockEntry) and entry.role == "cuda-base"
    )
    assert updated_cuda.tag == expected_tag
    assert updated_cuda.request_digest != initial_cuda.request_digest
    assert updated_cuda.request_digest == compute_request_digest(
        OciRequestIdentity(
            type="oci",
            role="cuda-base",
            repository="nvidia/cuda",
            tag=expected_tag,
            platform="linux/amd64",
        )
    )
    assert prepared.plan.toolchain.pytorch_channel == "cu130"
    assert prepared.plan.toolchain.cuda_image.reference == (
        f"nvidia/cuda:{updated_cuda.tag}@{DIGEST_A}"
    )
    assert (
        f"FROM --platform=linux/amd64 nvidia/cuda:{updated_cuda.tag}@{DIGEST_A}"
        in (output / "Dockerfile").read_text()
    )

    reuse_fake = FakeAcquirer()
    _prepare(config, output, reuse_fake, overwrite=True)
    assert reuse_fake.calls == []


def test_upgrade_refreshes_only_moving_requests_and_preserves_exact_results(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before_lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    before_exact = {
        (entry.type, getattr(entry, "package", None)): entry
        for entry in before_lock.entries
        if entry.type not in {"oci"}
    }
    fake = FakeAcquirer()

    prepared = _prepare(
        config,
        output,
        fake,
        options=PlanningOptions(upgrade_lock=True),
        overwrite=True,
    )

    assert fake.calls == [
        "oci",
        "comfyui-requirements",
        "comfy-cli",
        "oci",
        "pytorch-group",
    ]
    assert prepared.lock_result.provider_calls == (
        ("comfy-cli", "comfy-cli", "uv-tool:comfy-cli"),
        ("comfyui-requirements", COMFYUI_REPOSITORY),
        ("oci", "cuda-base"),
        ("oci", "uv-tool"),
        ("python-package", "application", "torch"),
    )
    assert prepared.lock_result.write_intent is False
    after_lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    assert {
        (entry.type, getattr(entry, "package", None)): entry
        for entry in after_lock.entries
        if entry.type not in {"oci"}
    } == before_exact


def test_locked_performs_zero_provider_calls_and_zero_writes(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before = _tree(output)
    fake = FakeAcquirer()

    prepared = _prepare(
        config, output, fake, options=PlanningOptions(locked=True), overwrite=True
    )

    assert fake.calls == []
    assert prepared.lock_result.write_intent is False
    assert _tree(output) == before


def test_locked_rejects_stale_context_for_config_only_build_changes(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before = _tree(output)
    config.write_text(
        _config().replace(
            'tags = ["example:test"]',
            'tags = ["cli:first", "cli:second"]\noutput = "push"',
        )
    )
    fake = FakeAcquirer()

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, fake, options=PlanningOptions(locked=True))

    assert raised.value.diagnostics[0].code == "render.context_changed"
    assert fake.calls == []
    assert _tree(output) == before


def test_uv_descriptor_pre_reuse_validates_cross_dependent_lock_in_every_mode(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    _write_cross_dependent_incompatible_uv_lock(output)

    default_fake = FakeAcquirer()
    prepared = _prepare(config, output, default_fake, overwrite=True)

    assert default_fake.calls == ["oci", "managed-python"]
    assert prepared.plan.toolchain.uv_image.descriptor_digest == DIGEST_B
    assert prepared.plan.toolchain.python.catalog_descriptor_digest == DIGEST_B
    corrected = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    uv_entry = next(
        entry
        for entry in corrected.entries
        if isinstance(entry, OciLockEntry) and entry.role == "uv-tool"
    )
    assert uv_entry.repository == "ghcr.io/astral-sh/uv"
    assert uv_entry.tag == "0.11.28"
    assert uv_entry.descriptor_digest == DIGEST_B

    _write_cross_dependent_incompatible_uv_lock(output)
    malformed_tree = _tree(output)
    check_fake = FakeAcquirer()
    with pytest.raises(HostRenderServiceError) as checked:
        _prepare(
            config,
            output,
            check_fake,
            options=PlanningOptions(check=True),
        )
    assert checked.value.diagnostics[0].code == "render.context_changed"
    assert check_fake.calls == ["oci", "managed-python"]
    assert _tree(output) == malformed_tree

    locked_fake = FakeAcquirer()
    with pytest.raises(HostRenderServiceError) as locked:
        _prepare(
            config,
            output,
            locked_fake,
            options=PlanningOptions(locked=True),
        )
    assert locked_fake.calls == []
    assert locked.value.diagnostics
    assert all(item.code == "lock.locked_mismatch" for item in locked.value.diagnostics)
    assert _tree(output) == malformed_tree


def test_dry_run_resolves_without_writing_and_check_compares_without_writing(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    dry_output = tmp_path / "dry"
    dry = _prepare(
        config,
        dry_output,
        FakeAcquirer(),
        options=PlanningOptions(dry_run=True),
    )
    assert dry.lock_result.changed
    assert not dry_output.exists()

    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before = _tree(output)
    _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))
    assert _tree(output) == before
    (output / "Dockerfile").write_text("changed")
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))
    assert raised.value.diagnostics[0].code == "render.context_changed"


def test_malformed_lock_fails_generically(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    output.mkdir()
    (output / "config.lock.toml").write_text("[invalid]\nvalue='malformed'\n")

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer())

    assert raised.value.diagnostics[0].code == "lock.invalid"
    assert "remove" in raised.value.diagnostics[0].message


# Runtime hooks/files preserve locked projection, precedence, and source containment.
def test_runtime_hooks_are_locked_planned_materialized_and_consumed(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = _runtime_hooks(tmp_path / "hooks")
    output = tmp_path / "context"

    prepared = _prepare(config, output, FakeAcquirer(), hooks_dir=hooks)

    assert [hook.relative_path for hook in prepared.plan.runtime.hooks] == [
        "pre-start.d/10-pre.sh",
        "post-start.d/20-post.py",
        "stop.d/30-stop.sh",
    ]
    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    local_entries = [
        entry for entry in lock.entries if isinstance(entry, LocalExecutableLockEntry)
    ]
    assert [entry.relative_path for entry in local_entries] == [
        "runtime-hooks/post-start.d/20-post.py",
        "runtime-hooks/pre-start.d/10-pre.sh",
        "runtime-hooks/stop.d/30-stop.sh",
    ]
    assert (output / "runtime/hooks/pre-start.d/10-pre.sh").read_text() == "pre\n"
    dockerfile = (output / "Dockerfile").read_text()
    assert "COPY runtime/config.toml /opt/cdh/runtime/config.toml" in dockerfile
    assert "COPY runtime/hooks /opt/cdh/runtime/hooks" in dockerfile
    runtime = load_runtime_config(
        baked_config_path=output / "runtime/config.toml",
        mounted_config_path=tmp_path / "missing.toml",
        environ={},
    )
    assert runtime.config.comfyui.port == 8188
    discovered = discover_runtime_hooks(
        baked_hooks_path=output / "runtime/hooks",
        mounted_hooks_path=tmp_path / "missing-hooks",
    )
    assert [hook.filename for hook in discovered.hooks] == [
        "10-pre.sh",
        "20-post.py",
        "30-stop.sh",
    ]


def test_runtime_file_projection_preserves_global_and_item_precedence(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + """
[cdh]
default_downloader = "httpx"
default_download_mode = "async"

[[files]]
url = "https://example.test/implicit.bin"
dir = "models"
filename = "implicit.bin"

[[files]]
url = "https://example.test/explicit.bin"
dir = "models"
filename = "explicit.bin"
downloader = "httpx"
download_mode = "async"
"""
    )
    output = tmp_path / "context"

    prepared = _prepare(config, output, FakeAcquirer())

    assert prepared.plan.files.downloader.default == "httpx"
    assert prepared.plan.files.default_download_mode == "async"
    assert [item.downloader_explicit for item in prepared.plan.files.files] == [
        False,
        True,
    ]
    assert [item.download_mode_explicit for item in prepared.plan.files.files] == [
        False,
        True,
    ]
    baked_runtime = load_runtime_config(
        baked_config_path=output / "runtime/config.toml",
        mounted_config_path=tmp_path / "missing.toml",
        environ={},
    )
    assert baked_runtime.config.cdh.default_downloader == "httpx"
    assert baked_runtime.config.cdh.default_download_mode == "async"

    runtime = load_runtime_config(
        baked_config_path=output / "runtime/config.toml",
        mounted_config_path=tmp_path / "missing.toml",
        environ={
            "CDH_DEFAULT_DOWNLOADER": "aria2",
            "CDH_DEFAULT_DOWNLOAD_MODE": "sync",
        },
    )
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    runtime_plan = build_runtime_file_plan(
        runtime.file_documents,
        comfyui_path=comfyui,
        default_download_mode=runtime.config.cdh.default_download_mode,
    )

    assert runtime_downloader_settings(runtime.config).default == "aria2"
    assert runtime_plan.items[0].downloader is None
    assert runtime_plan.items[0].download_mode == "sync"
    assert runtime_plan.items[1].downloader == "httpx"
    assert runtime_plan.items[1].download_mode == "async"


@pytest.mark.parametrize("source_kind", ["custom", "runtime"])
@pytest.mark.parametrize("relation", ["equal", "output-descendant", "output-ancestor"])
def test_render_rejects_every_source_output_overlap_before_overwrite(
    tmp_path: Path,
    source_kind: str,
    relation: str,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / ("scripts" if source_kind == "custom" else "hooks")
    workspace.mkdir()
    if source_kind == "custom":
        hook = source / "hook.sh"
        hook.parent.mkdir()
        hook.write_text("sentinel\n")
        config_text = (
            _config()
            + """
[[comfyui.custom_nodes]]
type = "git"
url = "https://example.test/node.git"
ref = "1111111111111111111111111111111111111111"
pre_install_scripts = ["hook.sh"]
"""
        )
        scripts_dir = source
        hooks_dir = None
    else:
        hook = source / "pre-start.d/10-hook.sh"
        hook.parent.mkdir(parents=True)
        hook.write_text("sentinel\n")
        config_text = _config()
        scripts_dir = tmp_path / "unused-scripts"
        hooks_dir = source
    hook.chmod(0o644)
    config = tmp_path / f"{source_kind}.toml"
    config.write_text(config_text)
    output = {
        "equal": source,
        "output-descendant": source / "context",
        "output-ancestor": workspace,
    }[relation]

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            overwrite=True,
            scripts_dir=scripts_dir,
            hooks_dir=hooks_dir,
        )

    assert raised.value.diagnostics[0].code == "render.input_output_overlap"
    assert hook.read_text() == "sentinel\n"


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("construct_build_plan", ValueError("injected constructor bug")),
        ("reconcile_canonical_lock", AssertionError("injected resolver bug")),
    ],
)
def test_unexpected_planning_failures_propagate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    error: Exception,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(f"comfyui_docker_helper.host.render_service.{target}", fail)
    with pytest.raises(type(error), match="injected"):
        _prepare(config, tmp_path / "context", FakeAcquirer())


def test_uv_pre_reconcile_expected_acquisition_failure_is_diagnostic(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())

    class FailingAcquirer:
        def acquire(self, request, request_digest):
            raise CanonicalAcquisitionError(
                "OCI registry: requested identity was not found"
            )

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, tmp_path / "context", FailingAcquirer())

    assert raised.value.diagnostics == (
        Diagnostic(
            ("config.lock.toml", "oci", "uv-tool"),
            "lock.resolve_failed",
            "OCI registry: requested identity was not found",
        ),
    )


def test_custom_node_and_runtime_hook_lock_namespaces_cannot_collide(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + """
[[comfyui.custom_nodes]]
type = "git"
url = "https://example.test/node.git"
ref = "1111111111111111111111111111111111111111"
pre_install_scripts = ["runtime-hooks/pre-start.d/10-pre.sh"]
"""
    )
    scripts = tmp_path / "scripts"
    custom_hook = scripts / "runtime-hooks/pre-start.d/10-pre.sh"
    custom_hook.parent.mkdir(parents=True)
    custom_hook.write_text("custom\n")
    custom_hook.chmod(0o755)
    hooks = _runtime_hooks(tmp_path / "hooks")
    output = tmp_path / "context"

    _prepare(
        config,
        output,
        FakeAcquirer(),
        hooks_dir=hooks,
        scripts_dir=scripts,
    )

    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    paths = {
        entry.relative_path
        for entry in lock.entries
        if isinstance(entry, LocalExecutableLockEntry)
    }
    custom_identity = "custom-node-hooks/runtime-hooks/pre-start.d/10-pre.sh"
    runtime_identity = "runtime-hooks/pre-start.d/10-pre.sh"
    assert paths == {
        custom_identity,
        runtime_identity,
        "runtime-hooks/post-start.d/20-post.py",
        "runtime-hooks/stop.d/30-stop.sh",
    }
    assert custom_identity != runtime_identity
    assert (output / "inputs/runtime-hooks/pre-start.d/10-pre.sh").read_text() == (
        "custom\n"
    )
    assert (output / "runtime/hooks/pre-start.d/10-pre.sh").read_text() == "pre\n"


# Hook-tree changes respect every no-write mode and closed filesystem validation.
def test_runtime_hook_change_add_delete_obey_all_no_write_modes(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = _runtime_hooks(tmp_path / "hooks")
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer(), hooks_dir=hooks)
    before = _tree(output)
    before_binding = (output / "manifest-binding.json").read_bytes()
    before_lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    before_request_digests = {
        entry.request_digest
        for entry in before_lock.entries
        if hasattr(entry, "request_digest")
    }

    changed = hooks / "pre-start.d/10-pre.sh"
    changed.write_text("changed\n")
    changed.chmod(0o755)
    locked_fake = FakeAcquirer()
    with pytest.raises(HostRenderServiceError) as locked:
        _prepare(
            config,
            output,
            locked_fake,
            hooks_dir=hooks,
            options=PlanningOptions(locked=True),
        )
    assert locked.value.diagnostics[0].code == "lock.locked_mismatch"
    assert locked_fake.calls == []
    check_fake = FakeAcquirer()
    with pytest.raises(HostRenderServiceError) as checked:
        _prepare(
            config,
            output,
            check_fake,
            hooks_dir=hooks,
            options=PlanningOptions(check=True),
        )
    assert checked.value.diagnostics[0].code == "render.context_changed"
    assert check_fake.calls == []
    dry_fake = FakeAcquirer()
    dry = _prepare(
        config,
        output,
        dry_fake,
        hooks_dir=hooks,
        options=PlanningOptions(dry_run=True),
    )
    assert dry.lock_result.changed
    assert dry_fake.calls == []
    assert _tree(output) == before

    added = hooks / "pre-start.d/11-added.py"
    added.write_text("print('added')\n")
    added.chmod(0o755)
    _prepare(config, output, FakeAcquirer(), hooks_dir=hooks, overwrite=True)
    updated_lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    assert {
        entry.request_digest
        for entry in updated_lock.entries
        if hasattr(entry, "request_digest")
    } == before_request_digests
    assert (output / "manifest-binding.json").read_bytes() != before_binding
    assert (output / "runtime/hooks/pre-start.d/11-added.py").is_file()

    added.unlink()
    with pytest.raises(HostRenderServiceError) as deleted_check:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            hooks_dir=hooks,
            options=PlanningOptions(check=True),
        )
    assert deleted_check.value.diagnostics[0].code == "render.context_changed"
    _prepare(config, output, FakeAcquirer(), hooks_dir=hooks, overwrite=True)
    assert not (output / "runtime/hooks/pre-start.d/11-added.py").exists()


def test_runtime_hook_tree_rejects_symlinks_and_special_files(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = _runtime_hooks(tmp_path / "hooks")
    target = hooks / "pre-start.d/10-pre.sh"
    target.unlink()
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(HostRenderServiceError) as symlinked:
        _prepare(config, tmp_path / "context", FakeAcquirer(), hooks_dir=hooks)
    assert symlinked.value.diagnostics[0].code == "runtime_hooks.symlink"

    target.unlink()
    os.mkfifo(target)
    with pytest.raises(HostRenderServiceError) as special:
        _prepare(config, tmp_path / "context", FakeAcquirer(), hooks_dir=hooks)
    assert special.value.diagnostics[0].code == "runtime_hooks.special_file"


def test_default_runtime_hook_tree_is_active_when_present(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    _runtime_hooks(tmp_path / "hooks")

    prepared = _prepare(
        config,
        tmp_path / "context",
        FakeAcquirer(),
        working_directory=tmp_path,
    )

    assert len(prepared.plan.runtime.hooks) == 3


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing", "runtime_hooks.source_not_directory"),
        ("unknown", "runtime_hooks.unknown_top_level"),
        ("extension", "runtime_hooks.unsupported_extension"),
    ],
)
def test_runtime_hook_tree_reports_closed_validation_contract(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = tmp_path / "hooks"
    if mutation != "missing":
        hooks.mkdir()
    if mutation == "unknown":
        (hooks / "README.md").write_text("unknown")
    elif mutation == "extension":
        phase = hooks / "pre-start.d"
        phase.mkdir()
        path = phase / "10-hook.txt"
        path.write_text("hook")
        path.chmod(0o755)
    fake = FakeAcquirer()

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, tmp_path / "context", fake, hooks_dir=hooks)

    assert raised.value.diagnostics[0].code == code
    assert fake.calls == []


def test_runtime_hook_tree_accepts_regular_0644_files(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = tmp_path / "hooks/pre-start.d"
    hooks.mkdir(parents=True)
    hook = hooks / "10-hook.sh"
    hook.write_text("hook\n")
    hook.chmod(0o644)

    prepared = _prepare(
        config, tmp_path / "context", FakeAcquirer(), hooks_dir=hooks.parent
    )

    assert prepared.plan.runtime.hooks[0].relative_path == "pre-start.d/10-hook.sh"
    assert (tmp_path / "context/runtime/hooks/pre-start.d/10-hook.sh").read_text() == (
        "hook\n"
    )


# Context replacement owns unique staging/backup paths and preserves foreign siblings.
def test_overwrite_uses_unique_owned_backup_and_preserves_sibling(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    sibling = tmp_path / ".context.previous"
    sibling.mkdir()
    sentinel = sibling / "sentinel"
    sentinel.write_text("keep")
    backup_sibling = tmp_path / ".context.backup-user"
    backup_sibling.mkdir()
    backup_sentinel = backup_sibling / "sentinel"
    backup_sentinel.write_text("also keep")

    _prepare(config, output, FakeAcquirer(), overwrite=True)

    assert sentinel.read_text() == "keep"
    assert backup_sentinel.read_text() == "also keep"
    assert list(tmp_path.glob(".context.backup-*")) == [backup_sibling]


def test_stage_rename_failure_restores_existing_context_and_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before = _tree(output)
    sibling = tmp_path / ".context.previous"
    sibling.write_text("keep")
    original_rename = Path.rename

    def fail_stage(self: Path, target: Path):
        if self.name.startswith(".context.stage-") and Path(target) == output:
            raise OSError("stage rename denied")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_stage)
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer(), overwrite=True)

    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    assert _tree(output) == before
    assert sibling.read_text() == "keep"
    assert not list(tmp_path.glob(".context.backup-*"))


def test_owned_backup_cleanup_failure_never_touches_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    sibling = tmp_path / ".context.previous"
    sibling.write_text("keep")
    original_rmtree = shutil.rmtree

    def fail_backup(path, *args, **kwargs):
        if Path(path).name.startswith(".context.backup-"):
            raise OSError("cleanup denied")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", fail_backup)
    _prepare(config, output, FakeAcquirer(), overwrite=True)

    assert sibling.read_text() == "keep"
    assert _valid_context(output)
    assert len(list(tmp_path.glob(".context.backup-*"))) == 1


def test_restore_rename_failure_retains_original_in_owned_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    before = _tree(output)
    sibling = tmp_path / ".context.previous"
    sibling.write_text("keep")
    original_rename = Path.rename

    def fail_stage_and_restore(self: Path, target: Path):
        target = Path(target)
        if self.name.startswith(".context.stage-") and target == output:
            raise OSError("stage rename denied")
        if self.name == "previous" and target == output:
            raise OSError("restore rename denied")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_stage_and_restore)
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, FakeAcquirer(), overwrite=True)

    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    backups = list(tmp_path.glob(".context.backup-*"))
    assert len(backups) == 1
    assert _tree(backups[0] / "previous") == before
    assert sibling.read_text() == "keep"


def test_context_parent_filesystem_failure_is_stable_render_diagnostic(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    parent = tmp_path / "not-a-directory"
    parent.write_text("sentinel")

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, parent / "context", FakeAcquirer())

    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    assert parent.read_text() == "sentinel"


@pytest.mark.parametrize("mutation", ["extra-dir", "missing-dir", "symlink", "special"])
def test_check_compares_complete_path_type_and_bytes_without_following(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    outside = tmp_path / "outside"
    outside.write_text("outside sentinel")
    if mutation == "extra-dir":
        (output / "extra-empty").mkdir()
    elif mutation == "missing-dir":
        shutil.rmtree(output / "runtime")
    elif mutation == "symlink":
        (output / "Dockerfile").unlink()
        (output / "Dockerfile").symlink_to(outside)
    else:
        os.mkfifo(output / "extra-special")

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            options=PlanningOptions(check=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"
    assert outside.read_text() == "outside sentinel"


def test_check_detects_materialized_hook_permission_drift(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    hooks = _runtime_hooks(tmp_path / "hooks")
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer(), hooks_dir=hooks)
    rendered = output / "runtime/hooks/pre-start.d/10-pre.sh"
    assert rendered.stat().st_mode & 0o777 == 0o755
    rendered.chmod(0o644)

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            FakeAcquirer(),
            hooks_dir=hooks,
            options=PlanningOptions(check=True),
        )

    assert raised.value.diagnostics[0].code == "render.context_changed"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _runtime_hooks(root: Path) -> Path:
    files = {
        "pre-start.d/10-pre.sh": "pre\n",
        "post-start.d/20-post.py": "print('post')\n",
        "stop.d/30-stop.sh": "stop\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)
    return root


def _valid_context(output: Path) -> bool:
    return (output / ".cdh-rendered").is_file() and (output / "Dockerfile").is_file()
