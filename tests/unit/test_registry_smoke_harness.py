"""Deterministic checks for the Registry image smoke process harness."""

from __future__ import annotations

import subprocess

from tests.smoke.test_registry_image_live import _REGISTRY_PROBE


def test_registry_image_probe_has_valid_posix_shell_syntax() -> None:
    completed = subprocess.run(
        ["/bin/sh", "-n"],
        input=_REGISTRY_PROBE,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
