"""Host render/build context preparation service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from comfyui_docker_helper.config import (
    Diagnostic,
    Lockfile,
    LockOptions,
    LockServiceError,
    LockServiceResult,
    RenderPlan,
    SourceResolvers,
    load_validate_plan_result,
    parse_lockfile_toml,
    resolve_lockfile,
)
from comfyui_docker_helper.rendering import (
    ContextWriteError,
    MaterializationError,
    has_valid_context_marker,
    materialize_expected_build_context,
    write_build_context,
)
from comfyui_docker_helper.rendering.context import ConfigInput

_ALWAYS_MANAGED_TREES = ("packages/cdh/src",)
_CONDITIONAL_MANAGED_TREES = ("scripts",)
_RETIRED_HELPER_PROJECTION_ROOTS = ("config",)


@dataclass(frozen=True, slots=True)
class PreparedContext:
    """Prepared render context data and non-fatal diagnostics."""

    plan: RenderPlan
    lock_result: LockServiceResult
    warnings: tuple[Diagnostic, ...] = ()


class HostRenderServiceError(ValueError):
    """A host render/build preparation failure."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("host render service errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("host render failed")


def prepare_render_context(
    config_files: ConfigInput,
    output_dir: str | Path,
    *,
    scripts_dir: str | Path = "./scripts",
    resolvers: SourceResolvers,
    lock_options: LockOptions | None = None,
    overwrite: bool = False,
    working_directory: str | Path | None = None,
) -> PreparedContext:
    """Validate config, resolve/check the lock, and optionally write the context."""
    options = lock_options or LockOptions()
    result = load_validate_plan_result(config_files, scripts_dir=scripts_dir)
    output_path = _resolve_effective_output_path(output_dir, working_directory)
    existing_lockfile = _load_existing_lockfile(output_path)
    try:
        lock_result = resolve_lockfile(
            result.config,
            existing_lockfile,
            resolvers,
            options,
        )
    except LockServiceError as error:
        raise HostRenderServiceError(error.diagnostics) from error

    warnings = (*result.warnings, *lock_result.warnings)
    if options.check:
        _check_managed_artifacts(
            output_path,
            result.plan,
            result.config,
            lock_result.lockfile,
        )
        return PreparedContext(
            plan=result.plan,
            lock_result=lock_result,
            warnings=warnings,
        )
    if options.dry_run:
        return PreparedContext(
            plan=result.plan,
            lock_result=lock_result,
            warnings=warnings,
        )

    try:
        write_build_context(
            result.plan,
            output_path,
            config=result.config,
            lockfile=lock_result.lockfile,
            overwrite=overwrite,
            working_directory=working_directory,
            config_file=config_files,
        )
    except (ContextWriteError, MaterializationError) as error:
        raise HostRenderServiceError(
            (
                Diagnostic(
                    path=("render",),
                    code="render.context_write_failed",
                    message=str(error),
                ),
            )
        ) from error
    return PreparedContext(plan=result.plan, lock_result=lock_result, warnings=warnings)


def _load_existing_lockfile(output_dir: Path) -> Lockfile | None:
    lock_path = output_dir / "config.lock.toml"
    if not lock_path.exists():
        return None
    if lock_path.is_symlink() or not lock_path.is_file():
        raise HostRenderServiceError(
            (
                Diagnostic(
                    path=("config.lock.toml",),
                    code="lockfile.invalid_path",
                    message="existing config.lock.toml must be a regular file",
                ),
            )
        )
    try:
        return parse_lockfile_toml(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError, ValueError) as error:
        raise HostRenderServiceError(
            (
                Diagnostic(
                    path=("config.lock.toml",),
                    code="lockfile.invalid",
                    message=f"existing config.lock.toml could not be read: {error}",
                ),
            )
        ) from error


def _resolve_effective_output_path(
    output_dir: str | Path,
    working_directory: str | Path | None,
) -> Path:
    base = Path.cwd() if working_directory is None else Path(working_directory)
    output = Path(output_dir)
    candidate = output if output.is_absolute() else base / output
    if candidate.is_symlink():
        raise HostRenderServiceError(
            (
                Diagnostic(
                    path=("render", "output"),
                    code="render.output_symlink",
                    message="render output directory must not be a symlink",
                ),
            )
        )
    return candidate.resolve(strict=False)


def _check_managed_artifacts(
    output_dir: Path,
    plan: RenderPlan,
    config,
    lockfile: Lockfile,
) -> None:
    if not output_dir.is_dir():
        raise HostRenderServiceError(
            (
                Diagnostic(
                    path=("render", "check"),
                    code="render.context_missing",
                    message="--check requires an existing rendered context directory",
                ),
            )
        )
    if not has_valid_context_marker(output_dir):
        raise HostRenderServiceError(
            (
                Diagnostic(
                    path=("render", "check"),
                    code="render.context_unmarked",
                    message="--check requires an existing cdh rendered context",
                ),
            )
        )
    try:
        with materialize_expected_build_context(
            plan,
            output_dir.parent,
            config=config,
            lockfile=lockfile,
        ) as expected:
            diagnostics = _compare_managed_artifacts(expected, output_dir)
    except (ContextWriteError, MaterializationError) as error:
        raise HostRenderServiceError(
            (
                Diagnostic(
                    path=("render", "check"),
                    code="render.check_failed",
                    message=str(error),
                ),
            )
        ) from error
    if diagnostics:
        raise HostRenderServiceError(tuple(diagnostics))


def _compare_managed_artifacts(
    expected_dir: Path,
    output_dir: Path,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for expected_path in _walk_expected_artifacts(expected_dir):
        relative = expected_path.relative_to(expected_dir)
        actual_path = output_dir / relative
        artifact = relative.as_posix()
        if expected_path.is_dir():
            if actual_path.is_symlink() or not actual_path.is_dir():
                diagnostics.append(_changed_artifact_diagnostic(artifact))
            continue
        if not expected_path.is_file():
            continue
        if (
            actual_path.is_symlink()
            or not actual_path.is_file()
            or not _files_match(expected_path, actual_path)
        ):
            diagnostics.append(_changed_artifact_diagnostic(artifact))
    diagnostics.extend(
        _actual_only_managed_artifact_diagnostics(expected_dir, output_dir)
    )
    return diagnostics


def _actual_only_managed_artifact_diagnostics(
    expected_dir: Path,
    output_dir: Path,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for root in _managed_tree_roots():
        actual_root = output_dir / root
        if not (expected_dir / root).exists():
            if actual_root.exists() or actual_root.is_symlink():
                diagnostics.append(_changed_artifact_diagnostic(root))
            continue
        if (
            not actual_root.exists()
            or actual_root.is_symlink()
            or not actual_root.is_dir()
        ):
            continue
        for actual_path in _walk_actual_artifacts(actual_root):
            relative = actual_path.relative_to(output_dir)
            if not (expected_dir / relative).exists():
                diagnostics.append(_changed_artifact_diagnostic(relative.as_posix()))

    for root in _RETIRED_HELPER_PROJECTION_ROOTS:
        actual_path = output_dir / root
        if actual_path.exists() or actual_path.is_symlink():
            diagnostics.append(_changed_artifact_diagnostic(root))
    return diagnostics


def _managed_tree_roots() -> tuple[str, ...]:
    return (*_ALWAYS_MANAGED_TREES, *_CONDITIONAL_MANAGED_TREES)


def _walk_actual_artifacts(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            root.rglob("*"),
            key=lambda path: path.as_posix(),
        )
    )


def _files_match(expected_path: Path, actual_path: Path) -> bool:
    try:
        return actual_path.read_bytes() == expected_path.read_bytes()
    except OSError:
        return False


def _walk_expected_artifacts(expected_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            expected_dir.rglob("*"),
            key=lambda path: path.relative_to(expected_dir).as_posix(),
        )
    )


def _changed_artifact_diagnostic(artifact: str) -> Diagnostic:
    return Diagnostic(
        path=tuple(artifact.split("/")),
        code="render.check_changed",
        message=f"{artifact} would be changed by render",
    )
