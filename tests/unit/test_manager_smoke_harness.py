"""Deterministic checks for the Manager image smoke process harness."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest
from tests.smoke.test_manager_image_live import (
    _BOUNDED_APPLICATION_CLEANUP,
    _DISABLED_PROBE,
    _ENABLED_PROBE,
)


@pytest.mark.parametrize("probe", [_ENABLED_PROBE, _DISABLED_PROBE])
def test_manager_image_probe_has_valid_posix_shell_syntax(probe: str) -> None:
    completed = subprocess.run(
        ["/bin/sh", "-n"],
        input=probe,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr


def test_cleanup_preserves_failure_status_and_reaps_normal_child() -> None:
    script = (
        "set -eu\n"
        "sleep 60 &\n"
        "application_pid=$!\n"
        f"{_BOUNDED_APPLICATION_CLEANUP}\n"
        "exit 7\n"
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )

    assert completed.returncode == 7, completed.stderr


def test_cleanup_preserves_term_signal_status() -> None:
    script = (
        "set -eu\n"
        "sleep 60 &\n"
        "application_pid=$!\n"
        f"{_BOUNDED_APPLICATION_CLEANUP}\n"
        "kill -TERM $$\n"
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )

    assert completed.returncode == 143, completed.stderr


def test_cleanup_kills_and_reaps_term_ignoring_child_within_bounds(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    child = f'trap "" TERM; : > {shlex.quote(str(ready))}; while :; do :; done'
    script = (
        "set -eu\n"
        f"/bin/sh -c {shlex.quote(child)} &\n"
        "application_pid=$!\n"
        f"while test ! -e {shlex.quote(str(ready))}; do sleep 0.01; done\n"
        f"{_BOUNDED_APPLICATION_CLEANUP}\n"
        "trap - EXIT INT TERM\n"
        "bounded_reap_application 1 1\n"
        'if kill -0 "$application_pid" 2>/dev/null; then exit 9; fi\n'
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def test_bounded_cleanup_exhaustion_fails_without_reaping(tmp_path: Path) -> None:
    reaped = tmp_path / "reaped"
    script = (
        "set -eu\n"
        "application_pid=999999\n"
        f"{_BOUNDED_APPLICATION_CLEANUP}\n"
        "application_is_non_zombie() { return 0; }\n"
        "signal_application() { return 0; }\n"
        f"reap_application() {{ : > {shlex.quote(str(reaped))}; }}\n"
        "complete_application_probe 0 0\n"
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )

    assert completed.returncode != 0
    assert not reaped.exists()


def test_exit_trap_turns_success_into_cleanup_failure() -> None:
    script = (
        "set -eu\n"
        "application_pid=999999\n"
        f"{_BOUNDED_APPLICATION_CLEANUP}\n"
        "bounded_reap_application() { return 1; }\n"
        ":\n"
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )

    assert completed.returncode != 0


def test_enabled_probe_only_reaps_after_non_zombie_observation() -> None:
    assert _ENABLED_PROBE.count('wait "$application_pid"') == 1
    assert (
        "if application_is_non_zombie; then\n"
        "    return 1\n"
        "  fi\n"
        "  reap_application" in _ENABLED_PROBE
    )
