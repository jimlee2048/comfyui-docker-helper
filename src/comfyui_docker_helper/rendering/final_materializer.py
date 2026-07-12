"""Materialize BuildPlan artifacts and verified local inputs."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomli_w

from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    HookPlan,
    build_plan_digest,
    dump_build_plan_json,
    manifest_binding,
)
from comfyui_docker_helper.config.runtime_hooks import (
    CUSTOM_NODE_HOOK_LOCK_PREFIX,
    RUNTIME_HOOK_LOCK_PREFIX,
)
from comfyui_docker_helper.container.phase_inputs import phase_document
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


def materialize_build_plan(
    plan: BuildPlan,
    directory: str | Path,
    *,
    local_sources: tuple[LocalMaterializationSource, ...] = (),
) -> None:
    """Populate one existing empty directory without re-reading config or lock."""
    target = Path(directory)
    if not target.is_dir() or target.is_symlink():
        raise FinalMaterializationError(
            "materialization target must be a real directory"
        )
    if next(target.iterdir(), None) is not None:
        raise FinalMaterializationError("materialization target must be empty")
    try:
        custom_hooks, runtime_hooks = _expected_hooks(plan)
        expected = {**custom_hooks, **runtime_hooks}
        sources = {item.relative_path.as_posix(): item for item in local_sources}
        if len(sources) != len(local_sources) or set(sources) != set(expected):
            raise FinalMaterializationError(
                "local materialization sources must exactly match locked inputs"
            )
        _write(target / "build-plan.json", dump_build_plan_json(plan), root=target)
        digest = build_plan_digest(plan)
        binding = manifest_binding(plan)
        _write(
            target / "manifest-binding.json",
            _json_bytes(binding.model_dump(mode="json")),
            root=target,
        )
        phases = {
            "build": plan.build,
            "toolchain": plan.toolchain,
            "application": plan.application,
            "custom-nodes": plan.custom_nodes,
            "files": plan.files,
            "runtime": plan.runtime,
        }
        for name, payload in phases.items():
            document = phase_document(name, payload, digest)
            _write(
                target / "phases" / f"{name}.json",
                _json_bytes(document.model_dump(mode="json")),
                root=target,
            )
        for relative_path, hook in expected.items():
            source = sources[relative_path].source_path
            content = _verified_source(source, hook.digest)
            if relative_path in runtime_hooks:
                runtime_relative = relative_path.removeprefix(
                    f"{RUNTIME_HOOK_LOCK_PREFIX}/"
                )
                output = target / "runtime" / "hooks" / runtime_relative
            else:
                custom_relative = relative_path.removeprefix(
                    f"{CUSTOM_NODE_HOOK_LOCK_PREFIX}/"
                )
                output = target / "inputs" / custom_relative
            _write(output, content, root=target, executable=True)
        _write(
            target / "runtime" / "config.toml",
            _runtime_config_bytes(plan),
            root=target,
        )
        _write(
            target / "Dockerfile",
            render_build_plan_dockerfile(plan).encode("utf-8"),
            root=target,
        )
    except BaseException:
        for child in tuple(target.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        raise


def _expected_hooks(
    plan: BuildPlan,
) -> tuple[dict[str, HookPlan], dict[str, HookPlan]]:
    custom: dict[str, HookPlan] = {}
    for node in plan.custom_nodes.nodes:
        for hook in (*node.pre_install, *node.post_install):
            identity_path = f"{CUSTOM_NODE_HOOK_LOCK_PREFIX}/{hook.relative_path}"
            existing = custom.get(identity_path)
            if existing is not None and existing != hook:
                raise FinalMaterializationError("hook identity has conflicting digests")
            custom[identity_path] = hook
    runtime = {
        f"{RUNTIME_HOOK_LOCK_PREFIX}/{hook.relative_path}": hook
        for hook in plan.runtime.hooks
    }
    if len(runtime) != len(plan.runtime.hooks) or set(custom) & set(runtime):
        raise FinalMaterializationError("hook identity has conflicting paths")
    return custom, runtime


def _runtime_config_bytes(plan: BuildPlan) -> bytes:
    command = plan.runtime.launch_command
    if (
        len(command) < 7
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
        "downloader": plan.files.downloader.model_dump(
            mode="json", exclude={"default"}
        ),
    }
    if plan.runtime.download_failure_policy is not None:
        cdh["download_failure_policy"] = plan.runtime.download_failure_policy
    comfyui_root = PurePosixPath(plan.application.paths.comfyui)
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
        _require_real_directory_chain(path.parent)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise FinalMaterializationError("local source must be a regular file")
        content = path.read_bytes()
    except OSError as error:
        raise FinalMaterializationError("local source could not be read") from error
    observed = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if observed != expected_digest:
        raise FinalMaterializationError("local source digest does not match BuildPlan")
    return content


def _write(
    path: Path,
    content: bytes,
    *,
    root: Path,
    executable: bool = False,
) -> None:
    root_absolute = root.absolute()
    path_absolute = path.absolute()
    if root_absolute not in path_absolute.parents:
        raise FinalMaterializationError("materialized target escapes context root")
    _ensure_real_directory_chain(path.parent)
    try:
        root_resolved = root.resolve(strict=True)
        parent_resolved = path.parent.resolve(strict=True)
    except OSError as error:
        raise FinalMaterializationError(
            "materialized target containment could not be verified"
        ) from error
    if (
        root_resolved != parent_resolved
        and root_resolved not in parent_resolved.parents
    ):
        raise FinalMaterializationError("materialized target escapes context root")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise FinalMaterializationError(
            "materialized target could not be inspected"
        ) from error
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise FinalMaterializationError(
                "materialized target must not be a symlink or special file"
            )
        raise FinalMaterializationError("materialized target already exists")
    try:
        with path.open("xb") as output:
            output.write(content)
    except OSError as error:
        raise FinalMaterializationError(
            "materialized target could not be written"
        ) from error
    path.chmod(0o755 if executable else 0o644)


def _ensure_real_directory_chain(directory: Path) -> None:
    missing: list[Path] = []
    current = directory
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise FinalMaterializationError(
                    "materialized target has no existing directory anchor"
                ) from error
            current = parent
            continue
        except NotADirectoryError as error:
            raise FinalMaterializationError(
                "materialized parent must not be a symlink or special file"
            ) from error
        except OSError as error:
            raise FinalMaterializationError(
                "materialized parent could not be inspected"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise FinalMaterializationError(
                "materialized parent must not be a symlink or special file"
            )
        break
    for item in reversed(missing):
        try:
            item.mkdir(mode=0o755)
        except OSError as error:
            raise FinalMaterializationError(
                "materialized parent could not be created"
            ) from error
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise FinalMaterializationError(
                "materialized parent must remain a real directory"
            )


def _require_real_directory_chain(directory: Path) -> None:
    absolute = directory.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise FinalMaterializationError("local source parent is invalid") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise FinalMaterializationError(
                "local source parent must not be a symlink or special file"
            )


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
