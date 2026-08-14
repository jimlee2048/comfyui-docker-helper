"""Exact application-environment installation helpers."""

from __future__ import annotations

import errno
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
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
    managed_runtime_constraints_bytes,
)
from comfyui_docker_helper.config.canonical_lock import (
    pytorch_core_version_matches_channel,
)
from comfyui_docker_helper.container.runners import ContainerRuntime, run_argv
from comfyui_docker_helper.errors import ApplicationError
from comfyui_docker_helper.pytorch_resolution import (
    pytorch_resolution_manifest_bytes,
)

_UV_PATH = Path("/usr/local/bin/uv")
_BUILD_DIRECTORY = Path("/opt/cdh/build")
_CONSTRAINTS_PATH = _BUILD_DIRECTORY / "python-package-constraints.txt"
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
    application: ApplicationPhase,
    toolchain: ToolchainPhase,
    *,
    runtime: ContainerRuntime,
    uv_path: Path = _UV_PATH,
    constraints_path: Path = _CONSTRAINTS_PATH,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Install and verify one BuildPlan-owned exact PyTorch group."""
    _validate_group(application, toolchain, runtime)
    group = application.pytorch
    with _pytorch_resolution_project(application) as resolution_manifest_path:
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
            env=application_install_environment(environ),
            description="inference package install",
        )
    expected = {package.name: package.version for package in group.packages}
    observed = dict(_application_inventory(application, runtime))
    mismatches = {
        name: (version, observed.get(name))
        for name, version in expected.items()
        if observed.get(name) != version
    }
    if mismatches:
        raise ApplicationInstallError(
            f"inference package identity changed: {mismatches!r}"
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
        env=application_install_environment(environ),
        description="application dependency verification",
    )
    _write_constraints(
        constraints_path,
        managed_runtime_constraints_bytes(group),
        owner_uid=0,
        owner_gid=0,
    )


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
            **application_install_environment(environ),
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
        env=application_install_environment(environ),
        description="application dependency verification",
    )
    _verify_application_pip_commands(application, runtime, environ)


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


@contextmanager
def _pytorch_resolution_project(application: ApplicationPhase):
    """Materialize routing from the admitted plan only for its uv invocation."""
    group = application.pytorch
    content = pytorch_resolution_manifest_bytes(
        requirements=tuple(package.requirement for package in group.packages),
        pytorch_index_packages=tuple(
            package.name
            for package in group.packages
            if package.direct_reference is None
        ),
        python_version=group.python_version,
        python_index_url=group.python_index_url,
        pytorch_index_url=group.pytorch_index_url,
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix=".pytorch-resolution-", dir=_BUILD_DIRECTORY
        ) as raw:
            path = Path(raw) / "pyproject.toml"
            path.write_bytes(content)
            path.chmod(0o444)
            yield path
    except OSError as error:
        raise ApplicationInstallError(
            "PyTorch resolution project could not be materialized"
        ) from error


def _write_constraints(
    path: Path,
    content: bytes,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            try:
                descriptor = os.open(
                    path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
            except OSError as error:
                if error.errno == errno.EEXIST:
                    raise ApplicationInstallError(
                        "managed constraints target already exists"
                    ) from error
                raise
            try:
                stream = os.fdopen(descriptor, "w+b")
            except BaseException:
                os.close(descriptor)
                raise
            with stream:
                written = stream.write(content)
                if written != len(content):
                    raise ApplicationInstallError(
                        "managed constraints could not be written"
                    )
                stream.flush()
                os.fchown(stream.fileno(), owner_uid, owner_gid)
                os.fchmod(stream.fileno(), 0o444)

                metadata = os.fstat(stream.fileno())
                stream.seek(0)
                observed = stream.read(len(content) + 1)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != owner_uid
                    or metadata.st_gid != owner_gid
                    or stat.S_IMODE(metadata.st_mode) != 0o444
                    or observed != content
                ):
                    raise ApplicationInstallError(
                        "managed constraints verification failed"
                    )

                target_metadata = os.stat(
                    path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(target_metadata.st_mode) or (
                    target_metadata.st_dev,
                    target_metadata.st_ino,
                ) != (metadata.st_dev, metadata.st_ino):
                    raise ApplicationInstallError(
                        "managed constraints verification failed"
                    )
        finally:
            os.close(parent_fd)
    except ApplicationInstallError:
        raise
    except OSError as error:
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
    if content != managed_runtime_constraints_bytes(application.pytorch):
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
        "x86_64",
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
    environment = application_install_environment(environ)
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


def application_install_environment(
    environ: Mapping[str, str] | None,
    *,
    constraints_path: Path | None = None,
    comfyui_path: Path | None = None,
    virtual_env: Path | None = None,
) -> dict[str, str]:
    """Build the narrow environment owned by application installation."""
    source = os.environ if environ is None else environ
    result = {
        name: source[name]
        for name in _NETWORK_ENVIRONMENT
        if source.get(name) is not None
    }
    result.update({"HOME": "/root", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"})
    if constraints_path is not None:
        result.update(
            {
                "PIP_CONSTRAINT": os.fspath(constraints_path),
                "UV_CONSTRAINT": os.fspath(constraints_path),
            }
        )
    if comfyui_path is not None:
        result["COMFYUI_PATH"] = os.fspath(comfyui_path)
    if virtual_env is not None:
        result["VIRTUAL_ENV"] = os.fspath(virtual_env)
    return result
