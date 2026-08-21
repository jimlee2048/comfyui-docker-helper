"""Materialize BuildPlan artifacts and verified local inputs."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomli_w

from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    HookPlan,
    HttpFilePlan,
    LocalFilePlan,
    build_plan_hook_identities,
    dump_build_plan_json,
)
from comfyui_docker_helper.config.runtime_hooks import (
    BUILD_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_LOCK_PREFIX,
)
from comfyui_docker_helper.file_admission import (
    AdmittedRegularFileReader,
    FileCloneUnavailableError,
    operate_regular_absolute_file,
    read_regular_absolute_file,
)
from comfyui_docker_helper.release_artifacts import (
    WORKSPACE_PROFILE_CONTEXT_PATH,
    WORKSPACE_PROFILE_RESOURCE,
    CanonicalWheel,
)
from comfyui_docker_helper.rendering.final_renderer import (
    render_build_plan_dockerfile,
)

_platform_name = os.name


class FinalMaterializationError(RuntimeError):
    """The BuildPlan context could not be materialized safely."""


@dataclass(frozen=True, slots=True)
class LocalMaterializationSource:
    """Host-only source path paired with one plan-owned relative identity."""

    relative_path: PurePosixPath
    source_path: Path


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
    expected_sources = {*expected_hooks, *local_files}
    sources = {item.relative_path.as_posix(): item for item in local_sources}
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
        source = sources[relative_path].source_path
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
    for relative_path, item in local_files.items():
        output = PurePosixPath(relative_path)
        if check_placeholders:
            _write(stage, output, b"")
            continue
        _materialize_local_file(
            stage,
            output,
            sources[relative_path].source_path,
            item,
            mode=local_file_mode,
        )
    _write(
        stage,
        PurePosixPath("runtime/config.toml"),
        _runtime_config_bytes(plan),
    )
    _write(
        stage,
        WORKSPACE_PROFILE_CONTEXT_PATH,
        _workspace_profile_bytes(),
    )
    _materialize_canonical_wheel(plan, canonical_wheel, stage)
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


def _workspace_profile_bytes() -> bytes:
    try:
        return WORKSPACE_PROFILE_RESOURCE.read_bytes()
    except OSError as error:
        raise FinalMaterializationError(
            "workspace profile resource could not be read"
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
            "url": item.url,
            "target_dir": relative.parent.as_posix(),
            "filename": relative.name,
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
    _write(stage, relative_path, b"")
    target = stage.joinpath(*relative_path.parts)

    def materialize(reader: AdmittedRegularFileReader) -> None:
        digest = hashlib.sha256() if plan.digest is not None else None
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
        if digest is not None and f"sha256:{digest.hexdigest()}" != plan.digest:
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
