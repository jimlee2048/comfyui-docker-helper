"""Canonical cdh wheel construction and admission contracts."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from build import BuildBackendException

from comfyui_docker_helper.exact_ledger import CDH_VERSION
from comfyui_docker_helper.host import release_wheel
from comfyui_docker_helper.host.release_wheel import (
    CanonicalWheelError,
    build_canonical_wheel,
)

WHEEL_NAME = f"comfyui_docker_helper-{CDH_VERSION}-py3-none-any.whl"
TEST_BUILD_REQUIREMENT = "test-build-backend==1.2.3"


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


def _install_builder_fakes(
    monkeypatch: pytest.MonkeyPatch,
    wheel_writer,
) -> tuple[list[frozenset[str]], list[tuple[str, Path]]]:
    installed: list[frozenset[str]] = []
    builds: list[tuple[str, Path]] = []

    class FakeEnvironment:
        python_executable = "/owned/build-env/bin/python"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def install(self, requirements):
            installed.append(frozenset(requirements))

    class FakeBuilder:
        build_system_requires = frozenset({TEST_BUILD_REQUIREMENT})

        @classmethod
        def from_isolated_env(cls, environment, source):
            assert environment.python_executable == "/owned/build-env/bin/python"
            assert Path(source, "pyproject.toml").is_file()
            return cls()

        def get_requires_for_build(self, distribution):
            assert distribution == "wheel"
            return {"backend-wheel-requirement>=1"}

        def build(self, distribution, output):
            destination = Path(output)
            destination.mkdir()
            builds.append((distribution, destination))
            wheel_writer(destination / WHEEL_NAME)
            return str(destination / WHEEL_NAME)

    monkeypatch.setattr(release_wheel, "DefaultIsolatedEnv", FakeEnvironment)
    monkeypatch.setattr(release_wheel, "ProjectBuilder", FakeBuilder)
    return installed, builds


# The host builds one wheel, validates its complete identity, and retains those bytes.
def test_build_canonical_wheel_returns_the_single_validated_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected: list[bytes] = []

    def write_expected(path: Path) -> None:
        expected.append(_write_wheel(path))

    installed, builds = _install_builder_fakes(monkeypatch, write_expected)
    wheel = build_canonical_wheel()

    assert installed == [
        frozenset({TEST_BUILD_REQUIREMENT}),
        frozenset({"backend-wheel-requirement>=1"}),
    ]
    assert len(builds) == 1
    assert builds[0][0] == "wheel"
    assert wheel.filename == WHEEL_NAME
    assert wheel.version == CDH_VERSION
    assert wheel.content == expected[0]
    assert wheel.digest == f"sha256:{hashlib.sha256(expected[0]).hexdigest()}"


# Invalid wheel metadata is rejected before its bytes can enter planning.
def test_build_canonical_wheel_rejects_distribution_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def write_invalid(path: Path) -> None:
        _write_wheel(path, name="other-project")

    _install_builder_fakes(monkeypatch, write_invalid)

    with pytest.raises(CanonicalWheelError) as raised:
        build_canonical_wheel()

    assert raised.value.diagnostics[0].code == "release.wheel.invalid"


# Backend failures are translated into the stable release-wheel diagnostic.
def test_build_canonical_wheel_reports_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_build(_path: Path) -> None:
        raise BuildBackendException(RuntimeError("backend failed"))

    _install_builder_fakes(monkeypatch, fail_build)

    with pytest.raises(CanonicalWheelError) as raised:
        build_canonical_wheel()

    assert raised.value.diagnostics[0].code == "release.wheel.invalid"
    assert raised.value.diagnostics[0].message == "canonical wheel could not be built"


# Isolated-environment creation failures use the same stable release diagnostic.
def test_build_canonical_wheel_reports_environment_enter_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingEnvironment:
        def __enter__(self):
            raise RuntimeError("environment creation failed")

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(release_wheel, "DefaultIsolatedEnv", FailingEnvironment)

    with pytest.raises(CanonicalWheelError) as raised:
        build_canonical_wheel()

    assert raised.value.diagnostics[0].code == "release.wheel.invalid"
    assert raised.value.diagnostics[0].message == "canonical wheel could not be built"
