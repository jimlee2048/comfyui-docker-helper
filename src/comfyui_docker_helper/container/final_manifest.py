"""Final build observation and canonical manifest emission."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.build_plan import dump_manifest_binding_json
from comfyui_docker_helper.config.custom_node_inventory import (
    dump_custom_node_inventory,
)
from comfyui_docker_helper.config.final_manifest import (
    ApplicationEvidence,
    AptPackageEvidence,
    ComfyCliEvidence,
    ComfyUISourceEvidence,
    DigestEvidence,
    DisabledManagerEvidence,
    EnabledManagerEvidence,
    FileEvidence,
    FinalManifest,
    HookEvidence,
    ImageEvidence,
    InventoryDistribution,
    LifecycleEvidence,
    MaterializedInputsEvidence,
    PlatformEvidence,
    ProtectedRequirementEvidence,
    SetuptoolsEvidence,
    ToolchainEvidence,
    ToolEnvironmentEvidence,
    VersionEvidence,
    dump_final_manifest,
)
from comfyui_docker_helper.container.application_installer import (
    run_application_checker,
)
from comfyui_docker_helper.container.build_plan_input import FinalManifestInput
from comfyui_docker_helper.container.comfyui_installer import (
    capture_application_requirements,
    capture_manager_authority,
    observe_application_state,
    observe_manager_absence,
)
from comfyui_docker_helper.container.evidence_writer import (
    ApplicationEvidenceError,
    write_application_evidence,
)
from comfyui_docker_helper.container.file_admission import read_regular_absolute_file
from comfyui_docker_helper.container.runners import ContainerRuntime, run_argv
from comfyui_docker_helper.container.transfer_core import verify_required_final
from comfyui_docker_helper.errors import ApplicationError
from comfyui_docker_helper.exact_ledger import UV_VERSION

_BUILD_DIRECTORY = Path("/opt/cdh/build")
_MANIFEST_PATH = _BUILD_DIRECTORY / "manifest.json"
_BINDING_PATH = _BUILD_DIRECTORY / "manifest-binding.json"
_CDH_REQUIREMENTS_PATH = _BUILD_DIRECTORY / "cdh-production-requirements.txt"
_CDH_INVENTORY_PATH = _BUILD_DIRECTORY / "cdh-production-inventory.txt"
_UV_PATH = Path("/usr/local/bin/uv")
_TINI_PATH = Path("/usr/bin/tini")
_GIT_PATH = Path("/usr/bin/git")
_DPKG_QUERY_PATH = Path("/usr/bin/dpkg-query")
_VERSION_PATTERN = re.compile(r"^(?:uv|uvx) (?P<version>\S+)(?: \([^\n]+\))?$")
_OBSERVATION_ENVIRONMENT = {
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}


class FinalManifestError(ApplicationError):
    """Final image evidence could not be proved or emitted."""


def emit_final_manifest(
    projection: FinalManifestInput,
    *,
    runtime: ContainerRuntime,
) -> FinalManifest:
    """Publish the canonical manifest only after every final observation passes."""
    manifest = _observe_final_manifest(projection, runtime=runtime)
    try:
        write_application_evidence(_MANIFEST_PATH, dump_final_manifest(manifest))
    except ApplicationEvidenceError as error:
        raise FinalManifestError(f"final manifest {error}") from error
    return manifest


def _observe_final_manifest(
    projection: FinalManifestInput,
    *,
    runtime: ContainerRuntime,
) -> FinalManifest:
    """Re-prove existing final state without publishing partial evidence."""
    _verify_binding(projection)
    application_authority = capture_application_requirements(
        projection.application, runtime
    )
    observe_application_state(
        projection.application,
        runtime,
        application_authority,
    )

    application_inventory = _environment_inventory(runtime.python)
    _verify_inventory_file(
        Path(projection.application.inventory_path),
        application_inventory,
        expected_mode=0o444,
    )
    manager = _manager_evidence(projection, runtime, application_inventory)
    direct_packages = _direct_application_packages(projection, application_inventory)
    if any(name == "torchaudio" for name, _identity in direct_packages):
        run_application_checker(
            runtime,
            "audio",
            {},
            environ=None,
            description="application audio capability verification",
        )
        audio_checks = ("import", "cpu-tensor", "resample", "mel-spectrogram")
    else:
        audio_checks = ()

    cdh_inventory = _environment_inventory(Path(sys.executable))
    expected_cdh_inventory = tuple(
        (item.name, item.version)
        for item in projection.toolchain.tool_store.cdh_closure
    )
    expected_cdh_inventory = tuple(
        sorted(
            (
                *expected_cdh_inventory,
                (
                    "comfyui-docker-helper",
                    projection.toolchain.python.cdh_version,
                ),
            )
        )
    )
    if cdh_inventory != expected_cdh_inventory:
        raise FinalManifestError("cdh environment inventory does not match BuildPlan")
    _verify_inventory_file(
        _CDH_INVENTORY_PATH,
        cdh_inventory,
        expected_mode=0o644,
    )
    _dependency_check(Path(sys.executable), "cdh dependency verification")

    comfy_cli = _comfy_cli_evidence(projection)
    uv_tools = tuple(
        _tool_evidence(
            tool.name,
            tool.version,
            tool.environment,
            Path(projection.toolchain.tool_store.tool_dir) / tool.name / "bin/python",
        )
        for tool in projection.toolchain.tool_store.uv_tools
    )

    custom_inventory = _custom_node_inventory(projection)
    files = _file_evidence(projection)
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

    cdh_requirements_digest = _sha256(
        _read_owned_regular(_CDH_REQUIREMENTS_PATH, expected_mode=0o644)
    )
    if cdh_requirements_digest != projection.toolchain.tool_store.requirements_digest:
        raise FinalManifestError("cdh requirements digest does not match BuildPlan")
    requirements_digest = application_authority.digest

    observed_commit = _capture(
        (_GIT_PATH, "-C", runtime.comfyui_path, "rev-parse", "HEAD"),
        cwd=runtime.comfyui_path,
        description="ComfyUI commit observation",
    ).strip()
    protected = tuple(
        ProtectedRequirementEvidence(
            package=item.package,
            extras=item.extras,
            selector=item.selector,
        )
        for item in application_authority.protected
    )
    expected_protected = tuple(
        (item.package, item.extras, item.selector)
        for item in projection.application.comfyui.requirements.protected
    )
    if tuple((item.package, item.extras, item.selector) for item in protected) != (
        expected_protected
    ):
        raise FinalManifestError(
            "ComfyUI protected projection does not match BuildPlan"
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
            host_uv_resolver_version=UV_VERSION,
            uv_build_version=projection.toolchain.python.uv_build_version,
            container_uv=VersionEvidence(
                intended=_required_uv_version(projection),
                observed=_binary_version((_UV_PATH, "--version"), "uv"),
            ),
            container_uvx=VersionEvidence(
                intended=_required_uv_version(projection),
                observed=_binary_version(
                    (Path("/usr/local/bin/uvx"), "--version"), "uvx"
                ),
            ),
            python=VersionEvidence(
                intended=projection.toolchain.python.version,
                observed=_python_version(runtime.python),
            ),
            python_provider="uv-managed",
            python_catalog_descriptor_digest=(
                projection.toolchain.python.catalog_descriptor_digest
            ),
            cdh=ToolEnvironmentEvidence(
                name="comfyui-docker-helper",
                environment="uv-tool:comfyui-docker-helper",
                direct=VersionEvidence(
                    intended=projection.toolchain.python.cdh_version,
                    observed=dict(cdh_inventory)["comfyui-docker-helper"],
                ),
                inventory=_inventory_models(cdh_inventory),
                dependency_check="passed",
            ),
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
            audio_checks=audio_checks,
        ),
        custom_nodes=custom_inventory,
        files=files,
        hooks=hooks,
        apt=apt,
        materialized_inputs=MaterializedInputsEvidence(
            cdh_source_digest=projection.toolchain.python.cdh_source_digest,
            cdh_requirements=DigestEvidence(
                intended=projection.toolchain.tool_store.requirements_digest,
                observed=cdh_requirements_digest,
            ),
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
                "entrypoint",
            ),
            shutdown_timeout=projection.shutdown_timeout,
        ),
    )


def _verify_binding(projection: FinalManifestInput) -> None:
    expected = dump_manifest_binding_json(projection.binding)
    if _read_owned_regular(_BINDING_PATH, expected_mode=0o644) != expected:
        raise FinalManifestError("manifest binding does not match the BuildPlan")


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
) -> tuple[tuple[str, VersionEvidence], ...]:
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
                VersionEvidence(intended=version, observed=observed[name]),
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


def _comfy_cli_evidence(
    projection: FinalManifestInput,
) -> ComfyCliEvidence | None:
    tool = projection.toolchain.tool_store.comfy_cli
    if tool is None:
        return None
    python = Path(projection.toolchain.tool_store.tool_dir) / tool.name / "bin/python"
    inventory = _environment_inventory(python)
    _verify_inventory_file(Path(tool.inventory_path), inventory, expected_mode=0o644)
    _dependency_check(python, "comfy-cli dependency verification")
    for command in tool.executables:
        link = Path(projection.toolchain.tool_store.bin_dir) / command
        expected = (
            Path(projection.toolchain.tool_store.tool_dir) / tool.name / "bin" / command
        )
        try:
            metadata = link.lstat()
            resolved = link.resolve(strict=True)
        except OSError as error:
            raise FinalManifestError(
                f"comfy-cli entrypoint is unavailable: {command}"
            ) from error
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or resolved != expected
        ):
            raise FinalManifestError(
                f"comfy-cli entrypoint ownership is invalid: {command}"
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
) -> ToolEnvironmentEvidence:
    inventory = _environment_inventory(python)
    _dependency_check(python, f"{name} dependency verification")
    observed = dict(inventory).get(name)
    if observed is None:
        raise FinalManifestError(f"uv tool observation is missing: {name}")
    return ToolEnvironmentEvidence(
        name=name,
        environment=environment,
        direct=VersionEvidence(intended=version, observed=observed),
        inventory=_inventory_models(inventory),
        dependency_check="passed",
    )


def _custom_node_inventory(projection: FinalManifestInput):
    path = Path(projection.custom_nodes.inventory_path)
    content = _read_owned_regular(path, expected_mode=0o444)
    expected = projection.custom_nodes.expected
    expected_bytes = dump_custom_node_inventory(expected)
    if content != expected_bytes:
        raise FinalManifestError("custom-node inventory does not match BuildPlan")
    return expected


def _file_evidence(projection: FinalManifestInput) -> tuple[FileEvidence, ...]:
    result: list[FileEvidence] = []
    root = Path(projection.application.paths.comfyui)
    for item in projection.files:
        target = Path(item.target)
        verify_required_final(
            root=root,
            target=target,
            expected_checksum=item.checksum,
        )
        if item.checksum is None:
            result.append(
                FileEvidence(
                    url=item.url,
                    target=item.target,
                    verification="unverified-moving",
                )
            )
        else:
            observed = _sha256(read_regular_absolute_file(item.target))
            result.append(
                FileEvidence(
                    url=item.url,
                    target=item.target,
                    verification="sha256",
                    intended_checksum=item.checksum,
                    observed_checksum=observed,
                )
            )
    return tuple(result)


def _hook_evidence(projection: FinalManifestInput) -> tuple[HookEvidence, ...]:
    result: list[HookEvidence] = []
    roots = {
        "custom-node": Path("/opt/cdh/build/inputs"),
        "runtime": Path("/opt/cdh/runtime/hooks"),
    }
    for hook in projection.materialized_hooks:
        observed = _sha256(
            read_regular_absolute_file(roots[hook.owner] / hook.relative_path)
        )
        result.append(
            HookEvidence(
                owner=hook.owner,
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
        description=f"{python} inventory observation",
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


def _verify_inventory_file(
    path: Path,
    inventory: tuple[tuple[str, str], ...],
    *,
    expected_mode: int,
) -> None:
    expected = "".join(f"{name}=={version}\n" for name, version in inventory).encode()
    if _read_owned_regular(path, expected_mode=expected_mode) != expected:
        raise FinalManifestError(f"factual inventory does not match: {path}")


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


def _python_version(python: Path) -> str:
    return _capture(
        (python, "-I", "-c", "import platform;print(platform.python_version())"),
        cwd=_BUILD_DIRECTORY,
        description="managed Python version observation",
    ).strip()


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


def _read_owned_regular(path: Path, *, expected_mode: int | None = None) -> bytes:
    try:
        metadata = path.lstat()
        content = read_regular_absolute_file(path)
    except (OSError, ValueError) as error:
        raise FinalManifestError(
            f"required build evidence is unavailable: {path}"
        ) from error
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        raise FinalManifestError(f"required build evidence is not root-owned: {path}")
    if expected_mode is not None and stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise FinalManifestError(f"required build evidence mode is invalid: {path}")
    return content


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
