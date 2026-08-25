"""Build and validate one canonical wheel from installed package resources."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from build import (
    BuildBackendException,
    BuildException,
    FailedProcessError,
    ProjectBuilder,
)
from build.env import DefaultIsolatedEnv
from packaging.utils import canonicalize_name, parse_wheel_filename

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticError
from comfyui_docker_helper.release_artifacts import (
    CanonicalWheel,
    release_projection_files,
)
from comfyui_docker_helper.version import package_version

_DISTRIBUTION = "comfyui-docker-helper"


class CanonicalWheelError(DiagnosticError):
    """The installed distribution could not produce its canonical wheel."""


def build_canonical_wheel() -> CanonicalWheel:
    """Build exactly once in owned temporary storage and return verified bytes."""
    version = package_version()
    wheel_filename = f"comfyui_docker_helper-{version}-py3-none-any.whl"
    try:
        with tempfile.TemporaryDirectory(prefix="cdh-canonical-wheel-") as raw:
            root = Path(raw)
            source = root / "source"
            output = root / "dist"
            for item in release_projection_files():
                target = source.joinpath(*item.relative_path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item.source_path.read_bytes())
            with DefaultIsolatedEnv() as environment:
                builder = ProjectBuilder.from_isolated_env(environment, source)
                environment.install(builder.build_system_requires)
                environment.install(builder.get_requires_for_build("wheel"))
                builder.build("wheel", output)
            wheels = tuple(sorted(output.glob("*.whl")))
            if len(wheels) != 1 or wheels[0].name != wheel_filename:
                raise _wheel_error("canonical wheel filename is invalid")
            content = wheels[0].read_bytes()
    except CanonicalWheelError:
        raise
    except (
        BuildBackendException,
        BuildException,
        FailedProcessError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        raise _wheel_error("canonical wheel could not be built") from error

    _validate_wheel(wheel_filename, content, expected_version=version)
    return CanonicalWheel(
        filename=wheel_filename,
        version=version,
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        content=content,
    )


def _validate_wheel(filename: str, content: bytes, *, expected_version: str) -> None:
    try:
        name, version, build, tags = parse_wheel_filename(filename)
        if (
            canonicalize_name(name) != _DISTRIBUTION
            or str(version) != expected_version
            or build != ()
            or {str(tag) for tag in tags} != {"py3-none-any"}
        ):
            raise _wheel_error("canonical wheel identity is invalid")
        with tempfile.TemporaryDirectory(prefix="cdh-wheel-validate-") as raw:
            path = Path(raw) / filename
            path.write_bytes(content)
            with zipfile.ZipFile(path) as archive:
                prefix = f"comfyui_docker_helper-{expected_version}.dist-info/"
                metadata = BytesParser().parsebytes(archive.read(prefix + "METADATA"))
                wheel = BytesParser().parsebytes(archive.read(prefix + "WHEEL"))
    except CanonicalWheelError:
        raise
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as error:
        raise _wheel_error("canonical wheel metadata is invalid") from error
    if (
        canonicalize_name(metadata.get("Name", "")) != _DISTRIBUTION
        or metadata.get("Version") != expected_version
        or wheel.get("Root-Is-Purelib") != "true"
        or wheel.get_all("Tag", []) != ["py3-none-any"]
    ):
        raise _wheel_error("canonical wheel metadata is invalid")


def _wheel_error(message: str) -> CanonicalWheelError:
    return CanonicalWheelError(
        (Diagnostic(("release", "wheel"), "release.wheel.invalid", message),)
    )
