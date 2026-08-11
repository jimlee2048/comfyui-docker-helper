"""Shared constructors for final-manifest behavior tests."""

from comfyui_docker_helper.config.build_plan import BuildPlan, manifest_binding
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
    final_build_check_ids,
)


def _inventory(
    values: tuple[tuple[str, str], ...],
) -> tuple[InventoryDistribution, ...]:
    return tuple(
        InventoryDistribution(name=name, version=version)
        for name, version in sorted(values)
    )


def manifest_for_plan(plan: BuildPlan) -> FinalManifest:
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
