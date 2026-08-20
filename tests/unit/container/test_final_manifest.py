"""Linux image-helper final-manifest execution contracts."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    LocalFilePlan,
    build_plan_digest,
    dump_build_plan_json,
    manifest_binding,
)
from comfyui_docker_helper.config.canonical_lock import DirectPythonRequestMember
from comfyui_docker_helper.config.custom_node_inventory import custom_node_inventory
from comfyui_docker_helper.config.final_manifest import (
    DistributionVersionEvidence,
    LocalFileEvidence,
    ProtectedRequirementEvidence,
    dump_final_manifest,
    final_build_check_ids,
)
from comfyui_docker_helper.container import final_manifest as final_manifest_service
from comfyui_docker_helper.container.build_plan_input import BuildPlanInputAdmission
from comfyui_docker_helper.container.final_manifest import FinalManifestError
from comfyui_docker_helper.container.final_manifest_writer import (
    FinalManifestWriteError,
)
from comfyui_docker_helper.container.helper_events import (
    ContainerHelperEvent,
    ContainerHelperPhase,
    ContainerHelperPhaseCompleted,
    ContainerHelperPhaseStarted,
    FinalManifestCompleted,
)
from tests.build_plan_support import accepted_resolution, build_plan, final_config
from tests.final_manifest_support import manifest_for_plan


def _plan_with_local_file(*, locked: bool) -> BuildPlan:
    plan = build_plan(final_config(), accepted_resolution())
    relative_target = "models/model.bin"
    local = LocalFilePlan(
        type="local",
        target="/workspace/ComfyUI/models/model.bin",
        relative_target=relative_target,
        context_path=(
            "build/files/" + hashlib.sha256(relative_target.encode("utf-8")).hexdigest()
        ),
        verification="sha256" if locked else "unverified-local",
        digest=f"sha256:{'a' * 64}" if locked else None,
    )
    return plan.model_copy(
        update={"files": plan.files.model_copy(update={"files": (local,)})}
    )


def test_final_observation_projects_protected_request_specifiers() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    protected = tuple(
        DirectPythonRequestMember(
            package=item.package,
            extras=item.extras,
            specifier=item.selector,
        )
        for item in plan.application.comfyui.requirements.protected
    )

    evidence = final_manifest_service._protected_requirement_evidence(
        protected,
        plan.application.comfyui.requirements.protected,
    )

    assert evidence == tuple(
        ProtectedRequirementEvidence(
            package=item.package,
            extras=item.extras,
            selector=item.selector,
        )
        for item in plan.application.comfyui.requirements.protected
    )


def test_final_observation_rejects_protected_request_name_mismatch() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    protected = tuple(
        DirectPythonRequestMember(
            package="torch-runtime" if index == 0 else item.package,
            extras=item.extras,
            specifier=item.selector,
        )
        for index, item in enumerate(plan.application.comfyui.requirements.protected)
    )

    with pytest.raises(
        FinalManifestError,
        match="ComfyUI protected projection does not match BuildPlan",
    ):
        final_manifest_service._protected_requirement_evidence(
            protected,
            plan.application.comfyui.requirements.protected,
        )


# Final comfy-cli evidence executes the tool interpreter and proves its isolation.
def test_comfy_cli_evidence_observes_exact_interpreter_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    projection = BuildPlanInputAdmission(plan).final_manifest()
    tool = projection.toolchain.tool_store.comfy_cli
    assert tool is not None
    python = Path("/opt/uv/tools/comfy-cli/bin/python")
    managed = projection.toolchain.python
    expected_base = (
        Path("/opt/python")
        / managed.catalog_key
        / "bin"
        / f"python{'.'.join(managed.version.split('.')[:2])}"
    )
    calls: list[tuple[tuple[Path | str, ...], Path, str]] = []

    def capture(argv, *, cwd, description):
        calls.append((argv, cwd, description))
        return json.dumps(
            {
                "base_executable": str(expected_base),
                "prefix": "/opt/uv/tools/comfy-cli",
            }
        )

    monkeypatch.setattr(final_manifest_service, "_capture", capture)
    monkeypatch.setattr(
        final_manifest_service,
        "_environment_inventory",
        lambda _python: (("comfy-cli", tool.version),),
    )
    monkeypatch.setattr(
        final_manifest_service, "_dependency_check", lambda *_args: None
    )
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0),
    )
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path, *, strict: Path("/opt/uv/tools/comfy-cli/bin") / path.name,
    )

    evidence = final_manifest_service._comfy_cli_evidence(projection)

    assert evidence is not None
    assert evidence.direct.observed == tool.version
    assert calls[0][0][:3] == (python, "-I", "-c")
    assert calls[0][1:] == (
        Path("/opt/cdh/build"),
        "comfy-cli interpreter identity observation",
    )


def test_configured_uv_tool_evidence_accepts_exact_prerelease_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = "0.16.0rc1"
    monkeypatch.setattr(
        final_manifest_service,
        "_environment_inventory",
        lambda _python: (("ruff", version),),
    )
    monkeypatch.setattr(
        final_manifest_service, "_dependency_check", lambda *_args: None
    )

    evidence = final_manifest_service._tool_evidence(
        "ruff",
        version,
        "uv-tool:ruff",
        Path("/opt/uv/tools/ruff/bin/python"),
    )

    assert isinstance(evidence.direct, DistributionVersionEvidence)
    assert evidence.direct.intended == version
    assert evidence.direct.observed == version


def test_application_evidence_accepts_exact_prerelease_result() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["application"]["python_extras"]["packages"][0]["version"] = "2.4.0rc1"
    plan = BuildPlan.model_validate(document)
    projection = BuildPlanInputAdmission(plan).final_manifest()
    direct = tuple(
        (package.name, package.version)
        for package in (
            *plan.application.pytorch.packages,
            *plan.application.python_extras.packages,
        )
    )

    evidence = final_manifest_service._direct_application_packages(projection, direct)

    numpy = dict(evidence)["numpy"]
    assert isinstance(numpy, DistributionVersionEvidence)
    assert numpy.observed == "2.4.0rc1"


@pytest.mark.parametrize(
    ("identity", "message"),
    [
        (
            {
                "base_executable": "/opt/python/expected/bin/python3.12",
                "prefix": "/tmp/forged-comfy-cli",
            },
            "environment does not match BuildPlan",
        ),
        (
            {
                "base_executable": "/tmp/forged-python",
                "prefix": "/opt/uv/tools/comfy-cli",
            },
            "base interpreter does not match BuildPlan",
        ),
    ],
)
def test_comfy_cli_evidence_rejects_pyvenv_or_base_interpreter_drift(
    monkeypatch: pytest.MonkeyPatch,
    identity: dict[str, str],
    message: str,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    projection = BuildPlanInputAdmission(plan).final_manifest()
    monkeypatch.setattr(
        final_manifest_service,
        "_capture",
        lambda *_args, **_kwargs: json.dumps(identity),
    )

    with pytest.raises(FinalManifestError, match=message):
        final_manifest_service._comfy_cli_evidence(projection)


# Final admission authenticates the plan once and exposes only the final projection.
def test_final_manifest_projection_binds_the_authenticated_plan(tmp_path: Path) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    path = tmp_path / "build-plan.json"
    path.write_bytes(dump_build_plan_json(plan))

    projection = BuildPlanInputAdmission.from_path(
        path,
        expected_build_plan_digest=build_plan_digest(plan),
    ).final_manifest()

    assert projection.binding == manifest_binding(plan)
    assert projection.toolchain == plan.toolchain
    assert projection.application == plan.application
    assert projection.custom_nodes == plan.custom_nodes
    assert tuple(
        (item.url, item.target, item.checksum) for item in projection.files
    ) == tuple((item.url, item.target, item.checksum) for item in plan.files.files)
    assert (
        tuple(
            (item.domain, item.relative_path, item.digest)
            for item in projection.materialized_hooks
        )
        == ()
    )
    assert projection.final_probe.workspace == plan.application.paths.comfyui
    assert projection.final_probe.checks == final_build_check_ids(
        tuple(package.name for package in plan.application.pytorch.packages),
        manager_enabled=plan.application.comfyui.manager is not None,
    )
    assert projection.shutdown_timeout == plan.runtime.shutdown_timeout


@pytest.mark.parametrize("locked", [True, False], ids=["locked", "unlocked"])
def test_local_file_evidence_preserves_declared_verification(
    monkeypatch: pytest.MonkeyPatch,
    locked: bool,
) -> None:
    plan = _plan_with_local_file(locked=locked)
    projection = BuildPlanInputAdmission(plan).final_manifest()
    observations: list[tuple[Path, Path, str | None]] = []

    def verify(*, root: Path, target: Path, expected_checksum: str | None) -> None:
        observations.append((root, target, expected_checksum))

    monkeypatch.setattr(final_manifest_service, "verify_required_final", verify)

    evidence = final_manifest_service._file_evidence(projection)

    digest = f"sha256:{'a' * 64}" if locked else None
    assert evidence == (
        LocalFileEvidence(
            type="local",
            target="/workspace/ComfyUI/models/model.bin",
            verification="sha256" if locked else "unverified-local",
            intended_checksum=digest,
            observed_checksum=digest,
        ),
    )
    assert observations == [
        (
            Path("/workspace/ComfyUI"),
            Path("/workspace/ComfyUI/models/model.bin"),
            digest,
        )
    ]


def test_final_manifest_observes_build_hook_domain_and_retained_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"echo retained build hook\n"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    build_hooks = tmp_path / "build-hooks"
    hook_path = build_hooks / "hooks/pre.py"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_bytes(content)
    plan = build_plan(
        final_config(build_hooks_dir=build_hooks, with_hook=True),
        accepted_resolution(hook_digest=digest),
    )
    path = tmp_path / "build-plan.json"
    path.write_bytes(dump_build_plan_json(plan))
    projection = BuildPlanInputAdmission.from_path(
        path,
        expected_build_plan_digest=build_plan_digest(plan),
    ).final_manifest()
    observed: list[Path] = []

    def read(path: Path) -> bytes:
        observed.append(path)
        return content

    monkeypatch.setattr(final_manifest_service, "read_regular_absolute_file", read)

    evidence = final_manifest_service._hook_evidence(projection)

    assert tuple(item.domain for item in projection.materialized_hooks) == ("build",)
    assert tuple(item.domain for item in evidence) == ("build",)
    assert observed == [Path("/opt/cdh/build/hooks/hooks/pre.py")]


def test_final_projection_sorts_uv_tools_by_normalized_name() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["toolchain"]["tool_store"]["uv_tools"] = (
        {
            "name": "z-tool",
            "extras": (),
            "version": "2.0.0",
            "direct_reference": None,
            "environment": "uv-tool:z-tool",
        },
        {
            "name": "a-tool",
            "extras": (),
            "version": "1.0.0",
            "direct_reference": None,
            "environment": "uv-tool:a-tool",
        },
    )
    projection = BuildPlanInputAdmission(
        BuildPlan.model_validate(document)
    ).final_manifest()

    assert tuple(tool.name for tool in projection.toolchain.tool_store.uv_tools) == (
        "a-tool",
        "z-tool",
    )


# Manifest emission observes first, writes canonical bytes, and projects writer errors.
def test_manifest_service_publishes_only_after_successful_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    projection = BuildPlanInputAdmission(plan).final_manifest()
    expected = manifest_for_plan(plan)
    published: list[tuple[Path, bytes]] = []
    events: list[str | ContainerHelperEvent] = []

    def observe(*_args, **_kwargs):
        events.append("observe")
        return expected

    def publish(path: Path, content: bytes) -> None:
        events.append("write")
        published.append((path, content))

    monkeypatch.setattr(
        final_manifest_service,
        "_observe_final_manifest",
        observe,
    )
    monkeypatch.setattr(
        final_manifest_service,
        "write_final_manifest_file",
        publish,
    )

    assert (
        final_manifest_service.emit_final_manifest(
            projection,
            runtime=object(),
            event_sink=SimpleNamespace(emit=events.append),
        )
        == expected
    )
    assert published == [
        (final_manifest_service._MANIFEST_PATH, dump_final_manifest(expected))
    ]
    assert events == [
        ContainerHelperPhaseStarted(ContainerHelperPhase.FINAL_STATE_VERIFICATION),
        "observe",
        ContainerHelperPhaseCompleted(ContainerHelperPhase.FINAL_STATE_VERIFICATION),
        ContainerHelperPhaseStarted(ContainerHelperPhase.FINAL_MANIFEST_WRITE),
        "write",
        ContainerHelperPhaseCompleted(ContainerHelperPhase.FINAL_MANIFEST_WRITE),
        FinalManifestCompleted(),
    ]

    published.clear()
    events.clear()

    def fail_observation(*_args, **_kwargs):
        events.append("observe")
        raise FinalManifestError("observation failed")

    monkeypatch.setattr(
        final_manifest_service,
        "_observe_final_manifest",
        fail_observation,
    )
    with pytest.raises(FinalManifestError, match="observation failed"):
        final_manifest_service.emit_final_manifest(
            projection,
            runtime=object(),
            event_sink=SimpleNamespace(emit=events.append),
        )
    assert published == []
    assert events == [
        ContainerHelperPhaseStarted(ContainerHelperPhase.FINAL_STATE_VERIFICATION),
        "observe",
    ]


def test_manifest_service_projects_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    projection = BuildPlanInputAdmission(plan).final_manifest()
    expected = manifest_for_plan(plan)
    events: list[str | ContainerHelperEvent] = []
    monkeypatch.setattr(
        final_manifest_service,
        "_observe_final_manifest",
        lambda *_args, **_kwargs: expected,
    )

    def fail_write(*_args, **_kwargs) -> None:
        events.append("write")
        raise FinalManifestWriteError("final manifest target already exists")

    monkeypatch.setattr(final_manifest_service, "write_final_manifest_file", fail_write)

    with pytest.raises(FinalManifestError) as error:
        final_manifest_service.emit_final_manifest(
            projection,
            runtime=object(),
            event_sink=SimpleNamespace(emit=events.append),
        )
    assert str(error.value) == "final manifest target already exists"
    assert events == [
        ContainerHelperPhaseStarted(ContainerHelperPhase.FINAL_STATE_VERIFICATION),
        ContainerHelperPhaseCompleted(ContainerHelperPhase.FINAL_STATE_VERIFICATION),
        ContainerHelperPhaseStarted(ContainerHelperPhase.FINAL_MANIFEST_WRITE),
        "write",
    ]


# Final probes use authenticated inputs and reject untruthful success evidence.
def test_final_probe_runner_uses_the_exact_application_python_and_typed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    probe = BuildPlanInputAdmission(plan).final_manifest().final_probe
    runtime = SimpleNamespace(python=Path("/opt/venv/bin/python"))
    calls: list[tuple[tuple[Path | str, ...], Path, str]] = []

    def capture(argv, *, cwd, description):
        calls.append((argv, cwd, description))
        return json.dumps(
            {"checks": probe.checks, "result": "passed", "stage": "final-build"}
        )

    monkeypatch.setattr(final_manifest_service, "_capture", capture)

    evidence = final_manifest_service._run_final_core_probe(probe, runtime)

    assert evidence.checks == probe.checks
    argv, cwd, description = calls[0]
    assert argv[:3] == (
        Path("/opt/venv/bin/python"),
        "-I",
        final_manifest_service._FINAL_CORE_PROBE_PATH,
    )
    assert json.loads(argv[3]) == {
        "checks": list(probe.checks),
        "workspace": plan.application.paths.comfyui,
    }
    assert cwd == Path("/opt/cdh/build")
    assert description == "final core application probe"


def test_final_probe_runner_rejects_untruthful_success_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    probe = BuildPlanInputAdmission(plan).final_manifest().final_probe
    monkeypatch.setattr(
        final_manifest_service,
        "_capture",
        lambda *_args, **_kwargs: json.dumps(
            {
                "checks": probe.checks[:-1],
                "result": "passed",
                "stage": "final-build",
            }
        ),
    )

    with pytest.raises(FinalManifestError, match="do not match BuildPlan"):
        final_manifest_service._run_final_core_probe(
            probe,
            SimpleNamespace(python=Path("/opt/venv/bin/python")),
        )


# Final observation delegates to the existing local custom-node identity authority.
def test_final_observation_uses_live_custom_node_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    projection = BuildPlanInputAdmission(plan).final_manifest()
    expected = custom_node_inventory(plan.custom_nodes.nodes)
    runtime = SimpleNamespace(comfyui_path=Path("/workspace/ComfyUI"))
    calls = []
    monkeypatch.setattr(
        final_manifest_service,
        "observe_custom_node_state",
        lambda custom_nodes, **kwargs: calls.append((custom_nodes, kwargs)) or expected,
    )

    assert final_manifest_service._custom_node_evidence(projection, runtime) == expected
    assert calls == [(plan.custom_nodes, {"runtime": runtime})]


def test_final_observation_reports_custom_node_proof_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    projection = BuildPlanInputAdmission(plan).final_manifest()
    monkeypatch.setattr(
        final_manifest_service,
        "observe_custom_node_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            final_manifest_service.CustomNodeInstallError("identity drift")
        ),
    )

    with pytest.raises(FinalManifestError, match="final custom-node observation"):
        final_manifest_service._custom_node_evidence(
            projection,
            SimpleNamespace(comfyui_path=Path("/workspace/ComfyUI")),
        )
