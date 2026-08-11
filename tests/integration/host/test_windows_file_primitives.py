"""Native Windows filesystem evidence for the narrow admission backend."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from comfyui_docker_helper import file_admission

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Win32 handles and reparse points",
)


def test_windows_regular_file_uses_handle_bytes_without_posix_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source with spaces 密钥.txt"
    source.write_bytes(b"secret bytes")

    admitted = file_admission.read_bounded_regular_absolute_file(
        source, max_bytes=len(b"secret bytes")
    )

    assert admitted.data == b"secret bytes"
    assert admitted.mode is None
    with pytest.raises(OSError) as raised:
        file_admission.read_bounded_regular_absolute_file(
            source, max_bytes=len(b"secret bytes") - 1
        )
    assert str(raised.value) == "admitted input exceeds the maximum byte count"


@pytest.mark.parametrize("location", ["leaf", "ancestor"])
def test_windows_admission_rejects_leaf_symlink_and_ancestor_junction(
    tmp_path: Path, location: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside")
    safe = tmp_path / "safe"
    safe.mkdir()
    link = safe / "linked"
    if location == "leaf":
        source = link.with_suffix(".txt")
        source.symlink_to(outside / "secret.txt")
        expected_error = "admitted input must be a regular local file"
    else:
        source = link / "secret.txt"
        _create_junction(link, outside)
        expected_error = "admitted path ancestors must be real local directories"
    try:
        with pytest.raises(OSError) as raised:
            file_admission.read_regular_absolute_file(source)
        assert str(raised.value) == expected_error
    finally:
        if location == "leaf":
            source.unlink()
        else:
            link.rmdir()


def _create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
