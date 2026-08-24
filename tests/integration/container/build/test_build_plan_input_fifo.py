"""Real POSIX FIFO admission boundary for the image-internal BuildPlan."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX FIFO")
def test_admission_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "build-plan.fifo"
    os.mkfifo(fifo)
    script = """
import sys
from comfyui_docker_helper.container.build.admission import BuildPlanInputAdmission

try:
    BuildPlanInputAdmission.from_path(
        sys.argv[1], expected_build_plan_digest="sha256:" + "a" * 64
    )
except ValueError as error:
    assert str(error) == "could not read canonical BuildPlan"
else:
    raise AssertionError("FIFO was admitted")
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(fifo)],
        check=False,
        capture_output=True,
        timeout=3,
    )

    assert result.returncode == 0, result.stderr.decode()
