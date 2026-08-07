"""Secret-only bounded regular-file admission contracts."""

import os
import stat
from pathlib import Path

import pytest

from comfyui_docker_helper import file_admission

_SECRET_LIMIT = 65_525


@pytest.mark.parametrize(
    ("size", "accepted"),
    [(_SECRET_LIMIT, True), (_SECRET_LIMIT + 1, False)],
)
def test_bounded_admission_accepts_the_limit_and_rejects_the_next_byte(
    tmp_path: Path, size: int, accepted: bool
) -> None:
    source = tmp_path / "secret"
    source.write_bytes(b"x" * size)

    if accepted:
        admitted = file_admission.read_bounded_regular_absolute_file(
            source, max_bytes=_SECRET_LIMIT
        )
        assert admitted.data == b"x" * size
    else:
        with pytest.raises(OSError) as raised:
            file_admission.read_bounded_regular_absolute_file(
                source, max_bytes=_SECRET_LIMIT
            )
        assert str(raised.value) == "admitted input exceeds the maximum byte count"


def test_bounded_admission_observes_mode_and_bytes_through_the_same_leaf_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "secret"
    source.write_bytes(b"secret bytes")
    source.chmod(0o640)
    real_fstat = os.fstat
    real_read = os.read
    fstat_descriptors: list[int] = []
    read_descriptors: list[int] = []

    def observed_fstat(descriptor: int) -> os.stat_result:
        fstat_descriptors.append(descriptor)
        return real_fstat(descriptor)

    def observed_read(descriptor: int, length: int) -> bytes:
        read_descriptors.append(descriptor)
        return real_read(descriptor, length)

    monkeypatch.setattr(file_admission.os, "fstat", observed_fstat)
    monkeypatch.setattr(file_admission.os, "read", observed_read)

    admitted = file_admission.read_bounded_regular_absolute_file(
        source, max_bytes=_SECRET_LIMIT
    )

    assert admitted.data == b"secret bytes"
    assert stat.S_IMODE(admitted.mode) == 0o640
    assert len(fstat_descriptors) == 1
    assert read_descriptors
    assert set(read_descriptors) == set(fstat_descriptors)


def test_existing_unbounded_admission_still_reads_beyond_the_secret_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large-input"
    content = b"x" * (_SECRET_LIMIT + 1)
    source.write_bytes(content)

    assert file_admission.read_regular_absolute_file(source) == content
