"""Build and validate one canonical wheel from installed package resources."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.utils import canonicalize_name, parse_wheel_filename

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticError
from comfyui_docker_helper.exact_ledger import CDH_VERSION
from comfyui_docker_helper.host.uv_runner import HostUvRunner
from comfyui_docker_helper.release_artifacts import (
    CanonicalWheel,
    release_projection_files,
)

_DISTRIBUTION = "comfyui-docker-helper"
_WHEEL_FILENAME = f"comfyui_docker_helper-{CDH_VERSION}-py3-none-any.whl"


class CanonicalWheelError(DiagnosticError):
    """The installed distribution could not produce its canonical wheel."""


def build_canonical_wheel(
    uv: HostUvRunner,
    *,
    runner=subprocess.run,
) -> CanonicalWheel:
    """Build exactly once in owned temporary storage and return verified bytes."""
    try:
        with tempfile.TemporaryDirectory(prefix="cdh-canonical-wheel-") as raw:
            root = Path(raw)
            source = root / "source"
            output = root / "dist"
            for item in release_projection_files():
                target = source.joinpath(*item.relative_path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item.source_path.read_bytes())
            completed = runner(
                uv.argv(
                    (
                        "build",
                        "--wheel",
                        "--offline",
                        "--no-python-downloads",
                        "--python",
                        sys.executable,
                        "--out-dir",
                        str(output),
                        str(source),
                    )
                ),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if completed.returncode != 0:
                raise _wheel_error("canonical wheel build failed")
            wheels = tuple(sorted(output.glob("*.whl")))
            if len(wheels) != 1 or wheels[0].name != _WHEEL_FILENAME:
                raise _wheel_error("canonical wheel filename is invalid")
            content = wheels[0].read_bytes()
    except CanonicalWheelError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise _wheel_error("canonical wheel could not be built") from error

    _validate_wheel(_WHEEL_FILENAME, content)
    return CanonicalWheel(
        filename=_WHEEL_FILENAME,
        version=CDH_VERSION,
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        content=content,
    )


def _validate_wheel(filename: str, content: bytes) -> None:
    try:
        name, version, build, tags = parse_wheel_filename(filename)
        if (
            canonicalize_name(name) != _DISTRIBUTION
            or str(version) != CDH_VERSION
            or build != ()
            or {str(tag) for tag in tags} != {"py3-none-any"}
        ):
            raise _wheel_error("canonical wheel identity is invalid")
        with tempfile.TemporaryDirectory(prefix="cdh-wheel-validate-") as raw:
            path = Path(raw) / filename
            path.write_bytes(content)
            with zipfile.ZipFile(path) as archive:
                prefix = f"comfyui_docker_helper-{CDH_VERSION}.dist-info/"
                metadata = BytesParser().parsebytes(archive.read(prefix + "METADATA"))
                wheel = BytesParser().parsebytes(archive.read(prefix + "WHEEL"))
    except CanonicalWheelError:
        raise
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as error:
        raise _wheel_error("canonical wheel metadata is invalid") from error
    if (
        canonicalize_name(metadata.get("Name", "")) != _DISTRIBUTION
        or metadata.get("Version") != CDH_VERSION
        or wheel.get("Root-Is-Purelib") != "true"
        or wheel.get_all("Tag", []) != ["py3-none-any"]
    ):
        raise _wheel_error("canonical wheel metadata is invalid")


def _wheel_error(message: str) -> CanonicalWheelError:
    return CanonicalWheelError(
        (Diagnostic(("release", "wheel"), "release.wheel.invalid", message),)
    )
