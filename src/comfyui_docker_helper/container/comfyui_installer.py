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
from pathlib import Path

from comfyui_docker_helper.comfyui_requirements import (
    ComfyUIRequirementsError,
    parse_comfyui_requirements,
)
from comfyui_docker_helper.config.build_plan import ApplicationPhase, ToolchainPhase
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
    parsed = _verify_checkout(application, runtime)
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
    for relative in _REQUIRED_ROOT_FILES:
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
    return _verify_requirements(
        application, target / application.comfyui.requirements.path
    )


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
            env=_isolated_install_environment(environ),
            description="ComfyUI ordinary requirements install",
        )
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
            description="ComfyUI dependency verification",
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
