"""Host-side baked runtime-hook input admission tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from comfyui_docker_helper.host import hook_paths
from comfyui_docker_helper.host.runtime_hook_inputs import (
    RuntimeHookInputError,
    discover_runtime_hook_inputs,
)


def test_runtime_hook_root_is_lexically_absolute_and_canonical(tmp_path: Path) -> None:
    root = tmp_path / "hooks"
    phase = root / "pre-start.d"
    phase.mkdir(parents=True)
    (phase / "10-start.sh").write_bytes(b"true\n")

    inputs = discover_runtime_hook_inputs(
        Path("unused") / ".." / root.name,
        working_directory=tmp_path,
    )

    assert inputs.source_root == root
    assert inputs.requests[0].root == root
    assert inputs.requests[0].relative_path.as_posix() == "pre-start.d/10-start.sh"


@pytest.mark.parametrize("location", ["root", "ancestor"])
def test_runtime_hook_root_rejects_a_static_symlink(
    tmp_path: Path, location: str
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    selected = linked
    if location == "ancestor":
        (target / "hooks").mkdir()
        selected = linked / "hooks"

    with pytest.raises(RuntimeHookInputError) as raised:
        discover_runtime_hook_inputs(selected, working_directory=None)

    assert raised.value.diagnostics[0].code == "runtime_hooks.source_not_directory"


@pytest.mark.parametrize(
    "invalid_root",
    [
        r"\\server\share\hooks",
        r"\\?\C:\hooks",
        r"\\.\C:\hooks",
        r"C:\hooks:stream",
    ],
)
def test_windows_hook_root_rejects_unsupported_namespace_before_lstat(
    invalid_root: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_lstat(_path: Path) -> os.stat_result:
        raise AssertionError("unsupported namespace must fail before lstat")

    monkeypatch.setattr(hook_paths, "_platform_name", "nt")
    monkeypatch.setattr(hook_paths, "_absolute_path", lambda _path: invalid_root)
    monkeypatch.setattr(Path, "lstat", unexpected_lstat)

    with pytest.raises(RuntimeHookInputError) as raised:
        discover_runtime_hook_inputs("ignored", working_directory=None)

    assert raised.value.diagnostics[0].code == "runtime_hooks.source_not_directory"
