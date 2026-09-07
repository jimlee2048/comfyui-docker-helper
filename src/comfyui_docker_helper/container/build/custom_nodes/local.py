"""Plan-selected local node copying and exact installed-root proof."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from comfyui_docker_helper.config.planning.build_plan import (
    LocalNodePlan,
    LocalTreeMemberPlan,
)
from comfyui_docker_helper.config.planning.local_tree import local_tree_digest
from comfyui_docker_helper.config.validation.selectors import (
    validate_local_node_target_dir,
)
from comfyui_docker_helper.container.build.custom_nodes import contracts
from comfyui_docker_helper.filesystem.admission import (
    LocalTreeInventory,
    LocalTreeMember,
    admit_local_source,
    consume_regular_absolute_file,
)

LOCAL_NODES_DIRECTORY = Path("/opt/cdh/build/local-nodes")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def planned_local_target(node: LocalNodePlan, custom_nodes_root: Path) -> Path:
    target = Path(node.target)
    if not target.is_absolute() or target.parent != custom_nodes_root:
        raise contracts.CustomNodeInstallError(
            "Local target does not match the safe BuildPlan path"
        )
    try:
        validate_local_node_target_dir(target.name)
    except ValueError as error:
        raise contracts.CustomNodeInstallError(
            "Local target does not match the safe BuildPlan path"
        ) from error
    return target


def verify_local_root(node: LocalNodePlan, custom_nodes_root: Path) -> Path:
    root = contracts._require_real_directory(custom_nodes_root, "custom-nodes root")
    target = planned_local_target(node, root)
    return contracts._require_real_directory(target, f"Local node {target.name}")


def prepare_local_node(
    node: LocalNodePlan,
    custom_nodes_root: Path,
    *,
    local_nodes_directory: Path = LOCAL_NODES_DIRECTORY,
) -> Path:
    """Copy one read-only mounted inventory before any current-node execution."""
    root = contracts._require_real_directory(custom_nodes_root, "custom-nodes root")
    target = planned_local_target(node, root)
    source = local_nodes_directory / Path(node.context_path).name
    locked = node.verification == "sha256"
    try:
        # This scan admits structure only. Content is consumed once, by the copy.
        admitted = admit_local_source(source)
        if admitted.kind != "tree" or admitted.tree is None:
            raise contracts.CustomNodeInstallError("Local node source must be a tree")
        expected = tuple((member.relative_path, member.kind) for member in node.members)
        actual = tuple(
            (member.relative_path.as_posix(), member.kind)
            for member in admitted.tree.members
        )
        if actual != expected:
            raise contracts.CustomNodeInstallError(
                "Local node source structure does not match BuildPlan"
            )
        verified: list[LocalTreeMember] = []
        with _open_directory(root) as root_fd:
            os.mkdir(target.name, mode=0o755, dir_fd=root_fd)
            with _open_directory(Path(target.name), parent_fd=root_fd) as target_fd:
                os.fchmod(target_fd, 0o755)
                for member in node.members:
                    relative = Path(member.relative_path)
                    with _open_directory(
                        relative.parent, parent_fd=target_fd
                    ) as parent_fd:
                        if member.kind == "directory":
                            os.mkdir(relative.name, mode=0o755, dir_fd=parent_fd)
                            with _open_directory(
                                Path(relative.name), parent_fd=parent_fd
                            ) as fd:
                                os.fchmod(fd, 0o755)
                            verified.append(
                                LocalTreeMember(member.relative_path, "directory")
                            )
                            continue
                        verified.append(
                            _copy_file(
                                source, relative, parent_fd, member, locked=locked
                            )
                        )
        if (
            locked
            and local_tree_digest(LocalTreeInventory(tuple(verified)))
            != node.tree_digest
        ):
            raise contracts.CustomNodeInstallError(
                "Local node source tree digest does not match BuildPlan"
            )
    except (OSError, ValueError) as error:
        raise contracts.CustomNodeInstallError(
            f"Local node {target.name} source copy failed"
        ) from error
    return target


def _copy_file(
    source: Path,
    relative: Path,
    parent_fd: int,
    member: LocalTreeMemberPlan,
    *,
    locked: bool,
) -> LocalTreeMember:
    digest = hashlib.sha256() if locked else None
    size = 0
    descriptor = os.open(
        relative.name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
        dir_fd=parent_fd,
    )
    try:
        stream = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        os.fchmod(stream.fileno(), 0o644)

        def consume(chunk: bytes) -> None:
            nonlocal size
            size += len(chunk)
            if locked and member.size is not None and size > member.size:
                raise contracts.CustomNodeInstallError(
                    "Local node source size does not match BuildPlan"
                )
            stream.write(chunk)
            if digest is not None:
                digest.update(chunk)

        consume_regular_absolute_file(source / relative, consume)
    observed_digest = None if digest is None else f"sha256:{digest.hexdigest()}"
    if locked and (size != member.size or observed_digest != member.digest):
        raise contracts.CustomNodeInstallError(
            "Local node source content does not match BuildPlan"
        )
    return LocalTreeMember(
        member.relative_path, "file", size if locked else None, observed_digest
    )


@contextmanager
def _open_directory(path: Path, *, parent_fd: int | None = None) -> Iterator[int]:
    """Anchor every write ancestor without following directory links."""
    descriptor = os.open(
        "/" if path.is_absolute() else ".", _DIRECTORY_FLAGS, dir_fd=parent_fd
    )
    try:
        for part in path.parts:
            if part in {"/", "."}:
                continue
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            previous = descriptor
            descriptor = child
            os.close(previous)
        yield descriptor
    finally:
        os.close(descriptor)
