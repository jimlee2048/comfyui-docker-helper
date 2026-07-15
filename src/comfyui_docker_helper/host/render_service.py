"""Active canonical Planning Authority render/build-context service."""

from __future__ import annotations

import json
import shutil
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    RuntimePlanningProvenance,
    construct_build_plan,
)
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    CanonicalLockError,
    dump_canonical_lock_toml,
    parse_canonical_lock_toml,
)
from comfyui_docker_helper.config.canonical_request import (
    CanonicalRequestError,
    build_canonical_request_graph,
)
from comfyui_docker_helper.config.canonical_resolver import (
    AcceptedCanonicalLock,
    CanonicalResolutionError,
    LocalExecutableEntryAcquirer,
    LockPolicy,
    ReconcilePurpose,
    reconcile_canonical_lock,
)
from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.service import (
    ConfigurationResult,
    load_validate_config_result,
)
from comfyui_docker_helper.host.planning_authority import (
    CachingCanonicalAcquirer,
    build_local_executable_requests,
    planning_release_inputs,
    stable_comfyui_entry,
    stable_comfyui_requirements_entry,
    uv_catalog_descriptor_digest,
)
from comfyui_docker_helper.host.runtime_hook_inputs import (
    RuntimeHookInputError,
    discover_runtime_hook_inputs,
)
from comfyui_docker_helper.rendering.final_materializer import (
    FinalMaterializationError,
    LocalMaterializationSource,
    materialize_build_plan,
)

_LOCK_FILE = "config.lock.toml"
_MARKER_FILE = ".cdh-rendered"
_MARKER = {"tool": "comfyui-docker-helper", "kind": "build-context", "version": 1}


@dataclass(frozen=True, slots=True)
class PlanningOptions:
    locked: bool = False
    check: bool = False
    upgrade_lock: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.locked and self.upgrade_lock:
            raise _render_error(
                "render.options_conflict",
                "--locked and --upgrade-lock are mutually exclusive",
            )
        if self.check and (self.locked or self.upgrade_lock or self.dry_run):
            raise _render_error(
                "render.options_conflict",
                "--check cannot be combined with lock or dry-run modifiers",
            )

    @property
    def policy(self) -> LockPolicy:
        if self.locked:
            return LockPolicy.LOCKED
        if self.upgrade_lock:
            return LockPolicy.UPGRADE
        return LockPolicy.DEFAULT

    @property
    def purpose(self) -> ReconcilePurpose:
        if self.check:
            return ReconcilePurpose.CHECK
        if self.dry_run:
            return ReconcilePurpose.DRY_RUN
        return ReconcilePurpose.APPLY

    @property
    def writes(self) -> bool:
        return not (self.locked or self.check or self.dry_run)


@dataclass(frozen=True, slots=True)
class PreparedContext:
    plan: BuildPlan
    lock_result: AcceptedCanonicalLock
    warnings: tuple[Diagnostic, ...] = ()


class HostRenderServiceError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("host render failed")


def prepare_render_context(
    config_files,
    output_dir: str | Path,
    *,
    scripts_dir: str | Path = "./scripts",
    hooks_dir: str | Path | None = None,
    acquirer: CachingCanonicalAcquirer,
    local_acquirer: LocalExecutableEntryAcquirer,
    options: PlanningOptions | None = None,
    overwrite: bool = False,
    working_directory: str | Path | None = None,
    configuration_result: ConfigurationResult | None = None,
) -> PreparedContext:
    """Resolve one canonical lock, construct once, then render or compare."""
    selected = options or PlanningOptions()
    result = configuration_result or load_validate_config_result(
        config_files, scripts_dir=scripts_dir
    )
    output = _output_path(output_dir, working_directory)
    existing = _load_existing_lock(output)
    try:
        runtime_hooks = discover_runtime_hook_inputs(
            hooks_dir, working_directory=working_directory
        )
        custom_hook_root = _custom_hook_source_root(result, scripts_dir)
        _validate_input_output_separation(output, custom_hook_root, "custom hook")
        _validate_input_output_separation(
            output, runtime_hooks.source_root, "runtime hook"
        )
        uv_digest = uv_catalog_descriptor_digest(
            result.config, existing, selected.policy, acquirer
        )
        comfyui = stable_comfyui_entry(
            result.config, existing, selected.policy, acquirer
        )
        requirements = stable_comfyui_requirements_entry(
            result.config, comfyui, existing, selected.policy, acquirer
        )
        graph = build_canonical_request_graph(
            result.config,
            release=planning_release_inputs(result.config.python.version),
            uv_descriptor_digest=uv_digest,
            comfyui_entry=comfyui,
            requirements_entry=requirements,
        )
        local_requests = build_local_executable_requests(
            graph,
            scripts_dir=scripts_dir,
            runtime_hook_requests=runtime_hooks.requests,
        )
        accepted = reconcile_canonical_lock(
            graph.desired,
            local_requests=local_requests,
            local_acquirer=local_acquirer,
            existing=existing,
            acquirer=acquirer,
            policy=selected.policy,
            purpose=selected.purpose,
        )
        plan = construct_build_plan(
            graph,
            accepted.lock,
            runtime_provenance=_runtime_provenance(result),
        )
    except RuntimeHookInputError as error:
        raise HostRenderServiceError(error.diagnostics) from error
    except CanonicalResolutionError as error:
        raise HostRenderServiceError(error.diagnostics) from error
    except CanonicalRequestError as error:
        raise HostRenderServiceError(error.diagnostics) from error

    sources = tuple(
        LocalMaterializationSource(
            request.canonical_path, request.root / request.relative_path
        )
        for request in local_requests
    )
    if selected.check or (selected.locked and not selected.dry_run):
        _check_context(output, plan, accepted.lock, sources)
    elif selected.writes:
        _write_context(output, plan, accepted.lock, sources, overwrite=overwrite)
    return PreparedContext(plan, accepted, result.warnings)


def _load_existing_lock(output: Path) -> CanonicalLock | None:
    path = output / _LOCK_FILE
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise _render_error(
            "lock.invalid", "config.lock.toml is invalid; remove it and regenerate"
        )
    try:
        return parse_canonical_lock_toml(path.read_bytes())
    except (OSError, CanonicalLockError) as error:
        raise _render_error("lock.invalid", str(error)) from error


def _write_context(
    output: Path,
    plan: BuildPlan,
    lock: CanonicalLock,
    sources: tuple[LocalMaterializationSource, ...],
    *,
    overwrite: bool,
) -> None:
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _render_error("render.context_write_failed", str(error)) from error
    if output.exists() or output.is_symlink():
        if not overwrite:
            raise _render_error(
                "render.output_exists", "output exists; use --overwrite"
            )
        if output.is_symlink() or not output.is_dir() or not _valid_marker(output):
            raise _render_error(
                "render.output_invalid", "existing output is not a cdh context"
            )
    try:
        stage = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent)
        )
    except OSError as error:
        raise _render_error("render.context_write_failed", str(error)) from error
    backup_root: Path | None = None
    try:
        materialize_build_plan(plan, stage, local_sources=sources)
        (stage / _LOCK_FILE).write_text(
            dump_canonical_lock_toml(lock), encoding="utf-8"
        )
        (stage / _MARKER_FILE).write_text(
            json.dumps(_MARKER, sort_keys=True) + "\n", encoding="utf-8"
        )
        if output.exists():
            backup_root = Path(
                tempfile.mkdtemp(prefix=f".{output.name}.backup-", dir=output.parent)
            )
            previous = backup_root / "previous"
            output.rename(previous)
            try:
                stage.rename(output)
            except BaseException:
                with suppress(OSError):
                    previous.rename(output)
                raise
            with suppress(OSError):
                shutil.rmtree(backup_root)
            backup_root = None
        else:
            stage.rename(output)
    except (FinalMaterializationError, OSError) as error:
        _cleanup_owned_after_failure(stage)
        _cleanup_empty_backup_root(backup_root)
        raise _render_error("render.context_write_failed", str(error)) from error
    except BaseException:
        _cleanup_owned_after_failure(stage)
        _cleanup_empty_backup_root(backup_root)
        raise
    if stage.exists():
        try:
            shutil.rmtree(stage)
        except OSError as error:
            raise _render_error("render.context_write_failed", str(error)) from error


def _check_context(
    output: Path,
    plan: BuildPlan,
    lock: CanonicalLock,
    sources: tuple[LocalMaterializationSource, ...],
) -> None:
    if not output.is_dir() or not _valid_marker(output):
        raise _render_error(
            "render.context_missing", "--check requires a rendered context"
        )
    try:
        with tempfile.TemporaryDirectory(
            prefix=".cdh-check-", dir=output.parent
        ) as raw:
            expected = Path(raw)
            materialize_build_plan(plan, expected, local_sources=sources)
            (expected / _LOCK_FILE).write_text(
                dump_canonical_lock_toml(lock), encoding="utf-8"
            )
            (expected / _MARKER_FILE).write_text(
                json.dumps(_MARKER, sort_keys=True) + "\n", encoding="utf-8"
            )
            if _tree(expected) != _tree(output):
                raise _render_error(
                    "render.context_changed", "rendered context is out of date"
                )
    except HostRenderServiceError:
        raise
    except (FinalMaterializationError, OSError) as error:
        raise _render_error("render.context_check_failed", str(error)) from error


def _tree(root: Path) -> dict[str, tuple[str, int, bytes | None]]:
    entries: dict[str, tuple[str, int, bytes | None]] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        children = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
        for path in children:
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            permissions = stat.S_IMODE(mode)
            if stat.S_ISLNK(mode):
                entries[relative] = ("symlink", permissions, None)
            elif stat.S_ISDIR(mode):
                entries[relative] = ("directory", permissions, None)
                pending.append(path)
            elif stat.S_ISREG(mode):
                entries[relative] = ("file", permissions, path.read_bytes())
            else:
                entries[relative] = ("special", permissions, None)
    return entries


def _cleanup_owned_after_failure(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    with suppress(OSError):
        shutil.rmtree(path)


def _cleanup_empty_backup_root(path: Path | None) -> None:
    if path is None or not path.exists() or (path / "previous").exists():
        return
    with suppress(OSError):
        path.rmdir()


def _valid_marker(output: Path) -> bool:
    path = output / _MARKER_FILE
    try:
        return not path.is_symlink() and json.loads(path.read_text()) == _MARKER
    except (OSError, ValueError):
        return False


def _output_path(output: str | Path, working_directory: str | Path | None) -> Path:
    base = Path.cwd() if working_directory is None else Path(working_directory)
    candidate = Path(output)
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.is_symlink():
        raise _render_error("render.output_symlink", "output must not be a symlink")
    return candidate.absolute()


def _validate_input_output_separation(
    output: Path, source: Path | None, source_kind: str
) -> None:
    if source is None:
        return
    try:
        output_resolved = output.resolve(strict=False)
        source_resolved = source.resolve(strict=True)
    except OSError as error:
        raise _render_error(
            "render.input_output_inspect_failed",
            f"{source_kind} source and output could not be compared",
        ) from error
    if (
        output_resolved == source_resolved
        or output_resolved in source_resolved.parents
        or source_resolved in output_resolved.parents
    ):
        raise _render_error(
            "render.input_output_overlap",
            f"output and {source_kind} source must not overlap",
        )


def _custom_hook_source_root(
    result: ConfigurationResult, scripts_dir: str | Path
) -> Path | None:
    has_hooks = any(
        node.pre_install_scripts or node.post_install_scripts
        for node in result.config.comfyui.custom_nodes
    )
    if not has_hooks:
        return None
    try:
        return Path(scripts_dir).resolve(strict=True)
    except OSError as error:
        raise _render_error(
            "render.custom_hook_source_unavailable",
            "custom hook source could not be resolved",
        ) from error


def _runtime_provenance(result: ConfigurationResult) -> RuntimePlanningProvenance:
    raw_cdh = result.raw_document.get("cdh", {})
    raw_files = result.raw_document.get("files", [])
    if not isinstance(raw_cdh, dict) or not isinstance(raw_files, list):
        raise AssertionError("validated raw config has an unexpected shape")
    return RuntimePlanningProvenance(
        failure_policy_explicit="download_failure_policy" in raw_cdh,
        file_downloader_explicit=tuple(
            isinstance(item, dict) and "downloader" in item for item in raw_files
        ),
        file_download_mode_explicit=tuple(
            isinstance(item, dict) and "download_mode" in item for item in raw_files
        ),
    )


def _render_error(code: str, message: str) -> HostRenderServiceError:
    return HostRenderServiceError((Diagnostic(("render",), code, message),))
