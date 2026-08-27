"""Final build observation and canonical manifest emission."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from comfyui_docker_helper.cli_output.events import EventSink
from comfyui_docker_helper.config.evidence.manifest import (
    ApplicationEvidence,
    AptPackageEvidence,
    CdhToolEnvironmentEvidence,
    ComfyCliEvidence,
    ComfyUISourceEvidence,
    DigestEvidence,
    DisabledManagerEvidence,
    DistributionVersionEvidence,
    EnabledManagerEvidence,
    FileEvidence,
    FinalBuildProbeEvidence,
    FinalManifest,
    HookEvidence,
    HttpFileEvidence,
    ImageEvidence,
    InventoryDistribution,
    LifecycleEvidence,
    LocalFileEvidence,
    MaterializedInputsEvidence,
    PlatformEvidence,
    ProtectedRequirementEvidence,
    SetuptoolsEvidence,
    ToolchainEvidence,
    ToolEnvironmentEvidence,
    VersionEvidence,
    dump_final_manifest,
)
from comfyui_docker_helper.config.planning.build_plan import ProtectedRequirementPlan
from comfyui_docker_helper.config.planning.canonical_lock import (
    DirectPythonRequestMember,
)
from comfyui_docker_helper.container.build.admission import (
    FinalCoreProbeInput,
    FinalManifestInput,
)
from comfyui_docker_helper.container.build.comfyui import (
    capture_application_requirements,
    capture_manager_authority,
    observe_application_state,
    observe_manager_absence,
)
from comfyui_docker_helper.container.build.custom_nodes.contracts import (
    CustomNodeInstallError,
)
from comfyui_docker_helper.container.build.custom_nodes.orchestrator import (
    observe_custom_node_state,
)
from comfyui_docker_helper.container.build.events import (
    ContainerHelperEvent,
    ContainerHelperPhase,
    ContainerHelperPhaseCompleted,
    ContainerHelperPhaseStarted,
    FinalManifestCompleted,
)
from comfyui_docker_helper.container.build.manifest import writer
from comfyui_docker_helper.container.process.runners import ContainerRuntime, run_argv
from comfyui_docker_helper.container.transfer.core import verify_required_final
from comfyui_docker_helper.errors import ApplicationError
from comfyui_docker_helper.filesystem.admission import read_regular_absolute_file

_BUILD_DIRECTORY = Path("/opt/cdh/build")
_MANIFEST_PATH = _BUILD_DIRECTORY / "manifest.json"
_UV_PATH = Path("/usr/local/bin/uv")
_TINI_PATH = Path("/usr/bin/tini")
_GIT_PATH = Path("/usr/bin/git")
_DPKG_QUERY_PATH = Path("/usr/bin/dpkg-query")
_FINAL_CORE_PROBE_PATH = Path(__file__).parents[3] / "resources" / "final-core-probe.py"
_VERSION_PATTERN = re.compile(r"^(?:uv|uvx) (?P<version>\S+)(?: \([^\n]+\))?$")
_OBSERVATION_ENVIRONMENT = {
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}
_COMFY_CLI_COMMANDS = ("comfy", "comfy-cli", "comfycli")


@dataclass(frozen=True, slots=True)
class _InterpreterIdentity:
    """Observed identity for one isolated Python interpreter."""

    prefix: Path
    base_executable: Path
    python_version: str | None = None


class FinalManifestError(ApplicationError):
    """Final image evidence could not be proved or emitted."""


def emit_final_manifest(
    projection: FinalManifestInput,
    *,
    runtime: ContainerRuntime,
    event_sink: EventSink[ContainerHelperEvent] | None = None,
) -> FinalManifest:
    """Publish the canonical manifest only after every final observation passes."""
    _emit_helper_event(
        event_sink,
        ContainerHelperPhaseStarted(ContainerHelperPhase.FINAL_STATE_VERIFICATION),
    )
    manifest = _observe_final_manifest(projection, runtime=runtime)
    _emit_helper_event(
        event_sink,
        ContainerHelperPhaseCompleted(ContainerHelperPhase.FINAL_STATE_VERIFICATION),
    )
    _emit_helper_event(
        event_sink,
        ContainerHelperPhaseStarted(ContainerHelperPhase.FINAL_MANIFEST_WRITE),
    )
    try:
        writer.write_final_manifest_file(_MANIFEST_PATH, dump_final_manifest(manifest))
    except writer.FinalManifestWriteError as error:
        raise FinalManifestError(str(error)) from error
    _emit_helper_event(
        event_sink,
        ContainerHelperPhaseCompleted(ContainerHelperPhase.FINAL_MANIFEST_WRITE),
    )
    _emit_helper_event(event_sink, FinalManifestCompleted())
    return manifest


def _emit_helper_event(
    event_sink: EventSink[ContainerHelperEvent] | None,
    event: ContainerHelperEvent,
) -> None:
    if event_sink is not None:
        event_sink.emit(event)


def _observe_final_manifest(
    projection: FinalManifestInput,
    *,
    runtime: ContainerRuntime,
) -> FinalManifest:
    """Re-prove existing final state without publishing partial evidence."""
    application_authority = capture_application_requirements(
        projection.application, runtime
    )
    observe_application_state(
        projection.application,
        runtime,
        application_authority,
    )

    application_inventory = _environment_inventory(runtime.python)
    manager = _manager_evidence(projection, runtime, application_inventory)
    direct_packages = _direct_application_packages(projection, application_inventory)

    cdh_evidence = _cdh_tool_evidence(projection)

    comfy_cli = _comfy_cli_evidence(projection)
    managed_interpreter = _managed_python_interpreter(projection)
    uv_tools = tuple(
        _tool_evidence(
            tool.name,
            tool.version,
            tool.environment,
            Path(projection.toolchain.tool_store.tool_dir) / tool.name / "bin/python",
            managed_interpreter=managed_interpreter,
        )
        for tool in projection.toolchain.tool_store.uv_tools
    )

    files = _file_evidence(projection)
    custom_inventory = _custom_node_evidence(projection, runtime)
    hooks = _hook_evidence(projection)
    apt = tuple(
        AptPackageEvidence(
            name=name,
            observed_version=_apt_version(name),
            resolution="external-moving",
        )
        for name in projection.application.os_packages
    )
    tini_version = next(
        (item.observed_version for item in apt if item.name == "tini"),
        None,
    )
    if tini_version is None:
        raise FinalManifestError("Tini is missing from the BuildPlan OS packages")
    _verify_tini()

    requirements_digest = application_authority.digest

    observed_commit = _capture(
        (_GIT_PATH, "-C", runtime.comfyui_path, "rev-parse", "HEAD"),
        cwd=runtime.comfyui_path,
        description="ComfyUI commit observation",
    ).strip()
    protected = _protected_requirement_evidence(
        application_authority.protected,
        projection.application.comfyui.requirements.protected,
    )

    observed = dict(application_inventory)
    setuptools_specifier = projection.application.pytorch.setuptools_specifier
    setuptools = None
    if setuptools_specifier is not None:
        actual = observed.get("setuptools")
        if actual is None:
            raise FinalManifestError("application setuptools observation is missing")
        setuptools = SetuptoolsEvidence(
            compatibility=setuptools_specifier,
            observed=actual,
        )

    container_uv_version = _binary_version((_UV_PATH, "--version"), "uv")
    container_uvx_version = _binary_version(
        (Path("/usr/local/bin/uvx"), "--version"), "uvx"
    )
    application_python_version = _observe_application_interpreter(projection, runtime)
    final_probe = _run_final_core_probe(projection.final_probe, runtime)

    return FinalManifest(
        schema_version=1,
        binding=projection.binding,
        platform=PlatformEvidence(
            platform=projection.toolchain.platform,
            backend="cuda",
            backend_version=projection.toolchain.cuda_version,
            channel=projection.toolchain.pytorch_channel,
            cuda_image=_image_evidence(projection.toolchain.cuda_image),
            uv_image=_image_evidence(projection.toolchain.uv_image),
        ),
        toolchain=ToolchainEvidence(
            container_uv=VersionEvidence(
                intended=_required_uv_version(projection),
                observed=container_uv_version,
            ),
            container_uvx=VersionEvidence(
                intended=_required_uv_version(projection),
                observed=container_uvx_version,
            ),
            python=VersionEvidence(
                intended=projection.toolchain.python.version,
                observed=application_python_version,
            ),
            python_provider="uv-managed",
            python_catalog_descriptor_digest=(
                projection.toolchain.python.catalog_descriptor_digest
            ),
            cdh=cdh_evidence,
            comfy_cli=comfy_cli,
            uv_tools=uv_tools,
        ),
        application=ApplicationEvidence(
            pip=VersionEvidence(
                intended=projection.application.pip_version,
                observed=observed["pip"],
            ),
            direct_packages=direct_packages,
            setuptools=setuptools,
            inventory=_inventory_models(application_inventory),
            dependency_check="passed",
            source=ComfyUISourceEvidence(
                repository=projection.application.comfyui.repository,
                intended_commit=projection.application.comfyui.commit,
                observed_commit=observed_commit,
                floor_commit=projection.application.comfyui.floor_commit,
                formal_release=projection.application.comfyui.formal_release,
                requirements_intended_digest=(
                    projection.application.comfyui.requirements.digest
                ),
                requirements_observed_digest=requirements_digest,
                protected=protected,
            ),
            manager=manager,
            final_probe=final_probe,
        ),
        custom_nodes=custom_inventory,
        files=files,
        hooks=hooks,
        apt=apt,
        materialized_inputs=MaterializedInputsEvidence(
            comfyui_requirements=DigestEvidence(
                intended=projection.application.comfyui.requirements.digest,
                observed=requirements_digest,
            ),
        ),
        lifecycle=LifecycleEvidence(
            tini_executable="/usr/bin/tini",
            tini_observed_version=tini_version,
            stop_signal="SIGTERM",
            entrypoint=(
                "/usr/bin/tini",
                "--",
                "/opt/uv/bin/cdh",
                "container",
                "runtime",
                "serve",
            ),
            shutdown_timeout=projection.shutdown_timeout,
        ),
    )


def _protected_requirement_evidence(
    observed: tuple[DirectPythonRequestMember, ...],
    expected: tuple[ProtectedRequirementPlan, ...],
) -> tuple[ProtectedRequirementEvidence, ...]:
    evidence = tuple(
        ProtectedRequirementEvidence(
            package=item.package,
            extras=item.extras,
            selector=item.specifier,
        )
        for item in observed
    )
    expected_identity = tuple(
        (item.package, item.extras, item.selector) for item in expected
    )
    if (
        tuple((item.package, item.extras, item.selector) for item in evidence)
        != expected_identity
    ):
        raise FinalManifestError(
            "ComfyUI protected projection does not match BuildPlan"
        )
    return evidence


def _run_final_core_probe(
    probe: FinalCoreProbeInput,
    runtime: ContainerRuntime,
) -> FinalBuildProbeEvidence:
    try:
        metadata = _FINAL_CORE_PROBE_PATH.lstat()
        resolved = _FINAL_CORE_PROBE_PATH.resolve(strict=True)
    except OSError as error:
        raise FinalManifestError("final core probe resource is unavailable") from error
    if (
        _FINAL_CORE_PROBE_PATH.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or resolved != _FINAL_CORE_PROBE_PATH
    ):
        raise FinalManifestError("final core probe resource identity is invalid")
    payload = json.dumps(
        {"checks": probe.checks, "workspace": probe.workspace},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    output = _capture(
        (runtime.python, "-I", _FINAL_CORE_PROBE_PATH, payload),
        cwd=_BUILD_DIRECTORY,
        description="final core application probe",
    )
    try:
        evidence = FinalBuildProbeEvidence.model_validate_json(output)
    except ValidationError as error:
        raise FinalManifestError(
            "final core probe returned invalid evidence"
        ) from error
    if evidence.checks != probe.checks:
        raise FinalManifestError("final core probe checks do not match BuildPlan")
    return evidence


def _image_evidence(image) -> ImageEvidence:
    return ImageEvidence(
        role=image.role,
        repository=image.repository,
        tag=image.tag,
        descriptor_digest=image.descriptor_digest,
        descriptor_kind=image.descriptor_kind,
        platform=image.platform,
    )


def _direct_application_packages(
    projection: FinalManifestInput,
    inventory: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, DistributionVersionEvidence], ...]:
    observed = dict(inventory)
    expected = {
        package.name: package.version
        for package in projection.application.pytorch.packages
    }
    if projection.application.python_extras is not None:
        expected.update(
            {
                package.name: package.version
                for package in projection.application.python_extras.packages
            }
        )
    try:
        return tuple(
            (
                name,
                DistributionVersionEvidence(intended=version, observed=observed[name]),
            )
            for name, version in sorted(expected.items())
        )
    except KeyError as error:
        raise FinalManifestError(
            f"application direct package observation is missing: {error.args[0]}"
        ) from error


def _manager_evidence(
    projection: FinalManifestInput,
    runtime: ContainerRuntime,
    inventory: tuple[tuple[str, str], ...],
) -> EnabledManagerEvidence | DisabledManagerEvidence:
    manager = projection.application.comfyui.manager
    if manager is None:
        observe_manager_absence(projection.application, runtime)
        return DisabledManagerEvidence(enabled=False, observed="absent")
    authority = capture_manager_authority(projection.application, runtime)
    observed = dict(inventory).get(manager.distribution)
    if observed is None:
        raise FinalManifestError("Manager distribution observation is missing")
    return EnabledManagerEvidence(
        enabled=True,
        distribution="comfyui-manager",
        version=VersionEvidence(
            intended=authority.manager_version,
            observed=observed,
        ),
        import_name="comfyui_manager",
        executable="/opt/venv/bin/cm-cli",
        registry_control="direct-cm-cli",
    )


def _managed_python_interpreter(projection: FinalManifestInput) -> Path:
    managed_python = projection.toolchain.python
    return (
        Path("/opt/python")
        / managed_python.catalog_key
        / "bin"
        / f"python{'.'.join(managed_python.version.split('.')[:2])}"
    )


def _observe_application_interpreter(
    projection: FinalManifestInput,
    runtime: ContainerRuntime,
) -> str:
    identity = _observe_interpreter_identity(
        runtime.python,
        "application interpreter identity observation",
        include_version=True,
    )
    if identity.prefix != Path(projection.application.paths.venv):
        raise FinalManifestError("application environment does not match BuildPlan")
    if not _same_resolved_path(
        identity.base_executable,
        _managed_python_interpreter(projection),
    ):
        raise FinalManifestError(
            "application base interpreter does not match BuildPlan"
        )
    if identity.python_version is None:  # pragma: no cover - parser owns this shape.
        raise FinalManifestError(
            "application interpreter identity omitted Python version"
        )
    if identity.python_version != projection.toolchain.python.version:
        raise FinalManifestError("application Python version does not match BuildPlan")
    return identity.python_version


def _cdh_tool_evidence(projection: FinalManifestInput) -> CdhToolEnvironmentEvidence:
    cdh = projection.toolchain.tool_store.cdh
    python = Path(sys.executable)
    inventory = _environment_inventory(python)
    identity = _observe_interpreter_identity(
        python,
        "cdh interpreter identity observation",
    )
    if identity.prefix != Path(cdh.environment):
        raise FinalManifestError("cdh environment does not match BuildPlan")
    if not _same_resolved_path(
        identity.base_executable,
        _managed_python_interpreter(projection),
    ):
        raise FinalManifestError("cdh base interpreter does not match BuildPlan")
    _verify_owned_entrypoint(
        Path(cdh.executable),
        Path(cdh.environment) / "bin" / Path(cdh.executable).name,
        "cdh",
    )
    _dependency_check(python, "cdh dependency verification")
    observed = dict(inventory).get(cdh.name)
    if observed != cdh.version:
        raise FinalManifestError("cdh direct identity does not match BuildPlan")
    return CdhToolEnvironmentEvidence(
        name=cdh.name,
        environment="uv-tool:comfyui-docker-helper",
        direct=VersionEvidence(
            intended=cdh.version,
            observed=observed,
        ),
        wheel_digest=cdh.wheel_digest,
        inventory=_inventory_models(inventory),
        dependency_check="passed",
    )


def _comfy_cli_evidence(
    projection: FinalManifestInput,
) -> ComfyCliEvidence | None:
    tool = projection.toolchain.tool_store.comfy_cli
    if tool is None:
        _verify_disabled_comfy_cli(projection)
        return None
    python = Path(projection.toolchain.tool_store.tool_dir) / tool.name / "bin/python"
    managed_interpreter = _managed_python_interpreter(projection)
    identity = _observe_interpreter_identity(
        python,
        "comfy-cli interpreter identity observation",
    )
    if identity.prefix != python.parent.parent:
        raise FinalManifestError("comfy-cli environment does not match BuildPlan")
    if not _same_resolved_path(identity.base_executable, managed_interpreter):
        raise FinalManifestError("comfy-cli base interpreter does not match BuildPlan")
    inventory = _environment_inventory(python)
    _dependency_check(python, "comfy-cli dependency verification")
    for command in tool.executables:
        _verify_owned_entrypoint(
            Path(projection.toolchain.tool_store.bin_dir) / command,
            Path(projection.toolchain.tool_store.tool_dir)
            / tool.name
            / "bin"
            / command,
            "comfy-cli",
            command=command,
        )
    observed = dict(inventory).get("comfy-cli")
    if observed is None:
        raise FinalManifestError("comfy-cli environment is missing comfy-cli")
    return ComfyCliEvidence(
        name="comfy-cli",
        environment="uv-tool:comfy-cli",
        direct=VersionEvidence(
            intended=tool.version,
            observed=observed,
        ),
        inventory=_inventory_models(inventory),
        dependency_check="passed",
        entrypoints=tool.executables,
    )


def _tool_evidence(
    name: str,
    version: str,
    environment: str,
    python: Path,
    *,
    managed_interpreter: Path,
) -> ToolEnvironmentEvidence:
    identity = _observe_interpreter_identity(
        python,
        f"{name} interpreter identity observation",
    )
    if identity.prefix != python.parent.parent:
        raise FinalManifestError(f"{name} environment does not match BuildPlan")
    if not _same_resolved_path(identity.base_executable, managed_interpreter):
        raise FinalManifestError(f"{name} base interpreter does not match BuildPlan")
    inventory = _environment_inventory(python)
    _dependency_check(python, f"{name} dependency verification")
    observed = dict(inventory).get(name)
    if observed is None:
        raise FinalManifestError(f"uv tool observation is missing: {name}")
    return ToolEnvironmentEvidence(
        name=name,
        environment=environment,
        direct=DistributionVersionEvidence(intended=version, observed=observed),
        inventory=_inventory_models(inventory),
        dependency_check="passed",
    )


def _verify_disabled_comfy_cli(projection: FinalManifestInput) -> None:
    for command in _COMFY_CLI_COMMANDS:
        path = Path(projection.toolchain.tool_store.bin_dir) / command
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise FinalManifestError(
                f"comfy-cli disabled command could not be checked: {command}"
            ) from error
        raise FinalManifestError(f"comfy-cli disabled command is present: {command}")


def _verify_owned_entrypoint(
    path: Path,
    expected: Path,
    owner: str,
    *,
    command: str | None = None,
) -> None:
    suffix = f": {command}" if command is not None else ""
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise FinalManifestError(
            f"{owner} entrypoint is unavailable{suffix}"
        ) from error
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or resolved != expected
    ):
        raise FinalManifestError(f"{owner} entrypoint ownership is invalid{suffix}")


def _same_resolved_path(actual: Path, expected: Path) -> bool:
    try:
        return actual.resolve(strict=True) == expected.resolve(strict=True)
    except OSError:
        return False


def _observe_interpreter_identity(
    python: Path,
    description: str,
    *,
    include_version: bool = False,
) -> _InterpreterIdentity:
    script = (
        "import json,pathlib,sys;"
        + ("import platform;" if include_version else "")
        + "print(json.dumps({"
        + "'base_executable':str("
        + "pathlib.Path(sys._base_executable).resolve(strict=True)),"
        + "'prefix':sys.prefix"
        + (",'python_version':platform.python_version()" if include_version else "")
        + "},sort_keys=True,separators=(',',':')))"
    )
    output = _capture(
        (python, "-I", "-c", script),
        cwd=_BUILD_DIRECTORY,
        description=description,
    )
    expected_keys = {"base_executable", "prefix"}
    if include_version:
        expected_keys.add("python_version")
    try:
        raw = json.loads(output)
    except (json.JSONDecodeError, TypeError) as error:
        raise FinalManifestError(
            f"{description} returned an invalid identity"
        ) from error
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise FinalManifestError(f"{description} returned an invalid identity")
    base_executable = raw["base_executable"]
    prefix = raw["prefix"]
    python_version = raw.get("python_version")
    if (
        not isinstance(base_executable, str)
        or not isinstance(prefix, str)
        or (include_version and not isinstance(python_version, str))
    ):
        raise FinalManifestError(f"{description} returned an invalid identity")
    return _InterpreterIdentity(
        prefix=Path(prefix),
        base_executable=Path(base_executable),
        python_version=python_version,
    )


def _custom_node_evidence(
    projection: FinalManifestInput,
    runtime: ContainerRuntime,
):
    try:
        return observe_custom_node_state(projection.custom_nodes, runtime=runtime)
    except CustomNodeInstallError as error:
        raise FinalManifestError("final custom-node observation failed") from error


def _file_evidence(projection: FinalManifestInput) -> tuple[FileEvidence, ...]:
    result: list[FileEvidence] = []
    root = Path(projection.application.paths.comfyui)
    for item in projection.files:
        target = Path(item.target)
        expected_checksum = (
            item.checksum
            if item.type == "http"
            else item.digest
            if item.verification == "sha256"
            else None
        )
        verify_required_final(
            root=root,
            target=target,
            expected_checksum=expected_checksum,
        )
        if item.type == "local":
            if item.verification == "sha256":
                result.append(
                    LocalFileEvidence(
                        type="local",
                        target=item.target,
                        verification="sha256",
                        intended_checksum=item.digest,
                        observed_checksum=item.digest,
                    )
                )
            else:
                result.append(
                    LocalFileEvidence(
                        type="local",
                        target=item.target,
                        verification="unverified-local",
                    )
                )
        elif item.checksum is None:
            result.append(
                HttpFileEvidence(
                    type="http",
                    url=item.url,
                    target=item.target,
                    verification="unverified-moving",
                )
            )
        else:
            result.append(
                HttpFileEvidence(
                    type="http",
                    url=item.url,
                    target=item.target,
                    verification="sha256",
                    intended_checksum=item.checksum,
                    observed_checksum=item.checksum,
                )
            )
    return tuple(result)


def _hook_evidence(projection: FinalManifestInput) -> tuple[HookEvidence, ...]:
    result: list[HookEvidence] = []
    roots = {
        "build": Path("/opt/cdh/build/hooks"),
        "runtime": Path("/opt/cdh/runtime/hooks"),
    }
    for hook in projection.materialized_hooks:
        observed = _sha256(
            read_regular_absolute_file(roots[hook.domain] / hook.relative_path)
        )
        result.append(
            HookEvidence(
                domain=hook.domain,
                relative_path=hook.relative_path,
                intended_digest=hook.digest,
                observed_digest=observed,
                effects="trusted-opaque",
            )
        )
    return tuple(result)


def _environment_inventory(python: Path) -> tuple[tuple[str, str], ...]:
    script = (
        "import importlib.metadata as m,json,re;"
        "n=lambda v:re.sub(r'[-_.]+','-',v).lower();"
        "print(json.dumps(sorted((n(d.metadata['Name']),d.version) "
        "for d in m.distributions()),separators=(',',':')))"
    )
    output = _capture(
        (python, "-I", "-c", script),
        cwd=_BUILD_DIRECTORY,
        description="environment inventory observation",
    )
    try:
        raw = json.loads(output)
        items = tuple(
            (
                canonicalize_name(name, validate=True),
                str(Version(version)),
            )
            for name, version in raw
        )
    except (InvalidName, InvalidVersion, TypeError, ValueError) as error:
        raise FinalManifestError("environment inventory is invalid") from error
    if items != tuple(sorted(items)) or len(items) != len({name for name, _ in items}):
        raise FinalManifestError("environment inventory is not sorted and unique")
    return items


def _inventory_models(
    inventory: tuple[tuple[str, str], ...],
) -> tuple[InventoryDistribution, ...]:
    return tuple(
        InventoryDistribution(name=name, version=version) for name, version in inventory
    )


def _dependency_check(python: Path, description: str) -> None:
    run_argv(
        (
            _UV_PATH,
            "--no-config",
            "pip",
            "check",
            "--python",
            python,
            "--no-python-downloads",
        ),
        cwd=_BUILD_DIRECTORY,
        env=_OBSERVATION_ENVIRONMENT,
        description=description,
    )


def _binary_version(argv: tuple[Path | str, ...], name: str) -> str:
    output = _capture(
        argv,
        cwd=_BUILD_DIRECTORY,
        description=f"{name} version observation",
    ).strip()
    match = _VERSION_PATTERN.fullmatch(output)
    if match is None:
        raise FinalManifestError(f"{name} returned an invalid version")
    return str(Version(match.group("version")))


def _required_uv_version(projection: FinalManifestInput) -> str:
    version = projection.toolchain.uv_image.resolved_version
    if version is None:  # pragma: no cover - BuildPlan validation owns the role.
        raise FinalManifestError("container uv identity is unavailable")
    return version


def _apt_version(name: str) -> str:
    return _capture(
        (_DPKG_QUERY_PATH, "-W", "-f=${Version}", "--", name),
        cwd=_BUILD_DIRECTORY,
        description=f"APT package observation for {name}",
    ).strip()


def _verify_tini() -> None:
    try:
        metadata = _TINI_PATH.lstat()
    except OSError as error:
        raise FinalManifestError("Tini executable is unavailable") from error
    if (
        _TINI_PATH.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or not metadata.st_mode & 0o111
    ):
        raise FinalManifestError("Tini executable identity is invalid")


def _capture(
    argv: tuple[Path | str, ...],
    *,
    cwd: Path,
    description: str,
) -> str:
    try:
        completed = subprocess.run(
            [os.fspath(item) for item in argv],
            cwd=cwd,
            env=_OBSERVATION_ENVIRONMENT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise FinalManifestError(f"{description} failed to start") from error
    if completed.returncode != 0:
        raise FinalManifestError(
            f"{description} failed with exit code {completed.returncode}"
        )
    return completed.stdout


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
