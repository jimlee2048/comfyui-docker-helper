"""Canonical Host render-context planning behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.host_render_service_support import (
    COMMIT,
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    FakeAcquirer,
    _config,
    _prepare,
    _tree,
)

import comfyui_docker_helper.config.planning.request as canonical_request_module
from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.planning.canonical_lock import (
    CanonicalLock,
    CudaImageLockEntry,
    DirectGitLockEntry,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    UvImageLockEntry,
    UvToolLockEntry,
    canonical_entry_key,
    compute_request_digest,
    dump_canonical_lock_toml,
    parse_canonical_lock_toml,
)
from comfyui_docker_helper.config.planning.request import CanonicalRequestError
from comfyui_docker_helper.config.planning.resolver import (
    CanonicalAcquisitionError,
    LockPolicy,
    ReconcilePurpose,
)
from comfyui_docker_helper.host.context.service import (
    HostRenderServiceError,
    PlanningOptions,
)
from comfyui_docker_helper.host.planning.acquisition import (
    DockerPythonGroupResolver,
    ProviderIdentityAcquirer,
)
from comfyui_docker_helper.host.planning.providers.oci import (
    DockerEngineOciIdentityProvider,
)
from comfyui_docker_helper.host.planning.providers.python import (
    DockerManagedPythonIdentityProvider,
)


class _NoProviderCalls:
    def __getattr__(self, name):
        raise AssertionError(f"matching lock must not call provider method {name}")


def _write_cross_dependent_incompatible_uv_lock(output: Path) -> None:
    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    data = lock.model_dump(mode="python")
    uv = data["images"]["uv"]
    uv["repository"] = "registry.example.test/wrong/uv"
    uv["tag"] = "wrong"
    uv["digest"] = DIGEST_C
    interpreter = data["python"]["interpreter"]
    interpreter["catalog_digest"] = DIGEST_C
    request = ManagedPythonRequestIdentity(
        type="managed-python",
        version=interpreter["version"],
        implementation="cpython",
        platform=interpreter["platform"],
        libc=interpreter["libc"],
        catalog_descriptor_digest=DIGEST_C,
    )
    interpreter["request_digest"] = compute_request_digest(request)
    malformed = CanonicalLock.model_validate(data)
    (output / "config.lock.toml").write_text(dump_canonical_lock_toml(malformed))


# Host rendering reconciles once and publishes one complete canonical context.
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
        if isinstance(entry, UvToolLockEntry) and entry.name == "ruff"
    )
    plan = json.loads((output / "build-plan.json").read_bytes())
    assert tool_entry.version == "0.15.18"
    assert plan["toolchain"]["tool_store"]["uv_tools"] == [
        {
            "environment": "uv-tool:ruff",
            "extras": [],
            "name": "ruff",
            "version": "0.15.18",
            "direct_reference": None,
        }
    ]
    assert plan["toolchain"]["tool_store"]["comfy_cli"] == {
        "environment": "uv-tool:comfy-cli",
        "executables": ["comfy", "comfy-cli", "comfycli"],
        "name": "comfy-cli",
        "version": "1.8.0",
    }
    assert plan["application"]["comfyui"]["manager"] is None
    dockerfile = (output / "Dockerfile").read_text()
    assert "uv --no-config tool install" in dockerfile
    assert "ruff==0.15.18" in dockerfile


def test_checkout_owned_manager_capability_flows_to_application_plan(
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
    assert "--enable-manager" not in plan["runtime"]["launch_command"]


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
    assert locked.url == locator
    assert locked.commit == COMMIT
    assert planned["url"] == locator
    assert planned["commit"] == locked.commit


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
        "comfyui_docker_helper.host.context.service.build_canonical_request_graph",
        reject_request,
    )

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, tmp_path / "context", FakeAcquirer())

    assert raised.value.diagnostics == (diagnostic,)
    assert isinstance(raised.value.__cause__, CanonicalRequestError)


# Public planning modes reconcile exact identities and control context publication.
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
    lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    assert lock.schema_version == 1
    assert not (output / "config.toml").exists()
    assert first_fake.calls

    before = _tree(output)
    second_fake = FakeAcquirer()
    _prepare(config, output, second_fake, overwrite=True)
    assert second_fake.calls == []
    assert _tree(output) == before


def test_omitted_uv_selector_reconciles_the_rolling_provider_request(
    tmp_path: Path,
) -> None:
    """The public default enters the lock as the rolling provider tag."""

    config = tmp_path / "config.toml"
    config.write_text(_config(uv_version=None))
    output = tmp_path / "context"

    prepared = _prepare(config, output, FakeAcquirer())

    uv_entry = next(
        entry
        for entry in prepared.lock_result.lock.entries
        if isinstance(entry, UvImageLockEntry)
    )
    assert uv_entry.repository == "astral/uv"
    assert uv_entry.tag == "debian-slim"
    assert uv_entry.digest == DIGEST_B
    assert uv_entry.observed_version == "0.11.28"


def test_request_graph_parses_one_source_snapshot_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    original = canonical_request_module.parse_comfyui_requirements
    calls = 0

    def counted_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        canonical_request_module, "parse_comfyui_requirements", counted_parse
    )

    _prepare(config, tmp_path / "context", FakeAcquirer())

    assert calls == 1


def test_target_change_reuses_source_and_refreshes_only_target_owned_groups(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    output = tmp_path / "context"
    config.write_text(_config(python_version="3.13.14"))
    _prepare(config, output, FakeAcquirer())
    before = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())

    config.write_text(_config(python_version="3.14.6"))
    fake = FakeAcquirer()
    prepared = _prepare(config, output, fake, overwrite=True)
    after = prepared.lock_result.lock

    assert fake.calls == ["managed-python", "pytorch-group", "comfy-cli"]
    assert after.comfyui.requirements == before.comfyui.requirements
    assert prepared.plan.application.comfyui.requirements.python_version == "3.14.6"


def test_changed_local_projection_reuses_source_and_obeys_lock_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    output = tmp_path / "context"
    config.write_text(_config())
    _prepare(config, output, FakeAcquirer())
    before = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    monkeypatch.setattr(
        canonical_request_module.CudaBackendAdapter,
        "protected_requirement_names",
        property(lambda _self: ("torch", "torchvision")),
    )

    locked_fake = FakeAcquirer()
    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(
            config,
            output,
            locked_fake,
            options=PlanningOptions(locked=True),
            overwrite=True,
        )

    assert locked_fake.calls == []
    assert [item.code for item in raised.value.diagnostics] == ["lock.locked_mismatch"]

    default_fake = FakeAcquirer()
    prepared = _prepare(config, output, default_fake, overwrite=True)

    assert default_fake.calls == ["pytorch-group"]
    assert prepared.lock_result.lock.comfyui.requirements == before.comfyui.requirements
    assert tuple(
        item.package
        for item in prepared.plan.application.comfyui.requirements.protected
    ) == ("torch", "torchvision")


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
        entry for entry in initial_lock.entries if isinstance(entry, CudaImageLockEntry)
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
        entry for entry in updated_lock.entries if isinstance(entry, CudaImageLockEntry)
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
        canonical_entry_key(entry): entry
        for entry in before_lock.entries
        if canonical_entry_key(entry)[0] != "images"
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
        "oci",
        "pytorch-group",
        "comfy-cli",
    ]
    assert prepared.lock_result.provider_calls == (
        ("images", "cuda"),
        ("images", "uv"),
        ("python", "package_groups", "pytorch"),
        ("python", "uv_tools", "comfy-cli"),
    )
    assert prepared.lock_result.write_intent is False
    after_lock = parse_canonical_lock_toml((output / "config.lock.toml").read_bytes())
    assert {
        canonical_entry_key(entry): entry
        for entry in after_lock.entries
        if canonical_entry_key(entry)[0] != "images"
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


@pytest.mark.parametrize(
    "options",
    [PlanningOptions(), PlanningOptions(check=True), PlanningOptions(locked=True)],
)
def test_matching_lock_modes_do_not_construct_docker_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: PlanningOptions,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())

    def fail_cli_docker_client():
        raise AssertionError("matching lock must not construct CLI DockerClient")

    def fail_engine_docker_client():
        raise AssertionError("matching lock must not construct Engine DockerClient")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.planning.providers.uv.DockerClient",
        fail_cli_docker_client,
    )
    unused = _NoProviderCalls()
    docker_backed = ProviderIdentityAcquirer(
        oci=DockerEngineOciIdentityProvider(fail_engine_docker_client),
        managed_python=DockerManagedPythonIdentityProvider(),
        comfyui=unused,
        registry=unused,
        git=unused,
        python_group=DockerPythonGroupResolver(),
    )

    prepared = _prepare(
        config,
        output,
        docker_backed,
        options=options,
        overwrite=options == PlanningOptions(),
    )

    assert prepared.lock_result.provider_calls == ()


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
        entry for entry in corrected.entries if isinstance(entry, UvImageLockEntry)
    )
    assert uv_entry.repository == "astral/uv"
    assert uv_entry.tag == "0.11.28-debian-slim"
    assert uv_entry.digest == DIGEST_B

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


def test_invalid_locked_requirements_content_uses_lock_invalid_diagnostic(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    path = output / "config.lock.toml"
    lock = parse_canonical_lock_toml(path.read_bytes())
    source_digest = lock.comfyui.requirements.digest
    path.write_text(path.read_text().replace(source_digest, DIGEST_C))
    fake = FakeAcquirer()

    with pytest.raises(HostRenderServiceError) as raised:
        _prepare(config, output, fake, overwrite=True)

    assert raised.value.diagnostics[0].code == "lock.invalid"
    assert fake.calls == []


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

    monkeypatch.setattr(f"comfyui_docker_helper.host.context.service.{target}", fail)
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
            ("config.lock.toml", "images", "uv"),
            "lock.resolve_failed",
            "OCI registry: requested identity was not found",
        ),
    )
