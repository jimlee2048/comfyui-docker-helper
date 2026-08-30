"""Materialize BuildPlan artifacts and verified local inputs."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import tomli_w

from comfyui_docker_helper.config.planning.build_plan import (
    BuildPlan,
    HookPlan,
    HttpFilePlan,
    LocalFilePlan,
    LocalTreePlan,
    build_plan_hook_identities,
    dump_build_plan_json,
)
from comfyui_docker_helper.config.validation.hooks import (
    BUILD_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_LOCK_PREFIX,
)
from comfyui_docker_helper.filesystem.admission import (
    AdmittedRegularFileReader,
    FileCloneUnavailableError,
    LocalTreeInventory,
    LocalTreeMember,
    TreeAdmissionError,
    local_tree_mode,
    operate_regular_absolute_file,
    read_regular_absolute_file,
    revalidate_local_tree,
)
from comfyui_docker_helper.release_artifacts import (
    WORKSPACE_PROFILE_CONTEXT_PATH,
    WORKSPACE_PROFILE_WHEEL_MEMBER,
    CanonicalWheel,
)
from comfyui_docker_helper.rendering.final_renderer import (
    render_build_plan_dockerfile,
)

_platform_name = os.name
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class FinalMaterializationError(RuntimeError):
    """The BuildPlan context could not be materialized safely."""


type LocalMaterializationKind = Literal["file", "tree"]


@dataclass(frozen=True, slots=True)
class LocalMaterializationSource:
    """Host-only source path paired with one plan-owned relative identity."""

    relative_path: PurePosixPath
    source_path: Path
    kind: LocalMaterializationKind

    def __post_init__(self) -> None:
        if self.kind not in {"file", "tree"}:
            raise ValueError("local materialization source kind is invalid")


def _materialize_private_stage(
    plan: BuildPlan,
    directory: str | Path,
    *,
    canonical_wheel: CanonicalWheel,
    local_sources: tuple[LocalMaterializationSource, ...] = (),
    local_file_mode: str = "copy",
    check_placeholders: bool = False,
) -> None:
    """Populate one existing real empty private stage owned by HostRenderService."""
    stage = Path(directory)
    try:
        stage_mode = stage.lstat().st_mode
    except OSError as error:
        raise FinalMaterializationError(
            "materialization stage could not be inspected"
        ) from error
    if not stat.S_ISDIR(stage_mode):
        raise FinalMaterializationError(
            "materialization stage must be a real directory"
        )
    try:
        first_entry = next(stage.iterdir(), None)
    except OSError as error:
        raise FinalMaterializationError(
            "materialization stage contents could not be inspected"
        ) from error
    if first_entry is not None:
        raise FinalMaterializationError("materialization stage must be empty")

    build_hooks, runtime_hooks = _expected_hooks(plan)
    expected_hooks = {**build_hooks, **runtime_hooks}
    local_files = {
        item.context_path: item
        for item in plan.files.files
        if isinstance(item, LocalFilePlan)
    }
    local_trees = {
        item.context_path: item
        for item in plan.files.files
        if isinstance(item, LocalTreePlan)
    }
    expected_sources = {(relative_path, "file") for relative_path in expected_hooks}
    expected_sources.update((relative_path, "file") for relative_path in local_files)
    expected_sources.update((relative_path, "tree") for relative_path in local_trees)
    sources = {
        (item.relative_path.as_posix(), item.kind): item for item in local_sources
    }
    if len(sources) != len(local_sources) or set(sources) != expected_sources:
        raise FinalMaterializationError(
            "local materialization sources must exactly match plan inputs"
        )
    _write(
        stage,
        PurePosixPath(".dockerignore"),
        b"/.cdh-rendered\n/config.lock.toml\n",
    )
    _write(stage, PurePosixPath("build-plan.json"), dump_build_plan_json(plan))
    for relative_path, hook in expected_hooks.items():
        source = sources[(relative_path, "file")].source_path
        content = _verified_source(source, hook.digest)
        if relative_path in runtime_hooks:
            runtime_relative = PurePosixPath(
                relative_path.removeprefix(f"{RUNTIME_HOOK_LOCK_PREFIX}/")
            )
            output = PurePosixPath("runtime/hooks") / runtime_relative
        else:
            build_relative = PurePosixPath(
                relative_path.removeprefix(f"{BUILD_HOOK_LOCK_PREFIX}/")
            )
            output = PurePosixPath("build/hooks") / build_relative
        _write(stage, output, content, executable=True)
    for item in plan.files.files:
        if isinstance(item, LocalFilePlan):
            relative_path = item.context_path
            output = PurePosixPath(relative_path)
            source = sources[(relative_path, "file")].source_path
            if check_placeholders:
                _write(stage, output, b"")
            else:
                _materialize_local_file(
                    stage,
                    output,
                    source,
                    item,
                    mode=local_file_mode,
                )
        elif isinstance(item, LocalTreePlan):
            relative_path = item.context_path
            _materialize_local_tree(
                stage,
                PurePosixPath(relative_path),
                sources[(relative_path, "tree")].source_path,
                item,
                mode=local_file_mode,
                check_placeholders=check_placeholders,
            )
    _write(
        stage,
        PurePosixPath("runtime/config.toml"),
        _runtime_config_bytes(plan),
    )
    _materialize_canonical_wheel(plan, canonical_wheel, stage)
    _write(
        stage,
        WORKSPACE_PROFILE_CONTEXT_PATH,
        _workspace_profile_from_canonical_wheel(canonical_wheel),
    )
    _write(
        stage,
        PurePosixPath("Dockerfile"),
        render_build_plan_dockerfile(plan).encode("utf-8"),
    )


def _materialize_canonical_wheel(
    plan: BuildPlan,
    wheel: CanonicalWheel,
    stage: Path,
) -> None:
    cdh = plan.toolchain.tool_store.cdh
    expected_filename = f"comfyui_docker_helper-{cdh.version}-py3-none-any.whl"
    observed_digest = f"sha256:{hashlib.sha256(wheel.content).hexdigest()}"
    if (
        wheel.filename != expected_filename
        or wheel.version != cdh.version
        or wheel.digest != cdh.wheel_digest
        or observed_digest != wheel.digest
    ):
        raise FinalMaterializationError("canonical cdh wheel does not match BuildPlan")
    _write(stage, PurePosixPath("bootstrap") / wheel.filename, wheel.content)


def _workspace_profile_from_canonical_wheel(wheel: CanonicalWheel) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(wheel.content)) as archive:
            return archive.read(WORKSPACE_PROFILE_WHEEL_MEMBER.as_posix())
    except (KeyError, zipfile.BadZipFile) as error:
        raise FinalMaterializationError(
            "canonical cdh wheel workspace profile is invalid"
        ) from error


def _expected_hooks(
    plan: BuildPlan,
) -> tuple[dict[str, HookPlan], dict[str, HookPlan]]:
    try:
        return build_plan_hook_identities(plan.custom_nodes, plan.runtime)
    except ValueError as error:
        raise FinalMaterializationError("hook identity is invalid") from error


def _runtime_config_bytes(plan: BuildPlan) -> bytes:
    command = plan.runtime.launch_command
    expected_launch_head = (
        str(PurePosixPath(plan.application.paths.venv) / "bin" / "python"),
        str(PurePosixPath(plan.application.paths.comfyui) / "main.py"),
    )
    if (
        len(command) < 7
        or command[:2] != expected_launch_head
        or command[2] != "--listen"
        or command[4] != "--port"
        or command[6] != "--disable-auto-launch"
    ):
        raise FinalMaterializationError("runtime launch command is invalid")
    try:
        port = int(command[5])
    except ValueError as error:
        raise FinalMaterializationError("runtime launch port is invalid") from error
    cdh = {
        "default_downloader": plan.files.downloader.default,
        "default_download_mode": plan.files.default_download_mode,
        "download_max_attempts": plan.files.download_max_attempts,
        "shutdown_timeout": plan.runtime.shutdown_timeout,
        "downloader": plan.files.downloader.model_dump(
            mode="json", exclude={"default"}
        ),
    }
    if plan.runtime.download_failure_policy is not None:
        cdh["download_failure_policy"] = plan.runtime.download_failure_policy
    comfyui_root = PurePosixPath(command[1]).parent
    files = []
    for item in plan.files.files:
        if not isinstance(item, HttpFilePlan):
            continue
        target = PurePosixPath(item.target)
        try:
            relative = target.relative_to(comfyui_root)
        except ValueError as error:
            raise FinalMaterializationError(
                "runtime file target is outside ComfyUI"
            ) from error
        runtime_item = {
            "type": "http",
            "source": item.url,
            "target": relative.as_posix(),
        }
        if item.checksum is not None:
            runtime_item["checksum"] = item.checksum
        if item.downloader_explicit:
            runtime_item["downloader"] = item.downloader
        if item.download_mode_explicit:
            runtime_item["download_mode"] = item.download_mode
        files.append(runtime_item)
    document = {
        "comfyui": {
            "listen": command[3],
            "port": port,
            "extra_args": list(command[7:]),
        },
        "cdh": cdh,
        "system": {"ssh": plan.runtime.ssh.model_dump(mode="json")},
    }
    if files:
        document["files"] = files
    return tomli_w.dumps(document).encode("utf-8")


def _verified_source(path: Path, expected_digest: str) -> bytes:
    try:
        content = read_regular_absolute_file(path)
    except (OSError, ValueError) as error:
        raise FinalMaterializationError(
            "local source must be a readable regular file without symlinks"
        ) from error
    observed = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if observed != expected_digest:
        raise FinalMaterializationError("local source digest does not match BuildPlan")
    return content


def _materialize_local_file(
    stage: Path,
    relative_path: PurePosixPath,
    source: Path,
    plan: LocalFilePlan,
    *,
    mode: str,
) -> None:
    if mode not in {"auto", "clone", "copy"}:
        raise FinalMaterializationError("local file materialization mode is invalid")
    _materialize_regular_file(
        stage,
        relative_path,
        source,
        expected_digest=plan.digest,
        mode=mode,
    )


def _materialize_local_tree(
    stage: Path,
    relative_path: PurePosixPath,
    source: Path,
    plan: LocalTreePlan,
    *,
    mode: str,
    check_placeholders: bool,
) -> None:
    """Materialize one complete source tree below its deterministic context slot."""
    if mode not in {"auto", "clone", "copy"}:
        raise FinalMaterializationError("local file materialization mode is invalid")
    try:
        expected = _local_tree_inventory(plan)
    except (TypeError, ValueError) as error:
        raise FinalMaterializationError(
            "local tree Plan inventory is invalid"
        ) from error
    try:
        revalidate_local_tree(source, expected)
    except TreeAdmissionError as error:
        message = (
            "local source tree changed before materialization"
            if error.code == "membership_drift"
            else "local source tree could not be enumerated"
        )
        raise FinalMaterializationError(message) from error
    except (OSError, ValueError) as error:
        raise FinalMaterializationError(
            "local source tree could not be enumerated"
        ) from error

    _ensure_directory(stage, relative_path)
    for member in plan.members:
        member_path = PurePosixPath(member.relative_path)
        if member.kind == "directory":
            _ensure_directory(stage, relative_path / member_path)
            continue
        source_member = source.joinpath(*member_path.parts)
        target_member = relative_path / member_path
        if check_placeholders:
            _write(stage, target_member, b"")
        else:
            _materialize_regular_file(
                stage,
                target_member,
                source_member,
                expected_digest=member.digest,
                mode=mode,
            )

    try:
        revalidate_local_tree(source, expected)
    except TreeAdmissionError as error:
        message = (
            "local source tree changed during materialization"
            if error.code == "membership_drift"
            else "local source tree could not be re-enumerated"
        )
        raise FinalMaterializationError(message) from error
    except (OSError, ValueError) as error:
        raise FinalMaterializationError(
            "local source tree could not be re-enumerated"
        ) from error


def _local_tree_inventory(plan: LocalTreePlan) -> LocalTreeInventory:
    """Convert the strict Plan inventory to the shared admission shape."""
    return LocalTreeInventory(
        tuple(
            LocalTreeMember(
                member.relative_path,
                member.kind,
                member.size,
                member.digest,
            )
            for member in plan.members
        ),
    )


def _ensure_directory(stage: Path, relative_path: PurePosixPath) -> None:
    """Create one Plan-selected directory with deterministic POSIX mode."""
    target = stage
    directory_mode = int(local_tree_mode("directory"), 8)
    for part in relative_path.parts:
        target /= part
        try:
            target.mkdir(mode=directory_mode, exist_ok=True)
            observed = target.lstat()
        except OSError as error:
            raise FinalMaterializationError(
                "local tree directory could not be materialized"
            ) from error
        if _observed_path_is_reparse(observed) or not stat.S_ISDIR(observed.st_mode):
            raise FinalMaterializationError(
                "local tree directory must be a real directory"
            )
        if _platform_name == "posix":
            try:
                target.chmod(directory_mode)
            except OSError as error:
                raise FinalMaterializationError(
                    "local tree directory mode could not be materialized"
                ) from error


def _materialize_regular_file(
    stage: Path,
    relative_path: PurePosixPath,
    source: Path,
    *,
    expected_digest: str | None,
    mode: str,
) -> None:
    """Copy one admitted regular file with the configured clone policy."""
    _write(stage, relative_path, b"")
    target = stage.joinpath(*relative_path.parts)

    def materialize(reader: AdmittedRegularFileReader) -> None:
        digest = hashlib.sha256() if expected_digest is not None else None
        try:
            with target.open("r+b", buffering=0) as output:
                cloned = False
                if mode != "copy":
                    try:
                        reader.clone_to(output.fileno())
                    except FileCloneUnavailableError:
                        if mode == "clone":
                            raise FinalMaterializationError(
                                "copy-on-write clone is unavailable"
                            ) from None
                        os.ftruncate(output.fileno(), 0)
                        os.lseek(output.fileno(), 0, os.SEEK_SET)
                    else:
                        cloned = True
                if not cloned:
                    while chunk := reader.read_chunk():
                        if digest is not None:
                            digest.update(chunk)
                        _write_all(output.fileno(), chunk)
                elif digest is not None:
                    while chunk := reader.read_chunk():
                        digest.update(chunk)
                if os.fstat(output.fileno()).st_size != reader.size:
                    raise FinalMaterializationError(
                        "materialized local file size does not match its source"
                    )
        except FinalMaterializationError:
            raise
        except OSError as error:
            raise FinalMaterializationError(
                "local file could not be materialized"
            ) from error
        if digest is not None and f"sha256:{digest.hexdigest()}" != expected_digest:
            raise FinalMaterializationError(
                "local source digest does not match BuildPlan"
            )

    try:
        operate_regular_absolute_file(source, materialize)
    except FinalMaterializationError:
        raise
    except (OSError, ValueError) as error:
        raise FinalMaterializationError(
            "local source must be a readable regular file without symlinks"
        ) from error


def _observed_path_is_reparse(observed: os.stat_result) -> bool:
    """Recognize links and Windows reparse points in one no-follow observation."""
    return stat.S_ISLNK(observed.st_mode) or bool(
        getattr(observed, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written < 1:
            raise OSError("materialized target write made no progress")
        remaining = remaining[written:]


def _write(
    stage: Path,
    relative_path: PurePosixPath,
    content: bytes,
    *,
    executable: bool = False,
) -> None:
    parent = stage
    try:
        for part in relative_path.parts[:-1]:
            parent /= part
            parent.mkdir(mode=0o755, exist_ok=True)
            if _platform_name == "posix":
                parent.chmod(0o755)
    except OSError as error:
        raise FinalMaterializationError(
            "materialized parent could not be created"
        ) from error
    path = stage.joinpath(*relative_path.parts)
    try:
        with path.open("xb") as output:
            output.write(content)
            if _platform_name == "posix":
                os.fchmod(output.fileno(), 0o755 if executable else 0o644)
    except OSError as error:
        raise FinalMaterializationError(
            "materialized target could not be written"
        ) from error
