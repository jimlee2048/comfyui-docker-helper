"""Plan-selected local-tree image normalization contracts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from comfyui_docker_helper.container.build import local_trees
from comfyui_docker_helper.container.build.admission import (
    LocalTreeMemberInput,
    LocalTreeNormalizationInput,
)
from comfyui_docker_helper.container.build.local_trees import (
    LocalTreeNormalizationError,
    normalize_local_trees,
    validate_local_trees,
)


def _tree(
    root: Path,
    relative_target: str = "user/default/workflows",
    members: tuple[LocalTreeMemberInput, ...] = (),
) -> LocalTreeNormalizationInput:
    target = root if relative_target == "." else root / relative_target
    return LocalTreeNormalizationInput(
        target=str(target),
        members=members,
    )


def test_empty_tree_creates_and_normalizes_only_selected_root(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"

    normalize_local_trees((_tree(root, "."),), root)

    assert root.is_dir()
    assert (root.stat().st_mode & 0o777) == 0o755


def test_normalizer_modes_new_nested_target_parents_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    unrelated = root / "unrelated"
    unrelated.mkdir(mode=0o755)
    unrelated.chmod(0o700)

    previous_umask = os.umask(0o077)
    try:
        normalize_local_trees(
            (_tree(root, "created/nested/workflows"),),
            root,
        )
    finally:
        os.umask(previous_umask)

    for path in (
        root / "created",
        root / "created" / "nested",
        root / "created" / "nested" / "workflows",
    ):
        assert path.stat().st_mode & 0o777 == 0o755
    assert unrelated.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="requires symbolic links")
def test_normalizer_rejects_symlink_target_parent_without_following_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    target_parent = root / "user" / "default"
    target_parent.parent.mkdir()
    target_parent.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(LocalTreeNormalizationError, match="link or reparse"):
        normalize_local_trees((_tree(root, "user/default/workflows"),), root)


def test_tree_overlay_validation_and_normalization_preserve_unrelated_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "user" / "default" / "workflows"
    (target / "nested").mkdir(parents=True)
    (target / "unrelated.txt").write_bytes(b"keep")
    (target / "nested" / "unrelated.txt").write_bytes(b"keep")
    (target / "nested" / "selected.txt").write_bytes(b"selected")
    target.chmod(0o700)
    (target / "nested").chmod(0o700)
    (target / "nested" / "selected.txt").chmod(0o600)
    members = (
        LocalTreeMemberInput("nested", "directory"),
        LocalTreeMemberInput("nested/selected.txt", "file"),
        LocalTreeMemberInput("selected-empty", "directory"),
    )

    monkeypatch.setattr(
        local_trees.os,
        "scandir",
        lambda *_args, **_kwargs: pytest.fail("tree path admission must not enumerate"),
    )
    before_modes = {
        path: path.stat().st_mode & 0o777
        for path in (target, target / "nested", target / "nested" / "selected.txt")
    }
    validate_local_trees((_tree(root, members=members),), root)
    assert {path: path.stat().st_mode & 0o777 for path in before_modes} == before_modes

    normalize_local_trees((_tree(root, members=members),), root)

    assert (target / "unrelated.txt").read_bytes() == b"keep"
    assert (target / "nested" / "unrelated.txt").read_bytes() == b"keep"
    assert (target.stat().st_mode & 0o777) == 0o755
    assert (target / "nested").stat().st_mode & 0o777 == 0o755
    assert (target / "nested" / "selected.txt").stat().st_mode & 0o777 == 0o644
    assert (target / "selected-empty").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    ("member", "existing_kind"),
    [
        (LocalTreeMemberInput("selected", "directory"), "file"),
        (LocalTreeMemberInput("selected", "file"), "directory"),
    ],
    ids=["directory-over-file", "file-over-directory"],
)
def test_tree_placement_rejects_selected_type_conflicts(
    tmp_path: Path,
    member: LocalTreeMemberInput,
    existing_kind: str,
) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "user" / "default" / "workflows"
    target.mkdir(parents=True)
    selected = target / "selected"
    if existing_kind == "file":
        selected.write_bytes(b"existing")
    else:
        selected.mkdir()
        (selected / "keep.txt").write_bytes(b"keep")

    for operation in (validate_local_trees, normalize_local_trees):
        with pytest.raises(LocalTreeNormalizationError, match="conflicts"):
            operation((_tree(root, members=(member,)),), root)
        if existing_kind == "file":
            assert selected.read_bytes() == b"existing"
        else:
            assert (selected / "keep.txt").read_bytes() == b"keep"


def test_normalizer_rejects_missing_selected_file(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "user" / "default" / "workflows"
    target.mkdir(parents=True)

    with pytest.raises(LocalTreeNormalizationError, match="file is missing"):
        normalize_local_trees(
            (
                _tree(
                    root,
                    members=(LocalTreeMemberInput("missing", "file"),),
                ),
            ),
            root,
        )


def test_normalizer_rejects_path_outside_comfyui_containment(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    tree = LocalTreeNormalizationInput(
        target=str(tmp_path / "outside"),
        members=(),
    )

    with pytest.raises(LocalTreeNormalizationError, match=r"invalid|escapes"):
        normalize_local_trees((tree,), root)


def test_tree_validation_allows_absent_target_ancestors_and_members(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    tree = _tree(
        root,
        "new/nested/workflows",
        members=(
            LocalTreeMemberInput("selected", "file"),
            LocalTreeMemberInput("empty", "directory"),
        ),
    )

    validate_local_trees((tree,), root)

    assert not (root / "new").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="requires symbolic links")
def test_tree_validation_rejects_symlinked_comfyui_ancestor(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "workspace"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    root = linked_parent / "ComfyUI"

    with pytest.raises(LocalTreeNormalizationError, match="link or reparse"):
        validate_local_trees((_tree(root, "models/workflows"),), root)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            "target-parent-file",
            "conflicts with a file",
        ),
        (
            "target-link",
            "link or reparse",
        ),
        (
            "selected-special",
            "special filesystem node",
        ),
    ],
    ids=["target-parent-file", "target-link", "selected-special"],
)
def test_tree_validation_rejects_incompatible_existing_paths(
    tmp_path: Path,
    setup: str,
    message: str,
) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "user" / "default" / "workflows"
    root.mkdir()
    if setup == "target-parent-file":
        (root / "user").write_bytes(b"file")
    elif setup == "target-link":
        (root / "user").mkdir()
        (root / "user" / "default").symlink_to(tmp_path / "elsewhere")
    else:
        target.mkdir(parents=True)
        os.mkfifo(target / "selected")

    member = (
        LocalTreeMemberInput("selected", "file")
        if setup == "selected-special"
        else LocalTreeMemberInput("selected", "directory")
    )

    with pytest.raises(LocalTreeNormalizationError, match=message):
        validate_local_trees((_tree(root, members=(member,)),), root)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="requires symbolic links")
def test_normalizer_rejects_selected_links_without_following_them(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    target = root / "user" / "default" / "workflows"
    target.mkdir(parents=True)
    target.joinpath("selected").symlink_to(tmp_path / "elsewhere")
    member = LocalTreeMemberInput("selected", "directory")

    with pytest.raises(LocalTreeNormalizationError, match="link or reparse"):
        normalize_local_trees((_tree(root, members=(member,)),), root)
