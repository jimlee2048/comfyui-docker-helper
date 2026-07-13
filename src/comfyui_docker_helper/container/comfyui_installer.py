"""Exact official ComfyUI checkout and root-requirements installation."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from importlib import metadata as importlib_metadata
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from comfyui_docker_helper.comfyui_requirements import (
    ComfyUIRequirementsError,
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
    _isolated_install_environment,
    install_inference_group,
)
from comfyui_docker_helper.container.phase_inputs import load_phase_input
from comfyui_docker_helper.container.runners import ContainerRuntime, run_argv
from comfyui_docker_helper.errors import ApplicationError
from comfyui_docker_helper.exact_ledger import COMFYUI_MINIMUM_VERSION

_GIT_PATH = Path("/usr/bin/git")
_UV_PATH = Path("/usr/local/bin/uv")
_BUILD_DIRECTORY = Path("/opt/cdh/build")
_CONSTRAINTS_PATH = _BUILD_DIRECTORY / "python-package-constraints.txt"
_RESOLUTION_MANIFEST_PATH = _BUILD_DIRECTORY / "pyproject.toml"
_REQUIRED_ROOT_FILES = ("main.py", "requirements.txt", "comfy_extras/nodes_audio.py")


class ComfyUIInstallError(ApplicationError):
    """Exact source or ordinary requirements installation failed."""


def install_comfyui(
    application_phase_path: str | Path,
    toolchain_phase_path: str | Path,
    *,
    expected_build_plan_digest: str,
    runtime: ContainerRuntime,
    git_path: Path = _GIT_PATH,
    uv_path: Path = _UV_PATH,
    constraints_path: Path = _CONSTRAINTS_PATH,
    resolution_manifest_path: Path = _RESOLUTION_MANIFEST_PATH,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Verify exact source before installing protected and ordinary requirements."""
    application, _toolchain = _load_phases(
        application_phase_path,
        toolchain_phase_path,
        expected_build_plan_digest,
    )
    _validate_paths(application, runtime)
    _checkout_exact(application, runtime, git_path, environ)
    # Repeat verification immediately before the first package mutation.
    parsed, parsed_manager = _verify_checkout(application, runtime)
    install_inference_group(
        application_phase_path,
        toolchain_phase_path,
        expected_build_plan_digest=expected_build_plan_digest,
        runtime=runtime,
        uv_path=uv_path,
        constraints_path=constraints_path,
        resolution_manifest_path=resolution_manifest_path,
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
    manager = application.comfyui.manager
    if manager is None:
        _verify_manager_absent(application, runtime, environ)
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
    _check_application_health(runtime, uv_path, environ)


def _load_phases(
    application_path: str | Path,
    toolchain_path: str | Path,
    digest: str,
) -> tuple[ApplicationPhase, ToolchainPhase]:
    application = load_phase_input(
        application_path, "application", expected_build_plan_digest=digest
    )
    toolchain = load_phase_input(
        toolchain_path, "toolchain", expected_build_plan_digest=digest
    )
    if not isinstance(application, ApplicationPhase) or not isinstance(
        toolchain, ToolchainPhase
    ):  # pragma: no cover - strict phase loader owns the type.
        raise ComfyUIInstallError("invalid ComfyUI phase inputs")
    return application, toolchain


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
    environment = _isolated_install_environment(environ)
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
    origin = _run_git(
        (git_path, "-C", stage, "remote", "get-url", "origin"),
        cwd=stage.parent,
        env=environment,
        description="ComfyUI origin verification",
    )
    if head != application.comfyui.commit:
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
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix="comfyui-requirements-", suffix=".txt", dir=_BUILD_DIRECTORY
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(ordinary) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
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
            env=_application_install_environment(environ, constraints_path),
            description="ComfyUI ordinary requirements install",
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix="manager-requirements-", suffix=".txt", dir=_BUILD_DIRECTORY
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(parsed.rows) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
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
            env=_application_install_environment(environ, constraints_path),
            description="Manager requirements install",
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    _write_import_anchor(Path(manager.import_anchor), runtime.comfyui_path)
    _verify_declared_manager_distributions(application, parsed, runtime)
    site_packages = Path(manager.import_anchor).parent
    run_argv(
        (
            runtime.python,
            "-I",
            "-c",
            _MANAGER_CAPABILITY_CHECK,
            parsed.manager_version,
            os.fspath(runtime.comfyui_path),
            os.fspath(site_packages),
            manager.import_name,
            manager.entrypoint_name,
            manager.entrypoint_value,
        ),
        cwd=_BUILD_DIRECTORY,
        env={
            **_isolated_install_environment(environ),
            "COMFYUI_PATH": os.fspath(runtime.comfyui_path),
            "VIRTUAL_ENV": os.fspath(runtime.virtual_env),
        },
        description="Manager application capability verification",
    )
    _verify_cm_cli(Path(manager.executable), runtime)


def _verify_declared_manager_distributions(
    application: ApplicationPhase,
    parsed: ParsedManagerRequirements,
    runtime: ContainerRuntime,
) -> None:
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    site_packages = (
        runtime.virtual_env / "lib" / f"python{python_minor}" / "site-packages"
    )
    observed: dict[str, str] = {}
    for distribution in importlib_metadata.distributions(path=[str(site_packages)]):
        name = distribution.metadata["Name"]
        if not name:
            continue
        normalized = canonicalize_name(name)
        if normalized in observed:
            raise ComfyUIInstallError(
                f"Manager environment duplicates distribution {normalized}"
            )
        observed[normalized] = distribution.version
    for declared in parsed.active:
        actual = observed.get(declared.package)
        if actual is None or not SpecifierSet(declared.specifier).contains(
            actual, prereleases=True
        ):
            raise ComfyUIInstallError(
                f"installed {declared.package} does not satisfy Manager requirements"
            )


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
    _require_existing_real_directory(path.parent)
    temporary: Path | None = None
    linked = False
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
            os.fchown(stream.fileno(), owner_uid, owner_gid)
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        temporary.unlink()
        temporary = None
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ComfyUIInstallError("Manager import anchor must be a regular file")
        if (
            metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or path.read_bytes() != content
        ):
            raise ComfyUIInstallError("Manager import anchor verification failed")
    except FileExistsError as error:
        raise ComfyUIInstallError("Manager import anchor already exists") from error
    except ComfyUIInstallError:
        if linked:
            path.unlink(missing_ok=True)
        raise
    except OSError as error:
        if linked:
            path.unlink(missing_ok=True)
        raise ComfyUIInstallError(
            "Manager import anchor could not be written"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    environ: Mapping[str, str] | None,
) -> None:
    python_minor = ".".join(application.pytorch.python_version.split(".")[:2])
    anchor = (
        runtime.virtual_env
        / "lib"
        / f"python{python_minor}"
        / "site-packages"
        / "comfyui-docker-helper-comfyui.pth"
    )
    for path, subject in (
        (runtime.virtual_env / "bin/cm-cli", "Manager cm-cli"),
        (anchor, "Manager import anchor"),
    ):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ComfyUIInstallError(f"{subject} could not be inspected") from error
        raise ComfyUIInstallError(f"{subject} exists while Manager is disabled")
    run_argv(
        (runtime.python, "-I", "-c", _MANAGER_ABSENCE_CHECK),
        cwd=_BUILD_DIRECTORY,
        env=_isolated_install_environment(environ),
        description="disabled Manager verification",
    )


def _check_application_health(
    runtime: ContainerRuntime,
    uv_path: Path,
    environ: Mapping[str, str] | None,
) -> None:
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


def _application_install_environment(
    environ: Mapping[str, str] | None,
    constraints_path: Path,
) -> dict[str, str]:
    result = _isolated_install_environment(environ)
    result.update(
        {
            "PIP_CONSTRAINT": os.fspath(constraints_path),
            "UV_CONSTRAINT": os.fspath(constraints_path),
        }
    )
    return result


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


_MANAGER_CAPABILITY_CHECK = "; ".join(
    (
        "import importlib, importlib.metadata as m, importlib.util, pathlib, sys",
        "assert m.version('comfyui-manager') == sys.argv[1]",
        "workspace=pathlib.Path(sys.argv[2]).resolve(strict=True)",
        "assert workspace in tuple(pathlib.Path(item).resolve() "
        "for item in sys.path if item)",
        "folder_paths=importlib.util.find_spec('folder_paths')",
        "assert folder_paths is not None and folder_paths.origin is not None",
        "assert pathlib.Path(folder_paths.origin).resolve().is_relative_to(workspace)",
        "site_packages=pathlib.Path(sys.argv[3]).resolve(strict=True)",
        "manager_name=sys.argv[4]",
        "manager_root=(site_packages / pathlib.Path(*manager_name.split('.'))).resolve("
        "strict=True)",
        "assert manager_root.is_relative_to(site_packages), "
        "'Manager import root escapes application site-packages'",
        "manager_spec=importlib.util.find_spec(manager_name)",
        "assert manager_spec is not None and manager_spec.name == manager_name",
        "manager_search=manager_spec.submodule_search_locations",
        "assert manager_search is not None",
        "manager_locations=tuple(pathlib.Path(item).resolve(strict=True) "
        "for item in manager_search)",
        "assert manager_locations and all(item == manager_root "
        "for item in manager_locations), "
        "'Manager import locations escape application site-packages'",
        "manager_origin=None if manager_spec.origin is None else "
        "pathlib.Path(manager_spec.origin).resolve(strict=True)",
        "assert manager_origin is None or manager_origin.is_relative_to(manager_root), "
        "'Manager import origin escapes application root'",
        "comfy_name='comfy'",
        "comfy_root=(workspace / comfy_name).resolve(strict=True)",
        "assert comfy_root.is_relative_to(workspace), "
        "'ComfyUI comfy import root escapes checkout'",
        "comfy_spec=importlib.util.find_spec(comfy_name)",
        "assert comfy_spec is not None and comfy_spec.name == comfy_name",
        "comfy_search=comfy_spec.submodule_search_locations",
        "assert comfy_search is not None",
        "comfy_locations=tuple(pathlib.Path(item).resolve(strict=True) "
        "for item in comfy_search)",
        "assert comfy_locations and all(item == comfy_root "
        "for item in comfy_locations), "
        "'ComfyUI comfy import locations escape checkout'",
        "comfy_origin=None if comfy_spec.origin is None else "
        "pathlib.Path(comfy_spec.origin).resolve(strict=True)",
        "assert comfy_origin is None or comfy_origin.is_relative_to(comfy_root), "
        "'ComfyUI comfy import origin escapes checkout root'",
        "manager=importlib.import_module(manager_name)",
        "imported_spec=manager.__spec__",
        "assert imported_spec is not None and imported_spec.name == manager_spec.name",
        "imported_search=imported_spec.submodule_search_locations",
        "assert imported_search is not None",
        "imported_locations=tuple(pathlib.Path(item).resolve(strict=True) "
        "for item in imported_search)",
        "assert imported_locations == manager_locations",
        "imported_origin=None if imported_spec.origin is None else "
        "pathlib.Path(imported_spec.origin).resolve(strict=True)",
        "assert imported_origin == manager_origin",
        "comfy=importlib.import_module(comfy_name)",
        "imported_comfy_spec=comfy.__spec__",
        "assert imported_comfy_spec is not None and "
        "imported_comfy_spec.name == comfy_spec.name",
        "imported_comfy_search=imported_comfy_spec.submodule_search_locations",
        "assert imported_comfy_search is not None",
        "imported_comfy_locations=tuple(pathlib.Path(item).resolve(strict=True) "
        "for item in imported_comfy_search)",
        "assert imported_comfy_locations == comfy_locations",
        "imported_comfy_origin=None if imported_comfy_spec.origin is None else "
        "pathlib.Path(imported_comfy_spec.origin).resolve(strict=True)",
        "assert imported_comfy_origin == comfy_origin",
        "distribution=m.distribution('comfyui-manager')",
        "commands=[item for item in distribution.entry_points "
        "if item.group == 'console_scripts' and item.name == sys.argv[5] "
        "and item.value == sys.argv[6]]",
        "assert len(commands) == 1",
    )
)


_MANAGER_ABSENCE_CHECK = """\
import importlib.metadata as metadata
import importlib.util

try:
    metadata.version("comfyui-manager")
except metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError("Manager distribution exists while disabled")

assert importlib.util.find_spec("comfyui_manager") is None, (
    "Manager import exists while disabled"
)
"""
