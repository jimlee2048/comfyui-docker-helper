"""Native filesystem capability evidence for Host file admission."""

import os
from pathlib import Path

import pytest

from comfyui_docker_helper.filesystem import admission as file_admission


@pytest.mark.skipif(os.name != "posix", reason="requires native Linux reflink")
def test_native_posix_clone_is_independent_when_supported(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"reflink source bytes")
    target.write_bytes(b"")

    def clone(reader: file_admission.AdmittedRegularFileReader) -> None:
        with target.open("r+b", buffering=0) as output:
            reader.clone_to(output.fileno())

    try:
        file_admission.operate_regular_absolute_file(source, clone)
    except file_admission.FileCloneUnavailableError:
        pytest.skip("test filesystem does not support FICLONE")

    assert target.read_bytes() == b"reflink source bytes"
    assert source.stat().st_ino != target.stat().st_ino
    source.write_bytes(b"changed source bytes")
    assert target.read_bytes() == b"reflink source bytes"
