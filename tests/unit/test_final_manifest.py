"""Final manifest schema, binding, omission, and renderer-order contracts."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tests.build_plan_support import accepted_resolution, build_plan, final_config

from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    build_plan_digest,
    dump_build_plan_json,
    manifest_binding,
)
from comfyui_docker_helper.config.custom_node_inventory import custom_node_inventory
from comfyui_docker_helper.config.final_manifest import (
    ApplicationEvidence,
    AptPackageEvidence,
    CdhToolEnvironmentEvidence,
    ComfyCliEvidence,
    ComfyUISourceEvidence,
    DigestEvidence,
    DisabledManagerEvidence,
    EnabledManagerEvidence,
    FinalBuildProbeEvidence,
    FinalManifest,
    ImageEvidence,
    InventoryDistribution,
    LifecycleEvidence,
    MaterializedInputsEvidence,
    PlatformEvidence,
    ProtectedRequirementEvidence,
    SetuptoolsEvidence,
    ToolchainEvidence,
    VersionEvidence,
    dump_final_manifest,
    final_build_check_ids,
    parse_final_manifest,
)
from comfyui_docker_helper.container import final_manifest as final_manifest_service
from comfyui_docker_helper.container.build_plan_input import BuildPlanInputAdmission
from comfyui_docker_helper.container.final_manifest import FinalManifestError
from comfyui_docker_helper.container.final_manifest_writer import (
    FinalManifestWriteError,
)
from comfyui_docker_helper.rendering.final_renderer import (
    render_build_plan_dockerfile,
)


def _inventory(
    values: tuple[tuple[str, str], ...],
) -> tuple[InventoryDistribution, ...]:
    return tuple(
        InventoryDistribution(name=name, version=version)
        for name, version in sorted(values)
    )


def _manifest(plan: BuildPlan) -> FinalManifest:
    cdh = plan.toolchain.tool_store.cdh
    cdh_inventory = ((cdh.name, cdh.version),)
    direct = tuple(
        sorted(
            (package.name, package.version)
            for package in (
                *plan.application.pytorch.packages,
                *(
                    ()
                    if plan.application.python_extras is None
                    else plan.application.python_extras.packages
                ),
            )
        )
    )
    application_inventory = (
        *direct,
        ("pip", plan.application.pip_version),
        ("setuptools", "81.0.0"),
        *(
            ()
            if plan.application.comfyui.manager is None
            else (("comfyui-manager", "4.0.5"),)
        ),
    )
    cli_plan = plan.toolchain.tool_store.comfy_cli
    comfy_cli = None
    if cli_plan is not None:
        comfy_cli = ComfyCliEvidence(
            name="comfy-cli",
            environment="uv-tool:comfy-cli",
            direct=VersionEvidence(
                intended=cli_plan.version,
                observed=cli_plan.version,
            ),
            inventory=_inventory((("comfy-cli", cli_plan.version),)),
            dependency_check="passed",
            entrypoints=cli_plan.executables,
        )
    manager = plan.application.comfyui.manager
    manager_evidence = (
        DisabledManagerEvidence(enabled=False, observed="absent")
        if manager is None
        else EnabledManagerEvidence(
            enabled=True,
            distribution="comfyui-manager",
            version=VersionEvidence(intended="4.0.5", observed="4.0.5"),
            import_name="comfyui_manager",
            executable="/opt/venv/bin/cm-cli",
            registry_control="direct-cm-cli",
        )
    )
    expected_uv = plan.toolchain.uv_image.resolved_version
    assert expected_uv is not None
    return FinalManifest(
        schema_version=1,
        binding=manifest_binding(plan),
        platform=PlatformEvidence(
            platform=plan.toolchain.platform,
            backend="cuda",
            backend_version=plan.toolchain.cuda_version,
            channel=plan.toolchain.pytorch_channel,
            cuda_image=ImageEvidence(
                role=plan.toolchain.cuda_image.role,
                repository=plan.toolchain.cuda_image.repository,
                tag=plan.toolchain.cuda_image.tag,
                descriptor_digest=plan.toolchain.cuda_image.descriptor_digest,
                descriptor_kind=plan.toolchain.cuda_image.descriptor_kind,
                platform=plan.toolchain.cuda_image.platform,
            ),
            uv_image=ImageEvidence(
                role=plan.toolchain.uv_image.role,
                repository=plan.toolchain.uv_image.repository,
                tag=plan.toolchain.uv_image.tag,
                descriptor_digest=plan.toolchain.uv_image.descriptor_digest,
                descriptor_kind=plan.toolchain.uv_image.descriptor_kind,
                platform=plan.toolchain.uv_image.platform,
            ),
        ),
        toolchain=ToolchainEvidence(
            container_uv=VersionEvidence(intended=expected_uv, observed=expected_uv),
            container_uvx=VersionEvidence(
                intended=expected_uv,
                observed=expected_uv,
            ),
            python=VersionEvidence(
                intended=plan.toolchain.python.version,
                observed=plan.toolchain.python.version,
            ),
            python_provider="uv-managed",
            python_catalog_descriptor_digest=(
                plan.toolchain.python.catalog_descriptor_digest
            ),
            cdh=CdhToolEnvironmentEvidence(
                name="comfyui-docker-helper",
                environment="uv-tool:comfyui-docker-helper",
                direct=VersionEvidence(
                    intended=cdh.version,
                    observed=cdh.version,
                ),
                inventory=_inventory(cdh_inventory),
                dependency_check="passed",
                wheel_digest=cdh.wheel_digest,
            ),
            comfy_cli=comfy_cli,
            uv_tools=(),
        ),
        application=ApplicationEvidence(
            pip=VersionEvidence(
                intended=plan.application.pip_version,
                observed=plan.application.pip_version,
            ),
            direct_packages=tuple(
                (
                    name,
                    VersionEvidence(intended=version, observed=version),
                )
                for name, version in direct
            ),
            setuptools=SetuptoolsEvidence(
                compatibility=plan.application.pytorch.setuptools_specifier or "<82",
                observed="81.0.0",
            ),
            inventory=_inventory(application_inventory),
            dependency_check="passed",
            source=ComfyUISourceEvidence(
                repository=plan.application.comfyui.repository,
                intended_commit=plan.application.comfyui.commit,
                observed_commit=plan.application.comfyui.commit,
                floor_commit=plan.application.comfyui.floor_commit,
                formal_release=plan.application.comfyui.formal_release,
                requirements_intended_digest=(
                    plan.application.comfyui.requirements.digest
                ),
                requirements_observed_digest=(
                    plan.application.comfyui.requirements.digest
                ),
                protected=tuple(
                    ProtectedRequirementEvidence(
                        package=item.package,
                        extras=item.extras,
                        selector=item.selector,
                    )
                    for item in plan.application.comfyui.requirements.protected
                ),
            ),
            manager=manager_evidence,
            final_probe=FinalBuildProbeEvidence(
                stage="final-build",
                result="passed",
                checks=final_build_check_ids(
                    tuple(name for name, _version in direct),
                    manager_enabled=manager is not None,
                ),
            ),
        ),
        custom_nodes=custom_node_inventory(plan.custom_nodes.nodes),
        files=(),
        hooks=(),
        apt=tuple(
            AptPackageEvidence(
                name=name,
                observed_version="1.0-1ubuntu1",
                resolution="external-moving",
            )
            for name in plan.application.os_packages
        ),
        materialized_inputs=MaterializedInputsEvidence(
            comfyui_requirements=DigestEvidence(
                intended=plan.application.comfyui.requirements.digest,
                observed=plan.application.comfyui.requirements.digest,
            ),
        ),
        lifecycle=LifecycleEvidence(
            tini_executable="/usr/bin/tini",
            tini_observed_version="0.19.0-1",
            stop_signal="SIGTERM",
            entrypoint=(
                "/usr/bin/tini",
                "--",
                "/opt/uv/bin/cdh",
                "container",
                "runtime",
                "serve",
            ),
            shutdown_timeout=plan.runtime.shutdown_timeout,
        ),
    )


# Canonical bytes retain exact local versions without promoting observations.
def test_manifest_round_trip_is_canonical_observational_and_strict() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    manifest = _manifest(plan)

    content = dump_final_manifest(manifest)

    assert content.endswith(b"\n") and content.count(b"\n") == 1
    assert parse_final_manifest(content) == manifest
    assert dump_final_manifest(parse_final_manifest(content)) == content
    assert b'"observed":"2.11.0+cu130"' in content
    assert b"timestamp" not in content
    assert b"/home/" not in content

    document = json.loads(content)
    document["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FinalManifest.model_validate(document)


# Disabled comfy-cli omits the capability instead of inventing an absent version.
def test_manifest_omits_disabled_comfy_cli_capability() -> None:
    plan = build_plan(
        final_config(install_cli=False),
        accepted_resolution(install_cli=False),
    )

    content = dump_final_manifest(_manifest(plan))

    assert b'"comfy_cli"' not in content
    assert json.loads(content)["application"]["manager"]["enabled"] is True


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


# Schema validators reject identity drift and a false setuptools compatibility claim.
def test_manifest_rejects_intended_observed_identity_mismatch() -> None:
    with pytest.raises(ValidationError, match="intended and observed versions"):
        VersionEvidence(intended="2.12.1+cu130", observed="2.12.1+cpu")

    with pytest.raises(ValidationError, match="does not satisfy compatibility"):
        SetuptoolsEvidence(compatibility="<82", observed="82.0.0")


@pytest.mark.parametrize("version", ["1.0rc1", "1.0.dev1", "1.0+cuda"])
def test_factual_inventory_accepts_canonical_complete_pep440(version: str) -> None:
    evidence = InventoryDistribution(name="observed-package", version=version)

    assert evidence.version == version


@pytest.mark.parametrize(
    ("contradiction", "message"),
    [
        ("setuptools", "does not match setuptools"),
        ("manager-version", "does not match Manager"),
        ("manager-disabled", "disabled Manager must be absent"),
    ],
)
def test_application_evidence_rejects_inventory_contradictions(
    contradiction: str,
    message: str,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = _manifest(plan).model_dump(mode="python")
    application = document["application"]
    if contradiction == "setuptools":
        application["setuptools"]["observed"] = "80.0.0"
    elif contradiction == "manager-version":
        application["manager"]["version"] = {
            "intended": "4.0.4",
            "observed": "4.0.4",
        }
    else:
        application["manager"] = {"enabled": False, "observed": "absent"}

    with pytest.raises(ValidationError, match=message):
        FinalManifest.model_validate(document)


def test_application_evidence_requires_the_exact_conditional_probe_checks() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = _manifest(plan).model_dump(mode="python")
    document["application"]["final_probe"]["checks"] = (
        "torch-import",
        "torch-cpu-tensor",
        "comfyui-folder-paths-import",
        "comfyui-comfy-import",
    )

    with pytest.raises(ValidationError, match="do not match the application intent"):
        FinalManifest.model_validate(document)


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
            "environment": "uv-tool:z-tool",
        },
        {
            "name": "a-tool",
            "extras": (),
            "version": "1.0.0",
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
    expected = _manifest(plan)
    published: list[tuple[Path, bytes]] = []
    monkeypatch.setattr(
        final_manifest_service,
        "_observe_final_manifest",
        lambda *_args, **_kwargs: expected,
    )
    monkeypatch.setattr(
        final_manifest_service,
        "write_final_manifest_file",
        lambda path, content: published.append((path, content)),
    )

    assert (
        final_manifest_service.emit_final_manifest(
            projection,
            runtime=object(),
        )
        == expected
    )
    assert published == [
        (final_manifest_service._MANIFEST_PATH, dump_final_manifest(expected))
    ]

    published.clear()
    monkeypatch.setattr(
        final_manifest_service,
        "_observe_final_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            FinalManifestError("observation failed")
        ),
    )
    with pytest.raises(FinalManifestError, match="observation failed"):
        final_manifest_service.emit_final_manifest(projection, runtime=object())
    assert published == []


def test_manifest_service_projects_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    projection = BuildPlanInputAdmission(plan).final_manifest()
    expected = _manifest(plan)
    monkeypatch.setattr(
        final_manifest_service,
        "_observe_final_manifest",
        lambda *_args, **_kwargs: expected,
    )
    monkeypatch.setattr(
        final_manifest_service,
        "write_final_manifest_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            FinalManifestWriteError("final manifest target already exists")
        ),
    )

    with pytest.raises(FinalManifestError) as error:
        final_manifest_service.emit_final_manifest(projection, runtime=object())
    assert str(error.value) == "final manifest target already exists"


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


# The manifest command is the last filesystem mutation in every rendered build.
@pytest.mark.parametrize("with_files", [False, True])
def test_renderer_places_manifest_emission_after_every_build_mutation(
    with_files: bool,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    if not with_files:
        document = plan.model_dump(mode="python")
        document["files"]["files"] = ()
        plan = BuildPlan.model_validate(document)

    lines = render_build_plan_dockerfile(plan).splitlines()

    manifest_index = next(
        index for index, line in enumerate(lines) if "emit-final-manifest" in line
    )
    assert sum("emit-final-manifest" in line for line in lines) == 1
    assert manifest_index > next(
        index for index, line in enumerate(lines) if "install-custom-nodes" in line
    )
    if with_files:
        assert manifest_index > next(
            index for index, line in enumerate(lines) if "download-files" in line
        )
    assert lines[manifest_index + 1 :] == [
        "STOPSIGNAL SIGTERM",
        'ENTRYPOINT ["/usr/bin/tini", "--", "/opt/uv/bin/cdh", '
        '"container", "runtime", "serve"]',
    ]


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
