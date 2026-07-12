"""Exact cdh-owned host uv runner."""

from __future__ import annotations

import os
import re
import subprocess
import sysconfig
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticError
from comfyui_docker_helper.exact_ledger import UV_VERSION

EXPECTED_UV_VERSION = UV_VERSION
_UV_VERSION_PATTERN = re.compile(r"^uv (?P<version>\S+)(?: \([^\n]+\))?$")

type UvLocator = Callable[[], str]


class UvVersionRunner(Protocol):
    """Subprocess seam used only to verify the located uv executable."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        encoding: str,
    ) -> subprocess.CompletedProcess[str]: ...


class HostUvError(DiagnosticError):
    """Expected failure while locating or verifying cdh-owned uv."""


@dataclass(frozen=True, slots=True)
class HostUvRunner:
    """Verified uv executable used for host-side resolution."""

    executable: Path
    version: str = EXPECTED_UV_VERSION

    def argv(self, arguments: Sequence[str]) -> tuple[str, ...]:
        """Build a no-config command using only the verified absolute path."""
        if not arguments:
            raise ValueError("uv arguments must not be empty")
        return (str(self.executable), "--no-config", *arguments)


def locate_host_uv(
    *,
    locator: UvLocator | None = None,
    version_runner: UvVersionRunner = subprocess.run,
) -> HostUvRunner:
    """Locate and verify the exact uv shipped in the cdh environment."""
    scripts_directory = Path(sysconfig.get_path("scripts")).resolve()
    uv_executable_name = f"uv{sysconfig.get_config_var('EXE') or ''}"
    selected_locator = locator or (lambda: str(scripts_directory / uv_executable_name))
    try:
        candidate = Path(selected_locator())
    except (FileNotFoundError, OSError) as error:
        raise _host_uv_error(
            "host.uv.not-found",
            "cdh-owned uv was not found; reinstall cdh.",
        ) from error

    if not candidate.is_absolute():
        raise _host_uv_error(
            "host.uv.not-absolute",
            "cdh-owned uv did not resolve to an absolute path; reinstall cdh.",
        )

    if candidate.parent.resolve() != scripts_directory:
        raise _host_uv_error(
            "host.uv.not-owned",
            "uv is not owned by the current cdh environment; reinstall cdh.",
        )

    try:
        executable = candidate.resolve(strict=True)
    except OSError as error:
        raise _host_uv_error(
            "host.uv.not-found",
            "cdh-owned uv was not found; reinstall cdh.",
        ) from error
    if executable.parent != scripts_directory:
        raise _host_uv_error(
            "host.uv.not-owned",
            "uv is not owned by the current cdh environment; reinstall cdh.",
        )
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise _host_uv_error(
            "host.uv.not-executable",
            "cdh-owned uv is not executable; reinstall cdh.",
        )

    try:
        completed = version_runner(
            (str(executable), "--version"),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as error:
        raise _host_uv_error(
            "host.uv.execution-failed",
            "cdh-owned uv could not run; reinstall cdh.",
        ) from error

    output = completed.stdout.strip()
    match = _UV_VERSION_PATTERN.fullmatch(output)
    if completed.returncode != 0 or match is None:
        raise _host_uv_error(
            "host.uv.invalid-version-output",
            "cdh-owned uv returned invalid version output; reinstall cdh.",
        )
    actual_version = match.group("version")
    if actual_version != EXPECTED_UV_VERSION:
        raise _host_uv_error(
            "host.uv.version-mismatch",
            (
                f"cdh requires uv {EXPECTED_UV_VERSION}, found {actual_version}; "
                "reinstall cdh."
            ),
        )

    return HostUvRunner(executable=executable)


def _host_uv_error(code: str, message: str) -> HostUvError:
    return HostUvError(
        (
            Diagnostic(
                path=("host", "uv"),
                code=code,
                message=message,
            ),
        )
    )
