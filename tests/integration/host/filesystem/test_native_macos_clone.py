"""Native APFS acquisition through the materializer and admitted source lifetime."""

import errno
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import PurePosixPath

import pytest

from comfyui_docker_helper.filesystem import macos
from comfyui_docker_helper.filesystem.admission import operate_regular_absolute_file
from comfyui_docker_helper.host.context.service import _cleanup_owned_after_failure
from comfyui_docker_helper.rendering.final_materializer import (
    FinalMaterializationError,
    _materialize_regular_file,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires native macOS clone"
)


def materialize(source, stage, *, mode="clone", digest=None):
    _materialize_regular_file(
        stage, PurePosixPath("nested/target"), source, expected_digest=digest, mode=mode
    )
    return stage / "nested/target"


@pytest.mark.parametrize(
    "attribute",
    ["plain", "readonly", "hidden", "quarantine", "resource_fork", "custom"],
)
def test_native_clone_preserves_content_and_attributes(tmp_path, attribute):
    source = tmp_path / "source"
    content = b"model bytes\x00\r\n" * 32
    source.write_bytes(content)
    name = {
        "quarantine": "com.apple.quarantine",
        "resource_fork": "com.apple.ResourceFork",
        "custom": "org.cdh.test",
    }.get(attribute)
    value = "0081;00000000;cdh-test;" if attribute == "quarantine" else "test metadata"
    if name:
        subprocess.run(["xattr", "-w", name, value, str(source)], check=True)
    if attribute == "readonly":
        source.chmod(0o444)
    if attribute == "hidden":
        os.chflags(source, stat.UF_HIDDEN)
    before = source.stat()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    target = materialize(
        source, stage, digest=f"sha256:{hashlib.sha256(content).hexdigest()}"
    )
    assert target.read_bytes() == content
    assert target.stat().st_ino != source.stat().st_ino
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert source.stat().st_mode == before.st_mode
    assert source.stat().st_flags == before.st_flags
    if name:
        for path in (source, target):
            result = subprocess.run(
                ["xattr", "-p", name, str(path)], check=True, capture_output=True
            )
            assert result.stdout.rstrip(b"\n") == value.encode()
    if attribute == "hidden":
        assert target.stat().st_flags & stat.UF_HIDDEN
    target.write_bytes(b"target changed")
    assert source.read_bytes() == content
    if attribute != "readonly":
        source.write_bytes(b"source changed")
        assert target.read_bytes() == b"target changed"
    _cleanup_owned_after_failure(stage)
    assert not stage.exists()


@pytest.mark.parametrize(
    "flag", [stat.UF_IMMUTABLE, stat.UF_APPEND] if sys.platform == "darwin" else [2, 4]
)
def test_restricted_source_falls_back_without_creating_restricted_clone(tmp_path, flag):
    source = tmp_path / "source"
    source.write_bytes(b"model bytes")
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    os.chflags(source, flag)
    try:
        with pytest.raises(FinalMaterializationError, match="clone is unavailable"):
            materialize(source, stage)
        assert not (stage / "nested/target").exists()
        target = materialize(source, stage, mode="auto")
        assert target.read_bytes() == b"model bytes"
        assert not target.stat().st_flags & flag
        assert source.stat().st_flags & flag
        _cleanup_owned_after_failure(stage)
        assert not stage.exists()
    finally:
        os.chflags(source, 0)


def test_native_existing_target_is_preserved(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"source")
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError) as error:
            operate_regular_absolute_file(
                source, lambda reader: reader.clone_new_file(directory, "target", 0o644)
            )
        assert error.value.errno == errno.EEXIST
        assert target.read_bytes() == b"keep"
    finally:
        os.close(directory)


def test_failed_clone_normalization_is_hard_error_and_stage_is_removable(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.write_bytes(b"model")
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    chmod = os.chmod

    def fail_target(path, mode, **kwargs):
        if kwargs.get("dir_fd") is not None:
            raise PermissionError(errno.EACCES, "normalization failed")
        return chmod(path, mode, **kwargs)

    monkeypatch.setattr(macos.os, "chmod", fail_target)
    with pytest.raises(FinalMaterializationError, match="could not be materialized"):
        materialize(source, stage, mode="auto")
    assert (stage / "nested/target").read_bytes() == b"model"
    _cleanup_owned_after_failure(stage)
    assert not stage.exists()
