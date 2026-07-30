"""Exact official ComfyUI checkout and root-requirements installation."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from importlib import metadata as importlib_metadata
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.utils import InvalidName, canonicalize_name

from comfyui_docker_helper.comfyui_requirements import (
    ComfyUIRequirementsError,
    ParsedComfyUIRequirements,
    ParsedManagerRequirements,
    parse_comfyui_requirements,
    parse_manager_requirements,
)
from comfyui_docker_helper.config.build_plan import (
    ApplicationPhase,
    ManagerCapabilityPlan,
    ToolchainPhase,
)
from comfyui_docker_helper.container.application_installer import (
    application_install_environment,
    install_inference_group,
    install_python_extras,
    verify_application_environment,
)
from comfyui_docker_helper.container.runners import ContainerRuntime, run_argv
from comfyui_docker_helper.errors import ApplicationError
from comfyui_docker_helper.exact_ledger import COMFYUI_MINIMUM_VERSION

_GIT_PATH = Path("/usr/bin/git")
_UV_PATH = Path("/usr/local/bin/uv")
_BUILD_DIRECTORY = Path("/opt/cdh/build")
_CONSTRAINTS_PATH = _BUILD_DIRECTORY / "python-package-constraints.txt"
_REQUIRED_ROOT_FILES = ("main.py", "requirements.txt", "comfy_extras/nodes_audio.py")
_MANAGER_IMPORT_NAME = "comfyui_manager"


class ComfyUIInstallError(ApplicationError):
    """Exact source or ordinary requirements installation failed."""


def install_comfyui(
    application: ApplicationPhase,
    toolchain: ToolchainPhase,
    *,
    runtime: ContainerRuntime,
    git_path: Path = _GIT_PATH,
    uv_path: Path = _UV_PATH,
    constraints_path: Path = _CONSTRAINTS_PATH,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Verify exact source before installing protected and ordinary requirements."""
    _validate_paths(application, runtime)
    _checkout_exact(application, runtime, git_path, environ)
    # Repeat verification immediately before the first package mutation.
    parsed, parsed_manager = _verify_checkout(application, runtime)
    install_inference_group(
        application,
        toolchain,
        runtime=runtime,
        uv_path=uv_path,
        constraints_path=constraints_path,
        environ=environ,
    )
    install_python_extras(
        application,
        runtime,
        uv_path=uv_path,
        constraints_path=constraints_path,
        environ=environ,
    )
    verify_application_environment(
        application,
        runtime,
        uv_path=uv_path,
        constraints_path=constraints_path,
        environ=environ,
    )
    _install_ordinary_requirements(
        application,
        parsed.ordinary,
        runtime,
        uv_path,
        constraints_path,
        environ,
    )
    verify_application_environment(
        application,
        runtime,
        uv_path=uv_path,
        constraints_path=constraints_path,
        environ=environ,
        ordinary_requirements=parsed.ordinary,
    )
    manager = application.comfyui.manager
    if manager is None:
        _verify_manager_absent(application, runtime)
    else:
        _install_manager_capability(
            application,
            manager,
            parsed_manager,
            runtime,
            uv_path,
            constraints_path,
            environ,
        )
    verify_application_environment(
        application,
        runtime,
        uv_path=uv_path,
        constraints_path=constraints_path,
        environ=environ,
        ordinary_requirements=parsed.ordinary,
    )


def observe_application_state(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    authority: ParsedComfyUIRequirements,
    *,
    git_path: Path = _GIT_PATH,
    uv_path: Path = _UV_PATH,
    constraints_path: Path = _CONSTRAINTS_PATH,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Observe complete application health against immutable requirements input."""
    _validate_paths(application, runtime)
    environment = application_install_environment(environ)
    _verify_stage_identity(application, runtime.comfyui_path, git_path, environment)
    current = _verify_requirements(
        application,
        runtime.comfyui_path / application.comfyui.requirements.path,
    )
    if current != authority:
        raise ComfyUIInstallError("ComfyUI requirements authority changed")
    verify_application_environment(
        application,
        runtime,
        uv_path=uv_path,
        constraints_path=constraints_path,
        environ=environ,
        ordinary_requirements=authority.ordinary,
    )


def capture_application_requirements(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
) -> ParsedComfyUIRequirements:
    """Capture the verified checkout-owned ordinary requirements before hooks."""
    _validate_paths(application, runtime)
    parsed, _parsed_manager = _verify_checkout(application, runtime)
    return parsed


def _validate_paths(application: ApplicationPhase, runtime: ContainerRuntime) -> None:
    if runtime.comfyui_path != Path(application.paths.comfyui):
        raise ComfyUIInstallError("ComfyUI target does not match BuildPlan")
    if runtime.workspace != Path(application.paths.workspace):
        raise ComfyUIInstallError("ComfyUI workspace does not match BuildPlan")
    if runtime.virtual_env != Path(application.paths.venv):
        raise ComfyUIInstallError("ComfyUI interpreter does not match BuildPlan")


def _checkout_exact(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    git_path: Path,
    environ: Mapping[str, str] | None,
) -> None:
    target = runtime.comfyui_path
    _require_absent(target)
    parent = _ensure_real_directory(target.parent)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=parent))
    environment = application_install_environment(environ)
    try:
        _run_git(
            (
                git_path,
                "clone",
                "--no-checkout",
                "--filter=blob:none",
                "--",
                application.comfyui.repository,
                stage,
            ),
            cwd=parent,
            env=environment,
            description="ComfyUI source clone",
        )
        _run_git(
            (
                git_path,
                "-C",
                stage,
                "checkout",
                "--detach",
                application.comfyui.commit,
                "--",
            ),
            cwd=parent,
            env=environment,
            description="ComfyUI exact checkout",
        )
        _verify_stage_identity(application, stage, git_path, environment)
        _verify_requirements(application, stage / application.comfyui.requirements.path)
        if application.comfyui.manager is not None:
            _read_manager_requirements(
                application,
                application.comfyui.manager,
                stage,
            )
        _require_absent(target)
        _rename_noreplace(stage, target)
    except BaseException:
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _verify_stage_identity(
    application: ApplicationPhase,
    stage: Path,
    git_path: Path,
    environment: Mapping[str, str],
) -> None:
    head = _run_git(
        (git_path, "-C", stage, "rev-parse", "HEAD"),
        cwd=stage.parent,
        env=environment,
        description="ComfyUI commit verification",
    )
    branch = _run_git(
        (git_path, "-C", stage, "rev-parse", "--abbrev-ref", "HEAD"),
        cwd=stage.parent,
        env=environment,
        description="ComfyUI detached checkout verification",
    )
    origin = _run_git(
        (git_path, "-C", stage, "remote", "get-url", "origin"),
        cwd=stage.parent,
        env=environment,
        description="ComfyUI origin verification",
    )
    if head != application.comfyui.commit or branch != "HEAD":
        raise ComfyUIInstallError("ComfyUI checkout commit does not match BuildPlan")
    if origin != application.comfyui.repository:
        raise ComfyUIInstallError("ComfyUI checkout origin does not match BuildPlan")
    _verify_floor_ancestry(
        stage,
        application.comfyui.floor_commit,
        application.comfyui.commit,
        git_path,
        environment,
    )
    required = list(_REQUIRED_ROOT_FILES)
    if application.comfyui.manager is not None:
        required.append(application.comfyui.manager.requirements_path)
    for relative in required:
        path = stage / relative
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ComfyUIInstallError(
                f"ComfyUI checkout is missing required file {relative}"
            ) from error
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ComfyUIInstallError(
                f"ComfyUI required file is not regular: {relative}"
            )


def _verify_floor_ancestry(
    stage: Path,
    floor_commit: str,
    commit: str,
    git_path: Path,
    environment: Mapping[str, str],
) -> None:
    command = [
        os.fspath(git_path),
        "-C",
        os.fspath(stage),
        "merge-base",
        "--is-ancestor",
        floor_commit,
        commit,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=stage.parent,
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ComfyUIInstallError(
            "ComfyUI support-floor ancestry verification failed to start"
        ) from error
    if completed.returncode == 0:
        return
    if completed.returncode == 1:
        raise ComfyUIInstallError(
            "ComfyUI checkout is older than the supported "
            f"v{COMFYUI_MINIMUM_VERSION} floor"
        )
    raise ComfyUIInstallError(
        "ComfyUI support-floor ancestry could not be proven from official history"
    )


def _verify_checkout(application: ApplicationPhase, runtime: ContainerRuntime):
    target = runtime.comfyui_path
    try:
        metadata = target.lstat()
    except OSError as error:
        raise ComfyUIInstallError("ComfyUI checkout is unavailable") from error
    if target.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ComfyUIInstallError("ComfyUI checkout must be one real directory")
    parsed = _verify_requirements(
        application, target / application.comfyui.requirements.path
    )
    manager = application.comfyui.manager
    parsed_manager = (
        None
        if manager is None
        else _read_manager_requirements(application, manager, target)
    )
    return parsed, parsed_manager


def _verify_requirements(application: ApplicationPhase, path: Path):
    try:
        metadata = path.lstat()
        content = path.read_bytes()
    except OSError as error:
        raise ComfyUIInstallError("ComfyUI requirements could not be read") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ComfyUIInstallError("ComfyUI requirements must be one regular file")
    expected = application.comfyui.requirements
    try:
        parsed = parse_comfyui_requirements(
            content,
            python_version=expected.python_version,
            platform=expected.platform,
            machine="x86_64",
            protected_names=expected.protected_names,
        )
    except ComfyUIRequirementsError as error:
        raise ComfyUIInstallError(str(error)) from error
    projection = tuple(
        (item.package, tuple(item.extras), item.selector) for item in parsed.protected
    )
    expected_projection = tuple(
        (item.package, item.extras, item.selector) for item in expected.protected
    )
    if parsed.digest != expected.digest or projection != expected_projection:
        raise ComfyUIInstallError(
            "ComfyUI requirements do not match the canonical projection"
        )
    return parsed


def _install_ordinary_requirements(
    application: ApplicationPhase,
    ordinary: tuple[str, ...],
    runtime: ContainerRuntime,
    uv_path: Path,
    constraints_path: Path,
    environ: Mapping[str, str] | None,
) -> None:
    if not ordinary:
        return
    with _temporary_requirements_input(
        ordinary, prefix="comfyui-requirements-"
    ) as temporary:
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
                "--requirements",
                temporary,
            ),
            cwd=_BUILD_DIRECTORY,
            env=application_install_environment(
                environ, constraints_path=constraints_path
            ),
            description="ComfyUI ordinary requirements install",
        )


def _install_manager_capability(
    application: ApplicationPhase,
    manager: ManagerCapabilityPlan,
    parsed: ParsedManagerRequirements | None,
    runtime: ContainerRuntime,
    uv_path: Path,
    constraints_path: Path,
    environ: Mapping[str, str] | None,
) -> None:
    if parsed is None:
        raise ComfyUIInstallError("Manager requirements were not verified")
    with _temporary_requirements_input(
        parsed.rows, prefix="manager-requirements-"
    ) as temporary:
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
                "--requirements",
                temporary,
            ),
            cwd=_BUILD_DIRECTORY,
            env=application_install_environment(
                environ, constraints_path=constraints_path
            ),
            description="Manager requirements install",
        )
    _write_import_anchor(Path(manager.import_anchor), runtime.comfyui_path)
    _verify_manager_capability(
        application,
        manager,
        parsed,
        runtime,
    )


@contextmanager
def _temporary_requirements_input(
    rows: tuple[str, ...],
    *,
    prefix: str,
) -> Iterator[Path]:
    descriptor, name = tempfile.mkstemp(
        prefix=prefix,
        suffix=".txt",
        dir=_BUILD_DIRECTORY,
    )
    path = Path(name)
    try:
        try:
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            stream.write("\n".join(rows) + "\n")
        yield path
    finally:
        path.unlink(missing_ok=True)


def _verify_manager_capability(
    application: ApplicationPhase,
    manager: ManagerCapabilityPlan,
    parsed: ParsedManagerRequirements,
    runtime: ContainerRuntime,
) -> None:
    _verify_manager_import_root(application, manager, runtime)
    _verify_manager_import_anchor(application, manager, runtime)
    _verify_declared_manager_distributions(application, parsed, runtime)
    _verify_cm_cli(Path(manager.executable), runtime)


def _verify_manager_import_root(
    application: ApplicationPhase,
    manager: ManagerCapabilityPlan,
    runtime: ContainerRuntime,
) -> None:
    path = _manager_site_packages(application, runtime) / manager.import_name
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ComfyUIInstallError("Manager import root is unavailable") from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or resolved != path:
        raise ComfyUIInstallError(
            "Manager import root must be one real non-symlink directory"
        )


def _manager_site_packages(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
) -> Path:
    application_venv = Path(application.paths.venv)
    if runtime.virtual_env != application_venv:
        raise ComfyUIInstallError("Manager runtime does not match the application venv")
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    return application_venv / "lib" / f"python{python_minor}" / "site-packages"


def _verify_manager_import_anchor(
    application: ApplicationPhase,
    manager: ManagerCapabilityPlan,
    runtime: ContainerRuntime,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> Path:
    expected_parent = _manager_site_packages(application, runtime)
    path = Path(manager.import_anchor)
    if path.parent != expected_parent:
        raise ComfyUIInstallError(
            "Manager import anchor is outside application site-packages"
        )
    _require_existing_real_directory(expected_parent)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ComfyUIInstallError("Manager import anchor is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ComfyUIInstallError(
            "Manager import anchor must be one regular non-symlink file"
        )
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        raise ComfyUIInstallError("Manager import anchor ownership is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o444:
        raise ComfyUIInstallError("Manager import anchor mode must be 0444")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ComfyUIInstallError("Manager import anchor could not be read") from error
    if content != f"{runtime.comfyui_path}\n".encode():
        raise ComfyUIInstallError("Manager import anchor content does not match")
    return expected_parent


def _verify_declared_manager_distributions(
    application: ApplicationPhase,
    parsed: ParsedManagerRequirements,
    runtime: ContainerRuntime,
) -> None:
    site_packages = _manager_site_packages(application, runtime)
    manager = application.comfyui.manager
    if manager is None:  # pragma: no cover - caller owns the enabled capability.
        raise ComfyUIInstallError("Manager capability is unavailable")
    observed: dict[str, str] = {}
    distributions: dict[str, importlib_metadata.Distribution] = {}
    for distribution in importlib_metadata.distributions(path=[str(site_packages)]):
        controlled_entries = tuple(
            entrypoint
            for entrypoint in distribution.entry_points
            if entrypoint.group == "console_scripts"
            and entrypoint.name == manager.entrypoint_name
        )
        name = distribution.metadata.get("Name")
        if not name:
            if controlled_entries:
                raise ComfyUIInstallError(
                    "Manager cm-cli console owner is unidentifiable"
                )
            continue
        try:
            normalized = canonicalize_name(name, validate=True)
        except InvalidName as error:
            if controlled_entries:
                raise ComfyUIInstallError(
                    "Manager cm-cli console owner is unidentifiable"
                ) from error
            continue
        if normalized in observed:
            raise ComfyUIInstallError(
                f"Manager environment duplicates distribution {normalized}"
            )
        observed[normalized] = distribution.version
        distributions[normalized] = distribution
    for declared in parsed.active:
        actual = observed.get(declared.package)
        if actual is None or not SpecifierSet(declared.specifier).contains(
            actual, prereleases=True
        ):
            raise ComfyUIInstallError(
                f"installed {declared.package} does not satisfy Manager requirements"
            )
    if observed.get(manager.distribution) != parsed.manager_version:
        raise ComfyUIInstallError(
            "installed Manager does not match checkout requirements"
        )
    owners = tuple(
        name
        for name, distribution in distributions.items()
        for entrypoint in distribution.entry_points
        if entrypoint.group == "console_scripts"
        and entrypoint.name == manager.entrypoint_name
    )
    if owners != (manager.distribution,):
        raise ComfyUIInstallError("Manager cm-cli console ownership is invalid")


def capture_manager_authority(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
) -> ParsedManagerRequirements:
    """Capture and prove the immutable Manager desired-state authority."""
    manager = application.comfyui.manager
    if manager is None:
        raise ComfyUIInstallError("Manager capability is unavailable")
    parsed = _read_manager_requirements(application, manager, runtime.comfyui_path)
    _verify_manager_capability(application, manager, parsed, runtime)
    return parsed


def verify_manager_authority(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    authority: ParsedManagerRequirements,
) -> None:
    """Prove that the current checkout still matches immutable Manager input."""
    manager = application.comfyui.manager
    if manager is None:
        raise ComfyUIInstallError("Manager capability is unavailable")
    current = _read_manager_requirements(application, manager, runtime.comfyui_path)
    if current != authority:
        raise ComfyUIInstallError("Manager requirements authority changed")


def observe_manager_capability(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
    authority: ParsedManagerRequirements,
) -> None:
    """Observe complete Manager health against an already-admitted authority."""
    manager = application.comfyui.manager
    if manager is None:
        raise ComfyUIInstallError("Manager capability is unavailable")
    _verify_manager_capability(application, manager, authority, runtime)


def observe_manager_absence(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
) -> None:
    """Observe the complete disabled Manager desired-state contract."""
    _verify_manager_absent(application, runtime)


def _read_manager_requirements(
    application: ApplicationPhase,
    manager: ManagerCapabilityPlan,
    comfyui_path: Path,
) -> ParsedManagerRequirements:
    path = comfyui_path / manager.requirements_path
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ComfyUIInstallError("Manager requirements could not be read") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ComfyUIInstallError("Manager requirements must be one regular file")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ComfyUIInstallError("Manager requirements could not be read") from error
    try:
        return parse_manager_requirements(
            content,
            python_version=application.pytorch.python_version,
            platform=application.pytorch.platform,
            machine="x86_64",
        )
    except ComfyUIRequirementsError as error:
        raise ComfyUIInstallError(str(error)) from error


def _write_import_anchor(
    path: Path,
    comfyui_path: Path,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    content = f"{comfyui_path}\n".encode()
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent_descriptor,
            )
            try:
                remaining = memoryview(content)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written == 0:
                        raise OSError(errno.EIO, "incomplete anchor write")
                    remaining = remaining[written:]
                os.fchown(descriptor, owner_uid, owner_gid)
                os.fchmod(descriptor, 0o444)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise ComfyUIInstallError("Manager import anchor already exists") from error
        raise ComfyUIInstallError(
            "Manager import anchor could not be written"
        ) from error


def _verify_cm_cli(
    path: Path,
    runtime: ContainerRuntime,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    try:
        metadata = path.lstat()
        first_line = path.read_bytes().splitlines()[0]
    except (OSError, IndexError) as error:
        raise ComfyUIInstallError("Manager cm-cli executable is unavailable") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ComfyUIInstallError("Manager cm-cli must be one regular executable")
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        raise ComfyUIInstallError("Manager cm-cli must be root-owned")
    if not metadata.st_mode & 0o111:
        raise ComfyUIInstallError("Manager cm-cli is not executable")
    if first_line != f"#!{runtime.python}".encode():
        raise ComfyUIInstallError(
            "Manager cm-cli interpreter does not match the application"
        )


def _verify_manager_absent(
    application: ApplicationPhase,
    runtime: ContainerRuntime,
) -> None:
    site_packages = _manager_site_packages(application, runtime)
    anchor = site_packages / "comfyui-docker-helper-comfyui.pth"
    for path, subject in (
        (runtime.virtual_env / "bin/cm-cli", "Manager cm-cli"),
        (anchor, "Manager import anchor"),
        (site_packages / _MANAGER_IMPORT_NAME, "Manager import root"),
    ):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ComfyUIInstallError(f"{subject} could not be inspected") from error
        raise ComfyUIInstallError(f"{subject} exists while Manager is disabled")
    for distribution in importlib_metadata.distributions(path=[str(anchor.parent)]):
        raw_name = distribution.metadata.get("Name")
        if raw_name is None:
            continue
        try:
            name = canonicalize_name(raw_name, validate=True)
        except InvalidName:
            continue
        if name == "comfyui-manager":
            raise ComfyUIInstallError(
                "Manager distribution exists while Manager is disabled"
            )


def _require_existing_real_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ComfyUIInstallError(
            "Manager import-anchor parent is unavailable"
        ) from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or resolved != path:
        raise ComfyUIInstallError(
            "Manager import-anchor parent must be one real directory"
        )


def _require_absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ComfyUIInstallError("ComfyUI target could not be inspected") from error
    raise ComfyUIInstallError("ComfyUI target already exists")


def _ensure_real_directory(path: Path) -> Path:
    missing: list[Path] = []
    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise ComfyUIInstallError(
                    "ComfyUI target parent is unavailable"
                ) from None
            current = parent
            continue
        except OSError as error:
            raise ComfyUIInstallError(
                "ComfyUI target parent could not be inspected"
            ) from error
        if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ComfyUIInstallError("ComfyUI target parent must be a real directory")
        break
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o755)
            metadata = directory.lstat()
        except OSError as error:
            raise ComfyUIInstallError(
                "ComfyUI target parent could not be created"
            ) from error
        if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ComfyUIInstallError("ComfyUI target parent must be a real directory")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise ComfyUIInstallError(
            "ComfyUI target parent could not be resolved"
        ) from error


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically place one Linux directory without replacing any target entry."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError) as error:  # pragma: no cover - Ubuntu owns it.
        raise ComfyUIInstallError("atomic ComfyUI placement is unavailable") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ComfyUIInstallError("ComfyUI target already exists")
    raise ComfyUIInstallError(
        f"atomic ComfyUI placement failed: {os.strerror(error_number)}"
    )


def _run_git(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: Mapping[str, str],
    description: str,
) -> str:
    command = [os.fspath(item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ComfyUIInstallError(f"{description} failed to start") from error
    if completed.returncode != 0:
        raise ComfyUIInstallError(
            f"{description} failed with exit code {completed.returncode}"
        )
    return completed.stdout.strip()
