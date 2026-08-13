"""Native Windows evidence for build-context materialization and publication."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from comfyui_docker_helper.host import render_service
from comfyui_docker_helper.host.render_service import HostRenderServiceError

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows rename and open-handle behavior",
)


def test_windows_context_publish_check_and_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "context with spaces"
    content = bytearray(b"first")
    _install_fake_materializer(monkeypatch, content)

    _write(output, overwrite=False)

    assert (output / "Dockerfile").read_bytes() == b"first"
    assert (output / "config.lock.toml").read_bytes() == b"lock\n"
    assert render_service._valid_marker(output)
    _check(output)

    content[:] = b"second"
    with pytest.raises(HostRenderServiceError) as changed:
        _check(output)
    assert changed.value.diagnostics[0].code == "render.context_changed"

    _write(output, overwrite=True)

    assert (output / "Dockerfile").read_bytes() == b"second"
    _check(output)
    assert not tuple(tmp_path.glob(f"{render_service._STAGE_PREFIX}*"))
    assert not tuple(tmp_path.glob(f"{render_service._BACKUP_PREFIX}*"))
    assert not tuple(tmp_path.glob(f"{render_service._CHECK_PREFIX}*"))


def test_windows_open_output_handle_preserves_original_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import win32con
    import win32file

    output = tmp_path / "context"
    content = bytearray(b"first")
    _install_fake_materializer(monkeypatch, content)
    _write(output, overwrite=False)
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
        content[:] = b"second"
        with pytest.raises(HostRenderServiceError) as raised:
            _write(output, overwrite=True)
    finally:
        handle.Close()

    assert raised.value.diagnostics[0].code == "render.context_write_failed"
    assert (output / "Dockerfile").read_bytes() == b"first"
    assert render_service._valid_marker(output)
    assert not tuple(tmp_path.glob(f"{render_service._STAGE_PREFIX}*"))
    assert not tuple(tmp_path.glob(f"{render_service._BACKUP_PREFIX}*"))


def _install_fake_materializer(
    monkeypatch: pytest.MonkeyPatch, content: bytearray
) -> None:
    def materialize(_plan: object, directory: str | Path, **_kwargs: object) -> None:
        Path(directory, "Dockerfile").write_bytes(bytes(content))

    monkeypatch.setattr(render_service, "_materialize_private_stage", materialize)
    monkeypatch.setattr(
        render_service,
        "dump_canonical_lock_toml",
        lambda _lock: "lock\n",
    )


def _write(output: Path, *, overwrite: bool) -> None:
    render_service._write_context(
        output,
        object(),
        object(),
        object(),
        (),
        local_file_mode="copy",
        overwrite=overwrite,
    )


def _check(output: Path) -> None:
    render_service._check_context(
        output,
        object(),
        object(),
        object(),
        (),
        local_file_mode="copy",
        check_unlocked_sources=True,
    )
