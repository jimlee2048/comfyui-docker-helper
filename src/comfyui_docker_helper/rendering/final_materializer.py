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
    build_plan_hook_identities,
    dump_build_plan_json,
)
from comfyui_docker_helper.config.runtime_hooks import (
    BUILD_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_LOCK_PREFIX,
)
from comfyui_docker_helper.file_admission import read_regular_absolute_file
from comfyui_docker_helper.release_artifacts import CanonicalWheel
from comfyui_docker_helper.rendering.final_renderer import (
    render_build_plan_dockerfile,
)


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
    expected = {**build_hooks, **runtime_hooks}
    sources = {item.relative_path.as_posix(): item for item in local_sources}
    if len(sources) != len(local_sources) or set(sources) != set(expected):
        raise FinalMaterializationError(
            "local materialization sources must exactly match locked inputs"
        )
    _write(
        stage,
        PurePosixPath(".dockerignore"),
        b"/.cdh-rendered\n/config.lock.toml\n",
    )
    _write(stage, PurePosixPath("build-plan.json"), dump_build_plan_json(plan))
    for relative_path, hook in expected.items():
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
    _write(
        stage,
        PurePosixPath("runtime/config.toml"),
        _runtime_config_bytes(plan),
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
        target = PurePosixPath(item.target)
        try:
            relative = target.relative_to(comfyui_root)
        except ValueError as error:
            raise FinalMaterializationError(
                "runtime file target is outside ComfyUI"
            ) from error
        runtime_item = {
            "url": item.url,
            "dir": relative.parent.as_posix(),
            "filename": relative.name,
            "overwrite": item.overwrite,
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
            parent.chmod(0o755)
    except OSError as error:
        raise FinalMaterializationError(
            "materialized parent could not be created"
        ) from error
    path = stage.joinpath(*relative_path.parts)
    try:
        with path.open("xb") as output:
            output.write(content)
            os.fchmod(output.fileno(), 0o755 if executable else 0o644)
    except OSError as error:
        raise FinalMaterializationError(
            "materialized target could not be written"
        ) from error
