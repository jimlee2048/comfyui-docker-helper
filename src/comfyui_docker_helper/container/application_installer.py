"""Exact application-environment installation helpers."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.comfyui_requirements import target_marker_environment
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


@dataclass(frozen=True, slots=True)
class _InventoryFileIdentity:
    device: int
    inode: int


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


def install_python_extras(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    *,
    uv_path: Path = _UV_PATH,
    constraints_path: Path = _CONSTRAINTS_PATH,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Install the exact BuildPlan-owned ordinary Python extras."""
    _validate_application_package_owners(application)
    group = application.python_extras
    if group is None or not group.packages:
        return
    run_argv(
        (
            uv_path,
            "--no-config",
            "pip",
            "install",
            "--python",
            runtime.python,
            "--no-python-downloads",
            "--default-index",
            application.python_index_url,
            "--constraint",
            constraints_path,
            "--",
            *(package.requirement for package in group.packages),
        ),
        cwd=_BUILD_DIRECTORY,
        env={
            **_isolated_install_environment(environ),
            "PIP_CONSTRAINT": os.fspath(constraints_path),
            "UV_CONSTRAINT": os.fspath(constraints_path),
        },
        description="application Python extras install",
    )


def verify_application_environment(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    *,
    uv_path: Path = _UV_PATH,
    constraints_path: Path = _CONSTRAINTS_PATH,
    environ: Mapping[str, str] | None = None,
    ordinary_requirements: tuple[str, ...] = (),
    verify_capabilities: bool = True,
    write_inventory: bool = False,
) -> None:
    """Re-prove exact application packages, constraints, and dependency health."""
    _validate_application_package_owners(application)
    _verify_constraints(constraints_path, application)
    inventory = _application_inventory(application, runtime)
    observed = dict(inventory)
    expected = {"pip": application.pip_version}
    for package in application.pytorch.packages:
        expected[package.name] = package.version
    if application.python_extras is not None:
        for package in application.python_extras.packages:
            existing = expected.get(package.name)
            if existing is not None and existing != package.version:
                raise ApplicationInstallError(
                    f"application package {package.name} has conflicting identities"
                )
            expected[package.name] = package.version
    mismatches = {
        name: (version, observed.get(name))
        for name, version in expected.items()
        if observed.get(name) != version
    }
    if mismatches:
        raise ApplicationInstallError(
            f"application direct package identity changed: {mismatches!r}"
        )
    specifier = application.pytorch.setuptools_specifier
    actual_setuptools = observed.get("setuptools")
    if specifier is not None and (
        actual_setuptools is None
        or not SpecifierSet(specifier).contains(actual_setuptools)
    ):
        raise ApplicationInstallError(
            "installed setuptools does not satisfy PyTorch wheel metadata"
        )
    _verify_ordinary_requirements(application, ordinary_requirements, observed)
    run_argv(
        (
            uv_path,
            "--no-config",
            "pip",
            "check",
            "--python",
            runtime.python,
            "--no-python-downloads",
        ),
        cwd=_BUILD_DIRECTORY,
        env=_isolated_install_environment(environ),
        description="application dependency verification",
    )
    if verify_capabilities:
        _verify_application_pip_commands(application, runtime, environ)
        _verify_application_imports(application, runtime, environ)
    if write_inventory:
        final_inventory = _application_inventory(application, runtime)
        if final_inventory != inventory:
            raise ApplicationInstallError(
                "application inventory changed during final verification"
            )
        _write_application_inventory(
            Path(application.inventory_path),
            b"".join(
                f"{name}=={version}\n".encode() for name, version in final_inventory
            ),
        )


def _validate_group(
    application: ApplicationPhase,
    toolchain: ToolchainPhase,
    runtime: ContainerRuntime,
) -> None:
    _validate_application_package_owners(application)
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


def _validate_application_package_owners(application: ApplicationPhase) -> None:
    python_names = (
        set()
        if application.python_extras is None
        else {package.name for package in application.python_extras.packages}
    )
    pytorch_names = {package.name for package in application.pytorch.packages}
    overlap = python_names.intersection({*pytorch_names, "pip", "setuptools"})
    invalid_pytorch = pytorch_names.intersection({"pip", "setuptools"})
    conflicts = overlap | invalid_pytorch
    if conflicts:
        raise ApplicationInstallError(
            f"application package owners overlap: {sorted(conflicts)!r}"
        )


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


def _verify_constraints(path: Path, application: ApplicationPhase) -> None:
    try:
        metadata = path.lstat()
        content = path.read_bytes()
    except OSError as error:
        raise ApplicationInstallError(
            "managed constraints could not be read"
        ) from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ApplicationInstallError("managed constraints must be a regular file")
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        raise ApplicationInstallError("managed constraints must be root-owned")
    if stat.S_IMODE(metadata.st_mode) != 0o444:
        raise ApplicationInstallError("managed constraints must be read-only")
    if content != managed_constraints_bytes(application.pytorch):
        raise ApplicationInstallError("managed constraints do not match BuildPlan")


def _application_inventory(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
) -> tuple[tuple[str, str], ...]:
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    site_packages = (
        runtime.virtual_env / "lib" / f"python{python_minor}" / "site-packages"
    )
    observed: dict[str, str] = {}
    for distribution in metadata.distributions(path=[str(site_packages)]):
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            raise ApplicationInstallError(
                "application inventory contains an unidentifiable distribution"
            )
        try:
            name = canonicalize_name(raw_name, validate=True)
            version = str(Version(distribution.version))
        except (InvalidName, InvalidVersion) as error:
            raise ApplicationInstallError(
                "application inventory contains an invalid distribution identity"
            ) from error
        if name in observed:
            raise ApplicationInstallError(
                f"application inventory duplicates distribution {name}"
            )
        observed[name] = version
    return tuple(sorted(observed.items()))


def _verify_ordinary_requirements(
    application: ApplicationPhase,
    requirements: tuple[str, ...],
    observed: Mapping[str, str],
) -> None:
    environment = target_marker_environment(
        application.pytorch.python_version,
        application.pytorch.platform,
    )
    for row in requirements:
        try:
            requirement = Requirement(row)
            name = canonicalize_name(requirement.name, validate=True)
        except (InvalidName, InvalidRequirement) as error:
            raise ApplicationInstallError(
                "ComfyUI ordinary requirement identity is invalid"
            ) from error
        if requirement.marker is not None and not requirement.marker.evaluate(
            environment
        ):
            continue
        actual = observed.get(name)
        if actual is None or not requirement.specifier.contains(
            actual, prereleases=True
        ):
            raise ApplicationInstallError(
                f"installed {name} does not satisfy ComfyUI requirements"
            )


def _verify_application_pip_commands(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    environ: Mapping[str, str] | None,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    environment = _isolated_install_environment(environ)
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    site_packages_path = (
        runtime.virtual_env / "lib" / f"python{python_minor}" / "site-packages"
    )
    try:
        site_packages_metadata = site_packages_path.lstat()
        site_packages = site_packages_path.resolve(strict=True)
    except OSError as error:
        raise ApplicationInstallError(
            "application site-packages is unavailable"
        ) from error
    if (
        site_packages_path.is_symlink()
        or not stat.S_ISDIR(site_packages_metadata.st_mode)
        or site_packages != site_packages_path
    ):
        raise ApplicationInstallError(
            "application site-packages must be one real directory"
        )
    expected_shebang = f"#!{runtime.python}".encode()
    commands: list[tuple[Path, str]] = []
    for name in ("pip", "pip3"):
        path = runtime.virtual_env / "bin" / name
        try:
            metadata = path.lstat()
            first_line = path.read_bytes().splitlines()[0]
        except (OSError, IndexError) as error:
            raise ApplicationInstallError(
                f"application {name} executable is unavailable"
            ) from error
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ApplicationInstallError(
                f"application {name} must be one regular executable"
            )
        if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
            raise ApplicationInstallError(f"application {name} must be root-owned")
        if not metadata.st_mode & 0o111 or first_line != expected_shebang:
            raise ApplicationInstallError(
                f"application {name} does not target the application interpreter"
            )
        commands.append((path, f"application {name} command verification"))
    run_argv(
        (
            runtime.python,
            "-I",
            "-c",
            _PIP_CAPABILITY_CHECK,
            os.fspath(site_packages),
            os.fspath(runtime.comfyui_path),
            application.pip_version,
            os.fspath(runtime.virtual_env / "bin/pip"),
            os.fspath(runtime.virtual_env / "bin/pip3"),
        ),
        cwd=_BUILD_DIRECTORY,
        env=environment,
        description="application pip module verification",
    )
    commands.append((runtime.python, "application python -m pip verification"))
    for executable, description in commands:
        argv = (
            (executable, "-I", "-m", "pip", "--version")
            if executable == runtime.python
            else (executable, "--version")
        )
        output = _capture_application_command(argv, environment, description)
        match = _PIP_VERSION_PATTERN.fullmatch(output.strip())
        if match is None:
            raise ApplicationInstallError(f"{description} returned an invalid identity")
        reported_root = Path(match.group("root"))
        try:
            resolved_root = reported_root.resolve(strict=True)
        except OSError as error:
            raise ApplicationInstallError(
                f"{description} returned an unavailable package path"
            ) from error
        if (
            match.group("version") != application.pip_version
            or match.group("python") != python_minor
            or resolved_root != site_packages / "pip"
        ):
            raise ApplicationInstallError(
                f"{description} does not match the application pip owner"
            )


def _capture_application_command(
    argv: tuple[Path | str, ...],
    environment: Mapping[str, str],
    description: str,
) -> str:
    try:
        completed = subprocess.run(
            [os.fspath(item) for item in argv],
            cwd=_BUILD_DIRECTORY,
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ApplicationInstallError(f"{description} failed to start") from error
    if completed.returncode != 0:
        raise ApplicationInstallError(
            f"{description} failed with exit code {completed.returncode}"
        )
    return completed.stdout


_PIP_VERSION_PATTERN = re.compile(
    r"pip (?P<version>\S+) from (?P<root>.+) "
    r"\(python (?P<python>[0-9]+\.[0-9]+)\)"
)


_PIP_CAPABILITY_CHECK = """\
import base64
import hashlib
import importlib
import importlib.metadata as metadata
import importlib.util
import pathlib
import stat
import sys

site_packages_path = pathlib.Path(sys.argv[1])
site_packages_metadata = site_packages_path.lstat()
assert not site_packages_path.is_symlink()
assert stat.S_ISDIR(site_packages_metadata.st_mode)
site_packages = site_packages_path.resolve(strict=True)
assert site_packages == site_packages_path
workspace_path = pathlib.Path(sys.argv[2])
workspace_metadata = workspace_path.lstat()
assert not workspace_path.is_symlink()
assert stat.S_ISDIR(workspace_metadata.st_mode)
workspace = workspace_path.resolve(strict=True)
assert workspace == workspace_path
expected_version = sys.argv[3]
sys.path.insert(0, str(site_packages))
sys.path.insert(0, str(workspace))

owners = tuple(
    distribution
    for distribution in metadata.distributions(path=[str(site_packages)])
    if distribution.metadata.get("Name") is not None
    and distribution.metadata["Name"].lower().replace("_", "-").replace(".", "-")
    == "pip"
)
assert len(owners) == 1
owner = owners[0]
assert owner.version == expected_version
assert pathlib.Path(owner.locate_file("")).resolve(strict=True) == site_packages
for expected_command in (pathlib.Path(item) for item in sys.argv[4:]):
    owned_files = tuple(
        item
        for item in owner.files or ()
        if pathlib.Path(owner.locate_file(item)).resolve(strict=True)
        == expected_command
    )
    assert len(owned_files) == 1
    owned_file = owned_files[0]
    assert owned_file.hash is not None and owned_file.hash.mode == "sha256"
    digest = base64.urlsafe_b64encode(
        hashlib.sha256(expected_command.read_bytes()).digest()
    ).rstrip(b"=").decode("ascii")
    assert digest == owned_file.hash.value

pip_root_path = site_packages / "pip"
pip_root_metadata = pip_root_path.lstat()
assert not pip_root_path.is_symlink()
assert stat.S_ISDIR(pip_root_metadata.st_mode)
pip_root = pip_root_path.resolve(strict=True)
assert pip_root == pip_root_path and pip_root.is_relative_to(site_packages)
pip_init_path = pip_root_path / "__init__.py"
pip_init_metadata = pip_init_path.lstat()
assert not pip_init_path.is_symlink()
assert stat.S_ISREG(pip_init_metadata.st_mode)
pip_init = pip_init_path.resolve(strict=True)
assert pip_init == pip_init_path and pip_init.is_relative_to(pip_root)

def verify_spec(spec):
    assert spec is not None and spec.name == "pip"
    assert spec.origin is not None
    assert pathlib.Path(spec.origin).resolve(strict=True) == pip_init
    assert spec.submodule_search_locations is not None
    assert tuple(
        pathlib.Path(item).resolve(strict=True)
        for item in spec.submodule_search_locations
    ) == (pip_root,)

verify_spec(importlib.util.find_spec("pip"))
module = importlib.import_module("pip")
verify_spec(module.__spec__)
assert pathlib.Path(module.__file__).resolve(strict=True) == pip_init
assert tuple(pathlib.Path(item).resolve(strict=True) for item in module.__path__) == (
    pip_root,
)
"""


def _verify_application_imports(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    environ: Mapping[str, str] | None,
) -> None:
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    site_packages = (
        runtime.virtual_env / "lib" / f"python{python_minor}" / "site-packages"
    )
    import_names = tuple(
        package.name
        for package in application.pytorch.packages
        if package.name in {"torch", "torchaudio", "torchvision"}
    )
    run_argv(
        (
            runtime.python,
            "-I",
            "-c",
            _PYTORCH_CAPABILITY_CHECK,
            os.fspath(site_packages),
            os.fspath(runtime.comfyui_path),
            json.dumps(import_names),
        ),
        cwd=_BUILD_DIRECTORY,
        env=_isolated_install_environment(environ),
        description="PyTorch import capability verification",
    )
    run_argv(
        (
            runtime.python,
            "-I",
            "-c",
            _COMFYUI_CAPABILITY_CHECK,
            os.fspath(runtime.comfyui_path),
        ),
        cwd=_BUILD_DIRECTORY,
        env={
            **_isolated_install_environment(environ),
            "COMFYUI_PATH": os.fspath(runtime.comfyui_path),
            "VIRTUAL_ENV": os.fspath(runtime.virtual_env),
        },
        description="ComfyUI import capability verification",
    )


def _write_application_inventory(
    path: Path,
    content: bytes,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    parent = _require_real_inventory_parent(path.parent)
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ApplicationInstallError(
            "application inventory target could not be inspected"
        ) from error
    else:
        raise ApplicationInstallError("application inventory already exists")
    temporary: Path | None = None
    identity: _InventoryFileIdentity | None = None
    linked = False
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            opened = os.fstat(stream.fileno())
            identity = _InventoryFileIdentity(opened.st_dev, opened.st_ino)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
            os.fchown(stream.fileno(), owner_uid, owner_gid)
        if not _inventory_path_has_identity(temporary, identity):
            raise ApplicationInstallError(
                "application inventory temporary identity changed"
            )
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        if not _inventory_path_has_identity(path, identity):
            raise ApplicationInstallError(
                "application inventory linked identity changed"
            )
        _unlink_inventory_if_identity(temporary, identity)
        temporary = None
        result = path.lstat()
        if (result.st_dev, result.st_ino) != (identity.device, identity.inode):
            raise ApplicationInstallError(
                "application inventory target identity changed"
            )
        observed_content = path.read_bytes()
        if not _inventory_path_has_identity(path, identity):
            raise ApplicationInstallError(
                "application inventory target identity changed"
            )
        if (
            path.is_symlink()
            or not stat.S_ISREG(result.st_mode)
            or result.st_uid != owner_uid
            or result.st_gid != owner_gid
            or stat.S_IMODE(result.st_mode) != 0o444
            or observed_content != content
        ):
            raise ApplicationInstallError("application inventory verification failed")
    except FileExistsError as error:
        raise ApplicationInstallError("application inventory already exists") from error
    except ApplicationInstallError:
        if linked and identity is not None:
            _unlink_inventory_if_identity(path, identity)
        raise
    except OSError as error:
        if linked and identity is not None:
            _unlink_inventory_if_identity(path, identity)
        raise ApplicationInstallError(
            "application inventory could not be written"
        ) from error
    finally:
        if temporary is not None and identity is not None:
            _unlink_inventory_if_identity(temporary, identity)


def _require_real_inventory_parent(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ApplicationInstallError(
            "application inventory parent is unavailable"
        ) from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or resolved != path:
        raise ApplicationInstallError(
            "application inventory parent must be one real directory"
        )
    return resolved


def _inventory_path_has_identity(path: Path, identity: _InventoryFileIdentity) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (metadata.st_dev, metadata.st_ino) == (identity.device, identity.inode)


def _unlink_inventory_if_identity(path: Path, identity: _InventoryFileIdentity) -> None:
    if not _inventory_path_has_identity(path, identity):
        return
    with suppress(FileNotFoundError):
        path.unlink()


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


_PYTORCH_CAPABILITY_CHECK = """\
import importlib
import importlib.util
import json
import pathlib
import stat
import sys

site_packages_path = pathlib.Path(sys.argv[1])
site_packages_metadata = site_packages_path.lstat()
assert not site_packages_path.is_symlink()
assert stat.S_ISDIR(site_packages_metadata.st_mode)
site_packages = site_packages_path.resolve(strict=True)
assert site_packages == site_packages_path
workspace_path = pathlib.Path(sys.argv[2])
workspace_metadata = workspace_path.lstat()
assert not workspace_path.is_symlink()
assert stat.S_ISDIR(workspace_metadata.st_mode)
workspace = workspace_path.resolve(strict=True)
assert workspace == workspace_path
sys.path.insert(0, str(site_packages))
sys.path.insert(0, str(workspace))

for module_name in json.loads(sys.argv[3]):
    expected_root_path = site_packages / module_name
    expected_root_metadata = expected_root_path.lstat()
    assert not expected_root_path.is_symlink()
    assert stat.S_ISDIR(expected_root_metadata.st_mode)
    expected_root = expected_root_path.resolve(strict=True)
    assert expected_root == expected_root_path
    assert expected_root.is_relative_to(site_packages)
    expected_init_path = expected_root_path / "__init__.py"
    expected_init_metadata = expected_init_path.lstat()
    assert not expected_init_path.is_symlink()
    assert stat.S_ISREG(expected_init_metadata.st_mode)
    expected_init = expected_init_path.resolve(strict=True)
    assert expected_init == expected_init_path
    assert expected_init.is_relative_to(expected_root)

    def verify_spec(spec):
        assert spec is not None and spec.name == module_name
        assert spec.origin is not None
        origin = pathlib.Path(spec.origin).resolve(strict=True)
        assert origin == expected_init
        assert spec.submodule_search_locations is not None
        locations = tuple(
            pathlib.Path(item).resolve(strict=True)
            for item in spec.submodule_search_locations
        )
        assert locations == (expected_root,)

    module_spec = importlib.util.find_spec(module_name)
    verify_spec(module_spec)
    imported = importlib.import_module(module_name)
    verify_spec(imported.__spec__)
    assert pathlib.Path(imported.__file__).resolve(strict=True) == expected_init
    imported_locations = tuple(
        pathlib.Path(item).resolve(strict=True) for item in imported.__path__
    )
    assert imported_locations == (expected_root,)
"""


_COMFYUI_CAPABILITY_CHECK = """\
import importlib
import importlib.util
import pathlib
import stat
import sys

workspace_path = pathlib.Path(sys.argv[1])
workspace_metadata = workspace_path.lstat()
assert not workspace_path.is_symlink()
assert stat.S_ISDIR(workspace_metadata.st_mode)
workspace = workspace_path.resolve(strict=True)
assert workspace == workspace_path
sys.path.insert(0, str(workspace))

folder_paths_path = workspace / "folder_paths.py"
folder_paths_metadata = folder_paths_path.lstat()
assert not folder_paths_path.is_symlink()
assert stat.S_ISREG(folder_paths_metadata.st_mode)
folder_paths = folder_paths_path.resolve(strict=True)
assert folder_paths.is_relative_to(workspace)

folder_paths_spec = importlib.util.find_spec("folder_paths")
assert folder_paths_spec is not None and folder_paths_spec.origin is not None
folder_paths_origin = pathlib.Path(folder_paths_spec.origin).resolve(strict=True)
assert folder_paths_origin == folder_paths
assert folder_paths_origin.is_relative_to(workspace)
folder_paths_module = importlib.import_module("folder_paths")
folder_paths_imported_spec = folder_paths_module.__spec__
assert folder_paths_imported_spec is not None
assert folder_paths_imported_spec.name == "folder_paths"
assert folder_paths_imported_spec.origin is not None
folder_paths_imported_origin = pathlib.Path(folder_paths_imported_spec.origin).resolve(
    strict=True
)
assert folder_paths_imported_origin == folder_paths
assert pathlib.Path(folder_paths_module.__file__).resolve(strict=True) == folder_paths

comfy_root_path = workspace / "comfy"
comfy_root_metadata = comfy_root_path.lstat()
assert not comfy_root_path.is_symlink()
assert stat.S_ISDIR(comfy_root_metadata.st_mode)
comfy_root = comfy_root_path.resolve(strict=True)
assert comfy_root.is_relative_to(workspace)
comfy_init_path = comfy_root_path / "__init__.py"
comfy_init_metadata = comfy_init_path.lstat()
assert not comfy_init_path.is_symlink()
assert stat.S_ISREG(comfy_init_metadata.st_mode)
comfy_init = comfy_init_path.resolve(strict=True)
assert comfy_init.is_relative_to(comfy_root)
comfy_spec = importlib.util.find_spec("comfy")
assert comfy_spec is not None and comfy_spec.submodule_search_locations is not None
comfy_locations = tuple(
    pathlib.Path(item).resolve(strict=True)
    for item in comfy_spec.submodule_search_locations
)
assert comfy_locations and all(item == comfy_root for item in comfy_locations)
assert comfy_spec.origin is not None
comfy_origin = pathlib.Path(comfy_spec.origin).resolve(strict=True)
assert comfy_origin == comfy_init
assert comfy_origin.is_relative_to(comfy_root)
comfy_module = importlib.import_module("comfy")
comfy_imported_spec = comfy_module.__spec__
assert comfy_imported_spec is not None and comfy_imported_spec.name == "comfy"
assert comfy_imported_spec.origin is not None
assert pathlib.Path(comfy_imported_spec.origin).resolve(strict=True) == comfy_init
assert pathlib.Path(comfy_module.__file__).resolve(strict=True) == comfy_init
comfy_imported_locations = tuple(
    pathlib.Path(item).resolve(strict=True) for item in comfy_module.__path__
)
assert comfy_imported_locations == (comfy_root,)
"""
