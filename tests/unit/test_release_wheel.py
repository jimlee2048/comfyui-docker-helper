"""Canonical cdh wheel construction and admission contracts."""

from __future__ import annotations

import hashlib
import subprocess
import zipfile
from pathlib import Path

import pytest

from comfyui_docker_helper.exact_ledger import CDH_VERSION
from comfyui_docker_helper.host.release_wheel import (
    CanonicalWheelError,
    build_canonical_wheel,
)
from comfyui_docker_helper.host.uv_runner import HostUvRunner

WHEEL_NAME = f"comfyui_docker_helper-{CDH_VERSION}-py3-none-any.whl"


def _write_wheel(path: Path, *, name: str = "comfyui-docker-helper") -> bytes:
    metadata_root = f"comfyui_docker_helper-{CDH_VERSION}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{metadata_root}/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {CDH_VERSION}\n",
        )
        archive.writestr(
            f"{metadata_root}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{metadata_root}/RECORD", "")
    return path.read_bytes()


# The host builds one wheel, validates its complete identity, and retains those bytes.
def test_build_canonical_wheel_returns_the_single_validated_artifact() -> None:
    calls: list[tuple[str, ...]] = []
    expected: list[bytes] = []

    def runner(argv, **_kwargs):
        calls.append(tuple(argv))
        output = Path(argv[argv.index("--out-dir") + 1])
        output.mkdir()
        expected.append(_write_wheel(output / WHEEL_NAME))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    wheel = build_canonical_wheel(HostUvRunner(Path("/opt/cdh/bin/uv")), runner=runner)

    assert len(calls) == 1
    assert calls[0][0:3] == ("/opt/cdh/bin/uv", "--no-config", "build")
    assert "--offline" in calls[0]
    assert wheel.filename == WHEEL_NAME
    assert wheel.version == CDH_VERSION
    assert wheel.content == expected[0]
    assert wheel.digest == f"sha256:{hashlib.sha256(expected[0]).hexdigest()}"


# Invalid wheel metadata is rejected before its bytes can enter planning.
def test_build_canonical_wheel_rejects_distribution_identity_drift() -> None:
    def runner(argv, **_kwargs):
        output = Path(argv[argv.index("--out-dir") + 1])
        output.mkdir()
        _write_wheel(output / WHEEL_NAME, name="other-project")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(CanonicalWheelError) as raised:
        build_canonical_wheel(HostUvRunner(Path("/opt/cdh/bin/uv")), runner=runner)

    assert raised.value.diagnostics[0].code == "release.wheel.invalid"
