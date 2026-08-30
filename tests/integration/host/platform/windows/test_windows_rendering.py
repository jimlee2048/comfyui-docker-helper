"""Native Windows evidence for build-context materialization and publication."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from tests.host_render_service_support import (
    FakeAcquirer,
    _config,
    _prepare,
    _tree,
)

from comfyui_docker_helper.host.context.service import (
    HostRenderServiceError,
    PlanningOptions,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows rename and open-handle behavior",
)


def test_windows_context_publish_check_and_overwrite(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config(install_cli=False))
    output = tmp_path / "context with spaces"

    _prepare(config, output, FakeAcquirer())

    initial = _tree(output)
    assert initial["Dockerfile"]
    assert initial["config.lock.toml"]
    _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))

    config.write_text(_config(install_cli=True))
    with pytest.raises(HostRenderServiceError) as changed:
        _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))
    assert changed.value.diagnostics[0].code == "render.context_changed"

    _prepare(config, output, FakeAcquirer(), overwrite=True)

    _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))
    assert {path.name for path in tmp_path.iterdir()} == {config.name, output.name}


def test_windows_open_output_handle_preserves_original_context(
    tmp_path: Path,
) -> None:
    import win32con
    import win32file

    config = tmp_path / "config.toml"
    config.write_text(_config(install_cli=False))
    output = tmp_path / "context"
    _prepare(config, output, FakeAcquirer())
    original = _tree(output)
    handle = win32file.CreateFile(
        os.fspath(output),
        win32con.GENERIC_READ,
        win32con.FILE_SHARE_READ,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    try:
        config.write_text(_config(install_cli=True))
        with pytest.raises(HostRenderServiceError) as raised:
            _prepare(config, output, FakeAcquirer(), overwrite=True)
    finally:
        handle.Close()

    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    assert _tree(output) == original
    config.write_text(_config(install_cli=False))
    _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))
    assert {path.name for path in tmp_path.iterdir()} == {config.name, output.name}


def test_windows_local_tree_context_materializes_direct_members(
    tmp_path: Path,
) -> None:
    source = tmp_path / "local-tree"
    (source / "nested" / "empty").mkdir(parents=True)
    (source / "nested" / "payload.bin").write_bytes(b"payload")
    config = tmp_path / "config.toml"
    config.write_text(
        _config()
        + f'''
[[files]]
type = "local"
source = "{source.as_posix()}"
target = "user/default/workflows"
'''
    )
    output = tmp_path / "context"

    prepared = _prepare(config, output, FakeAcquirer())
    local = prepared.plan.files.files[0]
    context = output / local.context_path

    assert local.kind == "tree"
    assert (context / "nested" / "payload.bin").read_bytes() == b"payload"
    assert (context / "nested" / "empty").is_dir()
    assert not (context / source.name).exists()

    _prepare(config, output, FakeAcquirer(), options=PlanningOptions(check=True))
