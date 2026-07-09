"""Host render/build context preparation service."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from comfyui_docker_helper.config import (
    ConfigurationResult,
    Diagnostic,
    Lockfile,
    LockOptions,
    LockServiceError,
    LockServiceResult,
    RenderPlan,
    RuntimeHooksPlan,
    SourceResolvers,
    load_validate_plan_result,
    parse_lockfile_toml,
    resolve_lockfile,
    with_runtime_hooks_plan,
)
from comfyui_docker_helper.config.runtime_hooks import (
    RUNTIME_HOOK_PHASE_DIRECTORY_NAMES,
    RUNTIME_HOOK_SUPPORTED_SUFFIXES,
    runtime_hook_phase_directory_list,
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
_CONDITIONAL_MANAGED_TREES = ("scripts", "runtime/hooks")
_RETIRED_HELPER_PROJECTION_ROOTS = ("config",)
_DEFAULT_RUNTIME_HOOKS_DIR = Path("./hooks")


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
    hooks_dir: str | Path | None = None,
    resolvers: SourceResolvers,
    lock_options: LockOptions | None = None,
    overwrite: bool = False,
    working_directory: str | Path | None = None,
    configuration_result: ConfigurationResult | None = None,
) -> PreparedContext:
    """Validate config, resolve/check the lock, and optionally write the context."""
    options = lock_options or LockOptions()
    result = configuration_result or load_validate_plan_result(
        config_files,
        scripts_dir=scripts_dir,
    )
    runtime_hooks = _resolve_runtime_hooks_plan(
        hooks_dir,
        working_directory=working_directory,
    )
    plan = with_runtime_hooks_plan(result.plan, runtime_hooks)
    output_path = _resolve_effective_output_path(output_dir, working_directory)
    _validate_output_source_relationships(
        output_path,
        scripts_source=plan.custom_nodes.scripts_source_dir,
        runtime_hooks_source=plan.runtime_hooks.source_dir,
    )
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
            plan,
            result.config,
            lock_result.lockfile,
            result.runtime_config,
        )
        return PreparedContext(
            plan=plan,
            lock_result=lock_result,
            warnings=warnings,
        )
    if options.dry_run:
        return PreparedContext(
            plan=plan,
            lock_result=lock_result,
            warnings=warnings,
        )

    try:
        write_build_context(
            plan,
            output_path,
            config=result.config,
            lockfile=lock_result.lockfile,
            runtime_config=result.runtime_config,
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
    return PreparedContext(plan=plan, lock_result=lock_result, warnings=warnings)


def _validate_output_source_relationships(
    output_path: Path,
    *,
    scripts_source: Path | None,
    runtime_hooks_source: Path | None,
) -> None:
    for label, source in (
        ("scripts source", scripts_source),
        ("runtime hooks source", runtime_hooks_source),
    ):
        if source is None:
            continue
        message = _output_source_relationship_error(output_path, source, label)
        if message is not None:
            raise HostRenderServiceError(
                (
                    Diagnostic(
                        path=("render",),
                        code="render.context_write_failed",
                        message=message,
                    ),
                )
            )


def _output_source_relationship_error(
    output_path: Path,
    source: Path,
    label: str,
) -> str | None:
    output = output_path.resolve(strict=False)
    protected = source.resolve(strict=False)
    if _is_equal_or_ancestor(output, protected):
        return f"output directory must not be equal to or an ancestor of {label}"
    if protected in output.parents:
        return f"output directory must not be nested inside the {label}"
    return None


def _is_equal_or_ancestor(candidate: Path, target: Path) -> bool:
    return candidate == target or candidate in target.parents


def _resolve_runtime_hooks_plan(
    hooks_dir: str | Path | None,
    *,
    working_directory: str | Path | None,
) -> RuntimeHooksPlan:
    base = Path.cwd() if working_directory is None else Path(working_directory)
    explicit = hooks_dir is not None
    source = Path(hooks_dir) if explicit else _DEFAULT_RUNTIME_HOOKS_DIR
    candidate = source if source.is_absolute() else base / source

    if not explicit:
        try:
            candidate.lstat()
        except FileNotFoundError:
            return RuntimeHooksPlan(has_hooks=False, source_dir=None)
        except OSError as error:
            raise HostRenderServiceError(
                (
                    Diagnostic(
                        path=("hooks_dir",),
                        code="runtime_hooks.source_inspect_failed",
                        message=(
                            f"runtime hook source could not be inspected: {error}"
                        ),
                    ),
                )
            ) from error

    diagnostics = _validate_runtime_hooks_source(candidate)
    if diagnostics:
        raise HostRenderServiceError(tuple(diagnostics))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise HostRenderServiceError(
            (
                Diagnostic(
                    path=("hooks_dir",),
                    code="runtime_hooks.source_resolve_failed",
                    message=f"runtime hook source could not be resolved: {error}",
                ),
            )
        ) from error
    return RuntimeHooksPlan(has_hooks=True, source_dir=resolved)


def _validate_runtime_hooks_source(source: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    try:
        source_mode = source.lstat().st_mode
    except FileNotFoundError:
        diagnostics.append(
            Diagnostic(
                path=("hooks_dir",),
                code="runtime_hooks.source_not_directory",
                message="runtime hook source must be an existing real directory",
            )
        )
        return diagnostics
    except OSError as error:
        diagnostics.append(
            Diagnostic(
                path=("hooks_dir",),
                code="runtime_hooks.source_inspect_failed",
                message=f"runtime hook source could not be inspected: {error}",
            )
        )
        return diagnostics
    if stat.S_ISLNK(source_mode) or not stat.S_ISDIR(source_mode):
        diagnostics.append(
            Diagnostic(
                path=("hooks_dir",),
                code="runtime_hooks.source_not_directory",
                message="runtime hook source must be an existing real directory",
            )
        )
        return diagnostics

    try:
        children = tuple(sorted(source.iterdir(), key=lambda item: item.name))
    except OSError as error:
        diagnostics.append(
            Diagnostic(
                path=("hooks_dir",),
                code="runtime_hooks.source_read_failed",
                message=f"runtime hook source could not be read: {error}",
            )
        )
        return diagnostics

    for child in children:
        child_path = ("hooks_dir", child.name)
        child_mode = _runtime_hook_path_mode(child, child_path, diagnostics)
        if child_mode is None:
            continue
        if stat.S_ISLNK(child_mode):
            diagnostics.append(
                Diagnostic(
                    path=child_path,
                    code="runtime_hooks.symlink",
                    message="runtime hook source must not contain symlinks",
                )
            )
            continue
        if not stat.S_ISDIR(child_mode) and not stat.S_ISREG(child_mode):
            diagnostics.append(
                Diagnostic(
                    path=child_path,
                    code="runtime_hooks.special_file",
                    message="runtime hook source must not contain special files",
                )
            )
            continue
        if child.name not in RUNTIME_HOOK_PHASE_DIRECTORY_NAMES:
            diagnostics.append(
                Diagnostic(
                    path=child_path,
                    code="runtime_hooks.unknown_top_level",
                    message=(
                        "runtime hook source may only contain "
                        f"{runtime_hook_phase_directory_list()} directories"
                    ),
                )
            )
            continue
        if not stat.S_ISDIR(child_mode):
            diagnostics.append(
                Diagnostic(
                    path=child_path,
                    code="runtime_hooks.phase_not_directory",
                    message="runtime hook phase entries must be directories",
                )
            )
            continue
        _validate_runtime_hook_phase(child, child_path, diagnostics)
    return diagnostics


def _validate_runtime_hook_phase(
    phase: Path,
    path: tuple[str, ...],
    diagnostics: list[Diagnostic],
) -> None:
    try:
        children = tuple(sorted(phase.iterdir(), key=lambda item: item.name))
    except OSError as error:
        diagnostics.append(
            Diagnostic(
                path=path,
                code="runtime_hooks.phase_read_failed",
                message=f"runtime hook phase directory could not be read: {error}",
            )
        )
        return
    for child in children:
        child_path = (*path, child.name)
        child_mode = _runtime_hook_path_mode(child, child_path, diagnostics)
        if child_mode is None:
            continue
        if stat.S_ISLNK(child_mode):
            diagnostics.append(
                Diagnostic(
                    path=child_path,
                    code="runtime_hooks.symlink",
                    message="runtime hook source must not contain symlinks",
                )
            )
            continue
        if stat.S_ISDIR(child_mode):
            diagnostics.append(
                Diagnostic(
                    path=child_path,
                    code="runtime_hooks.entry_not_file",
                    message="runtime hook phase entries must be regular files",
                )
            )
            continue
        if not stat.S_ISREG(child_mode):
            diagnostics.append(
                Diagnostic(
                    path=child_path,
                    code="runtime_hooks.special_file",
                    message="runtime hook source must not contain special files",
                )
            )
            continue
        if child.suffix not in RUNTIME_HOOK_SUPPORTED_SUFFIXES:
            diagnostics.append(
                Diagnostic(
                    path=child_path,
                    code="runtime_hooks.unsupported_extension",
                    message="runtime hook files must end in .sh or .py",
                )
            )


def _runtime_hook_path_mode(
    path: Path,
    diagnostic_path: tuple[str, ...],
    diagnostics: list[Diagnostic],
) -> int | None:
    try:
        return path.lstat().st_mode
    except OSError as error:
        diagnostics.append(
            Diagnostic(
                path=diagnostic_path,
                code="runtime_hooks.inspect_failed",
                message=f"runtime hook source entry could not be inspected: {error}",
            )
        )
        return None


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
    runtime_config,
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
            runtime_config=runtime_config,
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
