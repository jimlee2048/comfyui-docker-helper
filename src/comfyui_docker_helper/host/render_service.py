"""Canonical lock reconciliation and build-context rendering."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    LocalFilePlan,
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
    FileRequest,
    LocalFileRequest,
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
from comfyui_docker_helper.config.final_models import FinalLocalFileConfig
from comfyui_docker_helper.config.publication_tags import (
    PublicationTagError,
    resolve_publication_tags,
)
from comfyui_docker_helper.config.service import (
    ConfigurationResult,
)
from comfyui_docker_helper.file_admission import (
    AdmittedRegularFileReader,
    consume_regular_absolute_file,
    observe_regular_absolute_file,
    operate_regular_absolute_file,
)
from comfyui_docker_helper.host.buildx import BuildxOutput, BuildxOutputPlan
from comfyui_docker_helper.host.canonical_acquisition import (
    LocalFileEntryAcquirer as FilesystemLocalFileEntryAcquirer,
)
from comfyui_docker_helper.host.hook_paths import (
    lexical_hook_source_root,
    observed_path_is_real_directory,
    observed_path_is_reparse,
)
from comfyui_docker_helper.host.planning_authority import (
    CachingCanonicalAcquirer,
    build_local_executable_requests,
    planning_release_inputs,
    stable_comfyui_entry,
    stable_comfyui_requirements_entry,
    uv_catalog_descriptor_digest,
)
from comfyui_docker_helper.host.private_state import create_private_directory
from comfyui_docker_helper.host.runtime_hook_inputs import (
    RuntimeHookInputError,
    discover_runtime_hook_inputs,
)
from comfyui_docker_helper.local_file_identity import LocalFileIdentityRequest
from comfyui_docker_helper.release_artifacts import CanonicalWheel
from comfyui_docker_helper.rendering.final_materializer import (
    FinalMaterializationError,
    LocalMaterializationSource,
    _materialize_private_stage,
)

_LOCK_FILE = "config.lock.toml"
_MARKER_FILE = ".cdh-rendered"
_MARKER = {"tool": "comfyui-docker-helper", "kind": "build-context", "version": 1}
_STAGE_PREFIX = "cdh-render-stage-"
_BACKUP_PREFIX = "cdh-render-backup-"
_CHECK_PREFIX = "cdh-render-check-"
_LOCAL_FILE_COMPARE_BLOCK_BYTES = 1024 * 1024
_platform_name = os.name


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
    output_plan: BuildxOutputPlan | None = None
    warnings: tuple[Diagnostic, ...] = ()


class HostRenderServiceError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("host render failed")


def prepare_render_context(
    output_dir: str | Path,
    *,
    configuration_result: ConfigurationResult,
    build_hook_source_root: Path | None,
    runtime_hooks_dir: str | Path | None = None,
    acquirer: CachingCanonicalAcquirer,
    local_acquirer: LocalExecutableEntryAcquirer,
    canonical_wheel: CanonicalWheel,
    tag_templates: Sequence[str] = (),
    output_mode: BuildxOutput = "load",
    options: PlanningOptions | None = None,
    overwrite: bool = False,
    working_directory: str | Path | None = None,
) -> PreparedContext:
    """Resolve one canonical lock, construct once, then render or compare."""
    selected = options or PlanningOptions()
    result = configuration_result
    output = _output_path(output_dir, working_directory)
    existing = _load_existing_lock(output)
    try:
        runtime_hooks = discover_runtime_hook_inputs(
            runtime_hooks_dir, working_directory=working_directory
        )
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
            comfyui, existing, selected.policy, acquirer
        )
        graph = build_canonical_request_graph(
            result.config,
            domains=result.domains,
            release=planning_release_inputs(canonical_wheel),
            uv_descriptor_digest=uv_digest,
            comfyui_entry=comfyui,
            requirements_entry=requirements,
        )
        local_requests = build_local_executable_requests(
            graph,
            build_hooks_dir=build_hook_source_root,
            runtime_hook_requests=runtime_hooks.requests,
        )
        local_file_sources, local_file_requests = _local_file_inputs(
            result, graph.files, output
        )
        accepted = reconcile_canonical_lock(
            graph.desired,
            local_requests=local_requests,
            local_acquirer=local_acquirer,
            local_file_requests=local_file_requests,
            local_file_acquirer=FilesystemLocalFileEntryAcquirer(),
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
        output_plan = _resolve_buildx_output_plan(
            tag_templates,
            output=output_mode,
            plan=plan,
        )
    except RuntimeHookInputError as error:
        raise HostRenderServiceError(error.diagnostics) from error
    except CanonicalResolutionError as error:
        raise HostRenderServiceError(error.diagnostics) from error
    except CanonicalRequestError as error:
        raise HostRenderServiceError(error.diagnostics) from error
    except PublicationTagError as error:
        raise HostRenderServiceError(
            tuple(
                Diagnostic(("build", "tags", issue.index), issue.code, issue.message)
                for issue in error.issues
            )
        ) from error

    sources = (
        tuple(
            LocalMaterializationSource(
                request.canonical_path, request.root / request.relative_path
            )
            for request in local_requests
        )
        + local_file_sources
    )
    if selected.check or (selected.locked and not selected.dry_run):
        _check_context(
            output,
            plan,
            accepted.lock,
            canonical_wheel,
            sources,
            local_file_mode=result.config.cdh.local_file_mode,
            check_unlocked_sources=selected.check,
        )
    elif selected.writes:
        _write_context(
            output,
            plan,
            accepted.lock,
            canonical_wheel,
            sources,
            local_file_mode=result.config.cdh.local_file_mode,
            overwrite=overwrite,
        )
    return PreparedContext(
        plan=plan,
        lock_result=accepted,
        output_plan=output_plan,
        warnings=runtime_hooks.warnings,
    )


def _local_file_inputs(
    result: ConfigurationResult,
    graph_files: tuple[FileRequest, ...],
    output: Path,
) -> tuple[
    tuple[LocalMaterializationSource, ...],
    tuple[LocalFileIdentityRequest, ...],
]:
    sources: list[LocalMaterializationSource] = []
    requests: list[LocalFileIdentityRequest] = []
    for item, normalized, request in zip(
        result.config.files, result.domains.files, graph_files, strict=True
    ):
        if not isinstance(item, FinalLocalFileConfig):
            continue
        if not isinstance(request, LocalFileRequest):
            raise AssertionError("local file request projection is inconsistent")
        locator = Path(item.path)
        source = locator if locator.is_absolute() else result.secret_file_base / locator
        source = Path(os.path.abspath(source))
        _validate_input_output_separation(output, source, "local file")
        if not item.content_lock:
            try:
                observe_regular_absolute_file(source)
            except (OSError, ValueError) as error:
                raise _render_error(
                    "render.local_file_source_unavailable",
                    "local file source must be a readable regular file without links",
                ) from error
        sources.append(
            LocalMaterializationSource(PurePosixPath(request.context_path), source)
        )
        if item.content_lock:
            requests.append(
                LocalFileIdentityRequest(
                    source_path=source,
                    relative_target=PurePosixPath(normalized.relative_target),
                )
            )
    return tuple(sources), tuple(requests)


def _resolve_buildx_output_plan(
    tag_templates: Sequence[str],
    *,
    output: BuildxOutput,
    plan: BuildPlan,
) -> BuildxOutputPlan | None:
    if not tag_templates:
        return None
    comfyui = plan.application.comfyui
    return BuildxOutputPlan(
        tags=resolve_publication_tags(
            tag_templates,
            commit=comfyui.commit,
            formal_release=comfyui.formal_release,
        ),
        output=output,
    )


def admit_build_hook_source(
    result: ConfigurationResult,
    build_hooks_dir: str | Path | None,
    output_dir: str | Path,
    *,
    working_directory: str | Path | None = None,
) -> Path | None:
    """Admit the referenced build-hook root before provider initialization."""
    root = _build_hook_source_root(
        result, build_hooks_dir, working_directory=working_directory
    )
    _validate_input_output_separation(
        _output_path(output_dir, working_directory), root, "build hook"
    )
    return root


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
    canonical_wheel: CanonicalWheel,
    sources: tuple[LocalMaterializationSource, ...],
    *,
    local_file_mode: str,
    overwrite: bool,
) -> None:
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as error:
        raise _render_error("render.context_write_failed", str(error)) from error
    if output.exists() or output.is_symlink():
        if not overwrite:
            raise _render_error(
                "render.output_exists", "output exists; use --overwrite"
            )
        if not _is_real_directory(output) or not _valid_marker(output):
            raise _render_error(
                "render.output_invalid", "existing output is not a cdh context"
            )
    try:
        stage = _create_private_render_directory(output.parent, prefix=_STAGE_PREFIX)
    except OSError as error:
        raise _render_error("render.context_write_failed", str(error)) from error
    backup_root: Path | None = None
    try:
        _materialize_private_stage(
            plan,
            stage,
            canonical_wheel=canonical_wheel,
            local_sources=sources,
            local_file_mode=local_file_mode,
        )
        _write_private_stage_metadata(
            stage, _LOCK_FILE, dump_canonical_lock_toml(lock).encode("utf-8")
        )
        _write_private_stage_metadata(
            stage,
            _MARKER_FILE,
            (json.dumps(_MARKER, sort_keys=True) + "\n").encode("utf-8"),
        )
        if output.exists():
            backup_root = _create_private_render_directory(
                output.parent, prefix=_BACKUP_PREFIX
            )
            previous = backup_root / "previous"
            output.rename(previous)
            try:
                stage.rename(output)
            except OSError as publication_error:
                try:
                    previous.rename(output)
                except OSError as restore_error:
                    raise _render_error(
                        "render.context_restore_failed",
                        "context replacement failed: "
                        f"{publication_error}; old context could not be restored: "
                        f"{restore_error}; old context retained at {previous}",
                    ) from restore_error
                raise
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


def _check_context(
    output: Path,
    plan: BuildPlan,
    lock: CanonicalLock,
    canonical_wheel: CanonicalWheel,
    sources: tuple[LocalMaterializationSource, ...],
    *,
    local_file_mode: str,
    check_unlocked_sources: bool,
) -> None:
    if not _is_real_directory(output) or not _valid_marker(output):
        raise _render_error(
            "render.context_missing", "--check requires a rendered context"
        )
    try:
        expected = _create_private_render_directory(output.parent, prefix=_CHECK_PREFIX)
        try:
            _materialize_private_stage(
                plan,
                expected,
                canonical_wheel=canonical_wheel,
                local_sources=sources,
                local_file_mode=local_file_mode,
                check_placeholders=True,
            )
            _write_private_stage_metadata(
                expected, _LOCK_FILE, dump_canonical_lock_toml(lock).encode("utf-8")
            )
            _write_private_stage_metadata(
                expected,
                _MARKER_FILE,
                (json.dumps(_MARKER, sort_keys=True) + "\n").encode("utf-8"),
            )
            local_paths = {
                item.context_path
                for item in plan.files.files
                if isinstance(item, LocalFilePlan)
            }
            if _tree(expected, content_excluded=local_paths) != _tree(
                output, content_excluded=local_paths
            ):
                raise _render_error(
                    "render.context_changed", "rendered context is out of date"
                )
            _check_local_context_files(
                plan,
                output,
                sources,
                check_unlocked_sources=check_unlocked_sources,
            )
        finally:
            shutil.rmtree(expected)
    except HostRenderServiceError:
        raise
    except (FinalMaterializationError, OSError) as error:
        raise _render_error("render.context_check_failed", str(error)) from error


def _create_private_render_directory(parent: Path, *, prefix: str) -> Path:
    try:
        return create_private_directory(prefix=prefix, parent=parent)
    except ValueError as error:
        raise OSError(str(error)) from error


def _write_private_stage_metadata(stage: Path, name: str, content: bytes) -> None:
    with (stage / name).open("xb") as output:
        output.write(content)
        if _platform_name == "posix":
            os.fchmod(output.fileno(), 0o644)


def _check_local_context_files(
    plan: BuildPlan,
    output: Path,
    sources: tuple[LocalMaterializationSource, ...],
    *,
    check_unlocked_sources: bool,
) -> None:
    by_identity = {item.relative_path.as_posix(): item for item in sources}
    for item in plan.files.files:
        if not isinstance(item, LocalFilePlan):
            continue
        source = by_identity[item.context_path].source_path
        context_file = Path(os.path.abspath(output / item.context_path))
        try:
            if item.digest is not None:
                digest = hashlib.sha256()
                consume_regular_absolute_file(context_file, digest.update)
                matches = f"sha256:{digest.hexdigest()}" == item.digest
            elif check_unlocked_sources:
                matches = _regular_files_equal(source, context_file)
            else:
                matches = True
        except (OSError, ValueError) as error:
            raise FinalMaterializationError(
                "local context file could not be checked"
            ) from error
        if not matches:
            raise _render_error(
                "render.context_changed", "rendered context is out of date"
            )


def _regular_files_equal(source: Path, context_file: Path) -> bool:
    def read_block(reader: AdmittedRegularFileReader) -> bytearray:
        block = bytearray()
        while len(block) < _LOCAL_FILE_COMPARE_BLOCK_BYTES:
            chunk = reader.read_chunk(_LOCAL_FILE_COMPARE_BLOCK_BYTES - len(block))
            if not chunk:
                break
            block.extend(chunk)
        return block

    def compare_source(source_reader: AdmittedRegularFileReader) -> bool:
        def compare_context(context_reader: AdmittedRegularFileReader) -> bool:
            if source_reader.size != context_reader.size:
                return False
            while True:
                source_block = read_block(source_reader)
                context_block = read_block(context_reader)
                if source_block != context_block:
                    return False
                if not source_block:
                    return True

        return operate_regular_absolute_file(context_file, compare_context)

    return operate_regular_absolute_file(source, compare_source)


def _tree(
    root: Path,
    *,
    content_excluded: set[str] | None = None,
) -> dict[str, tuple[str, int | None, bytes | None]]:
    excluded = set() if content_excluded is None else content_excluded
    entries: dict[str, tuple[str, int | None, bytes | None]] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        children = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
        for path in children:
            relative = path.relative_to(root).as_posix()
            observed = path.lstat()
            mode = observed.st_mode
            permissions = stat.S_IMODE(mode) if _platform_name == "posix" else None
            if observed_path_is_reparse(observed):
                entries[relative] = ("symlink", permissions, None)
            elif stat.S_ISDIR(mode):
                entries[relative] = ("directory", permissions, None)
                pending.append(path)
            elif stat.S_ISREG(mode):
                content = None if relative in excluded else path.read_bytes()
                entries[relative] = ("file", permissions, content)
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


def _is_real_directory(path: Path) -> bool:
    try:
        observed = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(observed.st_mode) and not observed_path_is_reparse(observed)


def _output_path(output: str | Path, working_directory: str | Path | None) -> Path:
    base = Path.cwd() if working_directory is None else Path(working_directory)
    candidate = Path(output)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        observed = candidate.lstat()
    except (FileNotFoundError, NotADirectoryError):
        observed = None
    except OSError as error:
        raise _render_error(
            "render.output_inspect_failed", "output could not be inspected"
        ) from error
    if observed is not None and observed_path_is_reparse(observed):
        raise _render_error(
            "render.output_symlink", "output must not be a link or reparse point"
        )
    return candidate.absolute()


def _validate_input_output_separation(
    output: Path, source: Path | None, source_kind: str
) -> None:
    if source is None:
        return
    # Resolution here is only an overlap comparison. The lexical source path is
    # retained for later admission so links are not hidden from that boundary.
    try:
        output_resolved = output.resolve(strict=False)
        source_resolved = source.resolve(strict=True)
    except (OSError, ValueError) as error:
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


def _build_hook_source_root(
    result: ConfigurationResult,
    build_hooks_dir: str | Path | None,
    *,
    working_directory: str | Path | None,
) -> Path | None:
    has_hooks = any(
        node.pre_install_hooks or node.post_install_hooks
        for node in result.config.comfyui.custom_nodes
    )
    if not has_hooks:
        return None
    if build_hooks_dir is None:
        raise _render_error(
            "hook.build_hooks_dir_required",
            "--build-hooks-dir is required when build hooks are configured",
        )
    try:
        root = lexical_hook_source_root(
            build_hooks_dir, working_directory=working_directory
        )
        source_is_real_directory = observed_path_is_real_directory(root)
    except (OSError, ValueError) as error:
        raise _render_error(
            "render.build_hook_source_unavailable",
            "build hook source could not be inspected",
        ) from error
    if not source_is_real_directory:
        raise _render_error(
            "render.build_hook_source_unavailable",
            "build hook source must be an existing real directory",
        )
    return root


def _runtime_provenance(result: ConfigurationResult) -> RuntimePlanningProvenance:
    raw_cdh = result.raw_document.get("cdh", {})
    raw_files = result.raw_document.get("files", [])
    if not isinstance(raw_cdh, dict) or not isinstance(raw_files, list):
        raise AssertionError("validated raw config has an unexpected shape")
    return RuntimePlanningProvenance(
        failure_policy_explicit="download_failure_policy" in raw_cdh,
        file_downloader_explicit=tuple(
            "downloader" in item
            for item in raw_files
            if isinstance(item, dict) and item.get("type") == "http"
        ),
        file_download_mode_explicit=tuple(
            "download_mode" in item
            for item in raw_files
            if isinstance(item, dict) and item.get("type") == "http"
        ),
    )


def _render_error(code: str, message: str) -> HostRenderServiceError:
    return HostRenderServiceError((Diagnostic(("render",), code, message),))
