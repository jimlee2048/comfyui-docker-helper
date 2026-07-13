"""Exact application-environment installation helpers."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

from packaging.specifiers import SpecifierSet

from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    ToolchainPhase,
    managed_constraints_bytes,
)
from comfyui_docker_helper.config.canonical_lock import (
    pytorch_core_version_matches_channel,
)
from comfyui_docker_helper.container.phase_inputs import load_phase_input
from comfyui_docker_helper.container.runners import ContainerRuntime, run_argv
from comfyui_docker_helper.errors import ApplicationError
from comfyui_docker_helper.pytorch_resolution import (
    pytorch_resolution_manifest_bytes,
)

_UV_PATH = Path("/usr/local/bin/uv")
_BUILD_DIRECTORY = Path("/opt/cdh/build")
_CONSTRAINTS_PATH = _BUILD_DIRECTORY / "python-package-constraints.txt"
_RESOLUTION_MANIFEST_PATH = _BUILD_DIRECTORY / "pyproject.toml"
_NETWORK_ENVIRONMENT = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class ApplicationInstallError(ApplicationError):
    """An exact application-environment install invariant failed."""


def install_inference_group(
    application_phase_path: str | Path,
    toolchain_phase_path: str | Path,
    *,
    expected_build_plan_digest: str,
    runtime: ContainerRuntime,
    uv_path: Path = _UV_PATH,
    constraints_path: Path = _CONSTRAINTS_PATH,
    resolution_manifest_path: Path = _RESOLUTION_MANIFEST_PATH,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Install and verify one BuildPlan-owned exact PyTorch group."""
    application = load_phase_input(
        application_phase_path,
        "application",
        expected_build_plan_digest=expected_build_plan_digest,
    )
    toolchain = load_phase_input(
        toolchain_phase_path,
        "toolchain",
        expected_build_plan_digest=expected_build_plan_digest,
    )
    if not isinstance(application, ApplicationPhase) or not isinstance(
        toolchain, ToolchainPhase
    ):  # pragma: no cover - phase loader owns this invariant.
        raise ApplicationInstallError("invalid inference phase inputs")
    _validate_group(application, toolchain, runtime)
    _verify_resolution_manifest(
        resolution_manifest_path, application, expected_owner_uid=0
    )

    group = application.pytorch
    run_argv(
        [
            uv_path,
            "--no-config",
            "--project",
            str(resolution_manifest_path.parent),
            "pip",
            "install",
            "--python",
            runtime.python,
            "--no-python-downloads",
            "--requirements",
            str(resolution_manifest_path),
        ],
        cwd=_BUILD_DIRECTORY,
        env=_isolated_install_environment(environ),
        description="inference package install",
    )
    expected = {package.name: package.version for package in group.packages}
    run_argv(
        [
            runtime.python,
            "-I",
            "-c",
            _INVENTORY_CHECK,
            json.dumps(expected),
        ],
        cwd=_BUILD_DIRECTORY,
        env=_isolated_install_environment(environ),
        description="inference package verification",
    )
    _verify_setuptools_compatibility(application, runtime)
    run_argv(
        [
            uv_path,
            "--no-config",
            "pip",
            "check",
            "--python",
            runtime.python,
            "--no-python-downloads",
        ],
        cwd=_BUILD_DIRECTORY,
        env=_isolated_install_environment(environ),
        description="application dependency verification",
    )
    _write_constraints(
        constraints_path,
        managed_constraints_bytes(group),
        owner_uid=0,
        owner_gid=0,
    )
    resolution_manifest_path.unlink()


def _validate_group(
    application: ApplicationPhase,
    toolchain: ToolchainPhase,
    runtime: ContainerRuntime,
) -> None:
    group = application.pytorch
    if group.group != "pytorch":
        raise ApplicationInstallError("inference phase is not a PyTorch group")
    if group.python_version != toolchain.python.version:
        raise ApplicationInstallError("inference Python does not match the toolchain")
    if group.platform != toolchain.platform:
        raise ApplicationInstallError("inference platform does not match the toolchain")
    if group.backend != "cuda" or group.channel != toolchain.pytorch_channel:
        raise ApplicationInstallError("inference backend does not match the toolchain")
    if not group.pytorch_index_url.rstrip("/").endswith(
        f"/{toolchain.pytorch_channel}"
    ):
        raise ApplicationInstallError("inference index does not match the channel")
    if group.python_index_url != application.python_index_url:
        raise ApplicationInstallError(
            "inference Python index does not match application"
        )
    if runtime.virtual_env != Path(application.paths.venv):
        raise ApplicationInstallError(
            "application interpreter does not match BuildPlan"
        )
    names = {package.name for package in group.packages}
    if len(names) != len(group.packages):
        raise ApplicationInstallError("inference group contains duplicate packages")
    if "torch" not in names:
        raise ApplicationInstallError("inference group is missing torch")
    if any(
        not pytorch_core_version_matches_channel(
            package.name, package.version, toolchain.pytorch_channel
        )
        for package in group.packages
    ):
        raise ApplicationInstallError("inference package channel does not match")


def _verify_resolution_manifest(
    path: Path,
    application: ApplicationPhase,
    *,
    expected_owner_uid: int,
    expected_owner_gid: int = 0,
) -> None:
    try:
        metadata = path.lstat()
        content = path.read_bytes()
    except OSError as error:
        raise ApplicationInstallError(
            "PyTorch resolution manifest could not be read"
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ApplicationInstallError(
            "PyTorch resolution manifest must be a regular file"
        )
    if metadata.st_uid != expected_owner_uid or metadata.st_gid != expected_owner_gid:
        raise ApplicationInstallError("PyTorch resolution manifest must be root-owned")
    if stat.S_IMODE(metadata.st_mode) != 0o444:
        raise ApplicationInstallError("PyTorch resolution manifest must be read-only")
    group = application.pytorch
    expected = pytorch_resolution_manifest_bytes(
        requirements=tuple(package.requirement for package in group.packages),
        direct_packages=tuple(package.name for package in group.packages),
        python_version=group.python_version,
        python_index_url=group.python_index_url,
        pytorch_index_url=group.pytorch_index_url,
    )
    if content != expected:
        raise ApplicationInstallError(
            "PyTorch resolution manifest does not match BuildPlan"
        )


def _write_constraints(
    path: Path,
    content: bytes,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    if path.exists() or path.is_symlink():
        raise ApplicationInstallError("managed constraints target already exists")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
            os.fchown(stream.fileno(), owner_uid, owner_gid)
        temporary.replace(path)
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ApplicationInstallError(
            "managed constraints could not be written"
        ) from error


def _verify_setuptools_compatibility(
    application: ApplicationPhase, runtime: ContainerRuntime
) -> None:
    specifier = application.pytorch.setuptools_specifier
    if specifier is None:
        return
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    site_packages = (
        runtime.virtual_env / "lib" / f"python{python_minor}" / "site-packages"
    )
    versions = {
        distribution.metadata["Name"].lower(): distribution.version
        for distribution in metadata.distributions(path=[str(site_packages)])
        if distribution.metadata["Name"]
    }
    actual = versions.get("setuptools")
    if actual is None or not SpecifierSet(specifier).contains(actual):
        raise ApplicationInstallError(
            "installed setuptools does not satisfy PyTorch wheel metadata"
        )


def _isolated_install_environment(
    environ: Mapping[str, str] | None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    result = {
        name: source[name]
        for name in _NETWORK_ENVIRONMENT
        if source.get(name) is not None
    }
    result.update({"HOME": "/root", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"})
    return result


_INVENTORY_CHECK = "; ".join(
    (
        "import importlib.metadata as m, json, sys",
        "expected=json.loads(sys.argv[1])",
        "actual={name: m.version(name) for name in expected}",
        "assert actual == expected, (expected, actual)",
    )
)
