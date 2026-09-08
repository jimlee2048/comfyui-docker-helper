"""Direct-Git custom-node installation and provenance helpers."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from comfyui_docker_helper.config.planning.build_plan import (
    GitNodePlan,
)
from comfyui_docker_helper.config.validation.selectors import is_safe_git_target_dir
from comfyui_docker_helper.container.build.custom_nodes import contracts

_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_GITLINK_MODE = b"160000"


def _prepare_git_node(
    node: GitNodePlan,
    custom_nodes_root: Path,
    git_path: Path,
    git_environment: Mapping[str, str],
) -> Path:
    root = contracts._require_real_directory(custom_nodes_root, "custom-nodes root")
    target = _planned_git_target(node, root)
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as error:
        raise contracts.CustomNodeInstallError(
            f"Git target {target.name} already exists"
        ) from error
    except OSError as error:
        raise contracts.CustomNodeInstallError(
            f"Git target {target.name} could not be created"
        ) from error
    _run_git(
        (git_path, "clone", "--no-checkout", "--", node.url, target),
        cwd=root,
        env=git_environment,
        description=f"Git node {target.name} clone",
    )
    _run_git(
        (git_path, "-C", target, "checkout", "--detach", node.commit, "--"),
        cwd=root,
        env=git_environment,
        description=f"Git node {target.name} exact checkout",
    )
    _run_git(
        (
            git_path,
            "-C",
            target,
            "submodule",
            "update",
            "--init",
            "--recursive",
            "--checkout",
        ),
        cwd=root,
        env=git_environment,
        description=f"Git node {target.name} recursive submodule checkout",
    )
    return target


def _verify_git_provenance(
    node: GitNodePlan,
    target: Path,
    custom_nodes_root: Path,
    git_path: Path,
    environment: Mapping[str, str],
) -> None:
    root = contracts._require_real_directory(custom_nodes_root, "custom-nodes root")
    expected_target = _planned_git_target(node, root)
    if target != expected_target:
        raise contracts.CustomNodeInstallError(
            "Git node target does not match BuildPlan"
        )
    actual = contracts._require_real_directory(
        target, f"Git node {expected_target.name}"
    )
    if actual.parent != root:
        raise contracts.CustomNodeInstallError(
            "Git node target escapes custom-nodes root"
        )
    _verify_repository_root(
        actual,
        actual,
        git_path,
        environment,
        description=f"Git node {expected_target.name}",
    )
    root_git_directory = _verify_root_git_directory(
        actual,
        git_path,
        environment,
        description=f"Git node {expected_target.name}",
    )
    _verify_exact_detached_head(
        actual,
        node.commit,
        root,
        git_path,
        environment,
        description=f"Git node {expected_target.name}",
    )
    _verify_committed_gitlinks(
        actual,
        actual,
        root,
        git_path,
        environment,
        seen={actual},
        root_git_directory=root_git_directory,
        description=f"Git node {expected_target.name}",
    )


def _verify_repository_root(
    repository: Path,
    expected: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> None:
    output = _run_git(
        (
            git_path,
            "-C",
            repository,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
        ),
        cwd=repository,
        env=environment,
        description=f"{description} repository-root verification",
    )
    if output != os.fsencode(expected) + b"\n":
        raise contracts.CustomNodeInstallError(
            f"{description} repository root does not match its exact target"
        )


def _verify_root_git_directory(
    repository: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> Path:
    dot_git = repository / ".git"
    git_directory = contracts._require_real_directory(
        dot_git, f"{description} .git directory"
    )
    if git_directory != dot_git:
        raise contracts.CustomNodeInstallError(
            f"{description} .git directory is not exact"
        )
    actual_git_directory, common_directory = _git_directory_paths(
        repository,
        git_path,
        environment,
        description=description,
    )
    if actual_git_directory != git_directory or common_directory != git_directory:
        raise contracts.CustomNodeInstallError(
            f"{description} Git directory escapes its exact root repository"
        )
    return git_directory


def _verify_submodule_git_directory(
    repository: Path,
    root_git_directory: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> None:
    dot_git = repository / ".git"
    try:
        metadata = dot_git.lstat()
    except OSError as error:
        raise contracts.CustomNodeInstallError(
            f"{description} .git file is unavailable"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise contracts.CustomNodeInstallError(
            f"{description} .git must be one regular non-symlink file"
        )
    actual_git_directory, common_directory = _git_directory_paths(
        repository,
        git_path,
        environment,
        description=description,
    )
    if actual_git_directory != common_directory:
        raise contracts.CustomNodeInstallError(
            f"{description} linked worktree is not permitted"
        )
    _require_contained_real_directory(
        actual_git_directory,
        root_git_directory,
        f"{description} Git directory",
    )


def _git_directory_paths(
    repository: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> tuple[Path, Path]:
    git_directory = _single_absolute_git_path(
        _run_git(
            (git_path, "-C", repository, "rev-parse", "--absolute-git-dir"),
            cwd=repository,
            env=environment,
            description=f"{description} Git-directory verification",
        ),
        f"{description} Git directory",
    )
    common_directory = _single_absolute_git_path(
        _run_git(
            (
                git_path,
                "-C",
                repository,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ),
            cwd=repository,
            env=environment,
            description=f"{description} common-Git-directory verification",
        ),
        f"{description} common Git directory",
    )
    return git_directory, common_directory


def _single_absolute_git_path(output: bytes, subject: str) -> Path:
    if not output.endswith(b"\n") or output.count(b"\n") != 1:
        raise contracts.CustomNodeInstallError(f"{subject} output is ambiguous")
    path = Path(os.fsdecode(output[:-1]))
    if not path.is_absolute():
        raise contracts.CustomNodeInstallError(f"{subject} is not absolute")
    return path


def _require_contained_real_directory(path: Path, root: Path, subject: str) -> Path:
    root = contracts._require_real_directory(root, "root Git directory")
    if path == root:
        raise contracts.CustomNodeInstallError(
            f"{subject} replaces the root Git directory"
        )
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise contracts.CustomNodeInstallError(
            f"{subject} escapes root Git management"
        ) from error
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise contracts.CustomNodeInstallError(
                f"{subject} is unavailable"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise contracts.CustomNodeInstallError(f"{subject} contains a symlink")
    return contracts._require_real_directory(path, subject)


def _verify_exact_detached_head(
    repository: Path,
    expected_commit: str,
    cwd: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    description: str,
) -> None:
    _run_git(
        (git_path, "-C", repository, "cat-file", "-e", f"{expected_commit}^{{commit}}"),
        cwd=cwd,
        env=environment,
        description=f"{description} locked commit object verification",
    )
    head = _run_git(
        (git_path, "-C", repository, "rev-parse", "--verify", "HEAD"),
        cwd=cwd,
        env=environment,
        description=f"{description} commit verification",
    )
    if head != f"{expected_commit}\n".encode("ascii"):
        raise contracts.CustomNodeInstallError(
            f"{description} commit does not match gitlink"
        )
    symbolic = _run_git_allowing(
        (git_path, "-C", repository, "symbolic-ref", "-q", "HEAD"),
        cwd=cwd,
        env=environment,
        description=f"{description} detached HEAD verification",
        allowed_returncodes=(0, 1),
    )
    if symbolic.returncode != 1:
        raise contracts.CustomNodeInstallError(f"{description} HEAD must be detached")


def _verify_committed_gitlinks(
    repository: Path,
    repository_root: Path,
    custom_nodes_root: Path,
    git_path: Path,
    environment: Mapping[str, str],
    *,
    seen: set[Path],
    root_git_directory: Path,
    description: str,
) -> None:
    tree = _run_git(
        (git_path, "-C", repository, "ls-tree", "-rz", "--full-tree", "HEAD"),
        cwd=custom_nodes_root,
        env=environment,
        description=f"{description} committed gitlink enumeration",
    )
    for record in tree.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, raw_commit = header.split(b" ", 2)
        except ValueError as error:
            raise contracts.CustomNodeInstallError(
                f"{description} committed tree output is invalid"
            ) from error
        if mode != _GITLINK_MODE:
            continue
        if (
            object_type != b"commit"
            or _COMMIT_PATTERN.fullmatch(raw_commit.decode("ascii", errors="ignore"))
            is None
        ):
            raise contracts.CustomNodeInstallError(
                f"{description} gitlink entry is invalid"
            )
        child_relative = _safe_gitlink_path(raw_path, description)
        child = repository.joinpath(*child_relative.parts)
        actual_child = contracts._require_real_directory(
            child, f"{description} submodule"
        )
        if (
            actual_child == repository
            or not actual_child.is_relative_to(repository_root)
            or not actual_child.is_relative_to(custom_nodes_root)
            or actual_child in seen
        ):
            raise contracts.CustomNodeInstallError(
                f"{description} submodule path is unsafe"
            )
        seen.add(actual_child)
        child_description = f"{description} submodule {child_relative.as_posix()}"
        _verify_submodule_git_directory(
            actual_child,
            root_git_directory,
            git_path,
            environment,
            description=child_description,
        )
        _verify_repository_root(
            actual_child,
            actual_child,
            git_path,
            environment,
            description=child_description,
        )
        expected_commit = raw_commit.decode("ascii")
        _verify_exact_detached_head(
            actual_child,
            expected_commit,
            custom_nodes_root,
            git_path,
            environment,
            description=child_description,
        )
        _verify_committed_gitlinks(
            actual_child,
            repository_root,
            custom_nodes_root,
            git_path,
            environment,
            seen=seen,
            root_git_directory=root_git_directory,
            description=child_description,
        )


def _safe_gitlink_path(raw_path: bytes, description: str) -> PurePosixPath:
    value = os.fsdecode(raw_path)
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise contracts.CustomNodeInstallError(f"{description} gitlink path is unsafe")
    return path


def _planned_git_target(node: GitNodePlan, custom_nodes_root: Path) -> Path:
    target = Path(node.target)
    if (
        not target.is_absolute()
        or target.parent != custom_nodes_root
        or not is_safe_git_target_dir(target.name)
    ):
        raise contracts.CustomNodeInstallError(
            "Git target does not match the safe BuildPlan path"
        )
    return target


def _run_git(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: Mapping[str, str],
    description: str,
) -> bytes:
    completed = _run_git_allowing(
        argv,
        cwd=cwd,
        env=env,
        description=description,
        allowed_returncodes=(0,),
    )
    return completed.stdout


def _run_git_allowing(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: Mapping[str, str],
    description: str,
    allowed_returncodes: Sequence[int],
) -> subprocess.CompletedProcess[bytes]:
    command = [os.fspath(item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env),
            check=False,
            stdout=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise contracts.CustomNodeInstallError(
            f"{description} failed to start"
        ) from error
    if completed.returncode not in allowed_returncodes:
        raise contracts.CustomNodeInstallError(
            f"{description} failed with exit code {completed.returncode}"
        )
    return completed
