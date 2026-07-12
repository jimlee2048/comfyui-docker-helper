"""Tests for the exact cdh-owned host uv runner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import comfyui_docker_helper.host.uv_runner as uv_runner_module
from comfyui_docker_helper.host.uv_runner import (
    EXPECTED_UV_VERSION,
    HostUvError,
    locate_host_uv,
)


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def _set_owned_scripts(
    monkeypatch: pytest.MonkeyPatch, scripts_directory: Path
) -> None:
    scripts_directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        uv_runner_module.sysconfig,
        "get_path",
        lambda name: str(scripts_directory),
    )


def _completed(
    args: object,
    *,
    stdout: str = f"uv {EXPECTED_UV_VERSION} (x86_64-unknown-linux-gnu)\n",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout)


def test_locator_verifies_exact_absolute_owned_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_owned_scripts(monkeypatch, tmp_path)
    executable = _make_executable(tmp_path / "uv")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def version_runner(
        args: tuple[str, ...], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return _completed(args)

    runner = locate_host_uv(
        locator=lambda: str(executable), version_runner=version_runner
    )

    assert runner.executable == executable.resolve()
    assert runner.version == EXPECTED_UV_VERSION
    assert calls == [
        (
            (str(executable.resolve()), "--version"),
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
            },
        )
    ]
    assert runner.argv(("pip", "compile", "requirements.in")) == (
        str(executable.resolve()),
        "--no-config",
        "pip",
        "compile",
        "requirements.in",
    )


def test_relative_locator_result_is_rejected_without_execution() -> None:
    called = False

    def version_runner(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return _completed(args)

    with pytest.raises(HostUvError) as captured:
        locate_host_uv(locator=lambda: "uv", version_runner=version_runner)

    assert captured.value.diagnostics[0].code == "host.uv.not-absolute"
    assert called is False


def test_default_locator_never_uses_path_base_or_user_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned_scripts = tmp_path / "cdh-environment" / "bin"
    fallback_scripts = tmp_path / "fallback" / "bin"
    _set_owned_scripts(monkeypatch, owned_scripts)
    fallback = _make_executable(fallback_scripts / "uv")
    monkeypatch.setenv("PATH", str(fallback_scripts))

    assert fallback.is_file()

    with pytest.raises(HostUvError) as captured:
        locate_host_uv()

    assert captured.value.diagnostics[0].code == "host.uv.not-found"


def test_exact_binary_outside_current_environment_is_rejected_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned_scripts = tmp_path / "cdh-environment" / "bin"
    outside = _make_executable(tmp_path / "base" / "bin" / "uv")
    _set_owned_scripts(monkeypatch, owned_scripts)
    called = False

    def version_runner(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return _completed(args)

    with pytest.raises(HostUvError) as captured:
        locate_host_uv(locator=lambda: str(outside), version_runner=version_runner)

    assert captured.value.diagnostics[0].code == "host.uv.not-owned"
    assert called is False


def test_owned_name_symlink_to_external_binary_is_rejected_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned_scripts = tmp_path / "cdh-environment" / "bin"
    outside = _make_executable(tmp_path / "base" / "bin" / "uv")
    _set_owned_scripts(monkeypatch, owned_scripts)
    (owned_scripts / "uv").symlink_to(outside)
    called = False

    def version_runner(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return _completed(args)

    with pytest.raises(HostUvError) as captured:
        locate_host_uv(version_runner=version_runner)

    assert captured.value.diagnostics[0].code == "host.uv.not-owned"
    assert called is False


def test_owned_name_symlink_to_internal_binary_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned_scripts = tmp_path / "cdh-environment" / "bin"
    _set_owned_scripts(monkeypatch, owned_scripts)
    internal = _make_executable(owned_scripts / "uv-real")
    (owned_scripts / "uv").symlink_to(internal)

    runner = locate_host_uv(
        version_runner=lambda args, **kwargs: _completed(args),
    )

    assert runner.executable == internal.resolve()


@pytest.mark.parametrize(
    ("stdout", "returncode", "code"),
    [
        ("uv 0.11.27 (x86_64-unknown-linux-gnu)\n", 0, "host.uv.version-mismatch"),
        ("unexpected\n", 0, "host.uv.invalid-version-output"),
        ("", 2, "host.uv.invalid-version-output"),
    ],
)
def test_invalid_or_wrong_version_has_short_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
    code: str,
) -> None:
    _set_owned_scripts(monkeypatch, tmp_path)
    executable = _make_executable(tmp_path / "uv")

    def version_runner(
        args: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return _completed(args, stdout=stdout, returncode=returncode)

    with pytest.raises(HostUvError) as captured:
        locate_host_uv(locator=lambda: str(executable), version_runner=version_runner)

    diagnostic = captured.value.diagnostics[0]
    assert diagnostic.path == ("host", "uv")
    assert diagnostic.code == code
    assert len(diagnostic.message) < 100


def test_real_installed_runner_is_exact_and_absolute() -> None:
    runner = locate_host_uv()

    assert runner.executable.is_absolute()
    assert runner.version == EXPECTED_UV_VERSION
    assert runner.argv(("--version",))[0] == os.fspath(runner.executable)


def test_runner_rejects_empty_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_owned_scripts(monkeypatch, tmp_path)
    executable = _make_executable(tmp_path / "uv")
    runner = locate_host_uv(
        locator=lambda: str(executable),
        version_runner=lambda args, **kwargs: _completed(args),
    )

    with pytest.raises(ValueError, match="must not be empty"):
        runner.argv(())
