"""Execution contracts for explicit authorization of cost-marked tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parents[2]


def _run_probe(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-c",
            str(_PROJECT_ROOT / "pyproject.toml"),
            "-q",
            "-s",
            *arguments,
            str(path),
        ),
        cwd=_PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


# Every cost marker is inert by default and runs only with its matching opt-in.
@pytest.mark.parametrize(
    ("marker", "option"),
    [
        ("network", "--run-network"),
        ("docker", "--run-docker"),
        ("gpu", "--run-gpu"),
        ("slow", "--run-slow"),
    ],
)
def test_cost_marker_requires_matching_authorization(
    tmp_path: Path, marker: str, option: str
) -> None:
    probe = tmp_path / f"test_{marker}_probe.py"
    token = f"EXECUTED_{marker.upper()}"
    probe.write_text(
        "import pytest\n\n"
        f"@pytest.mark.{marker}\n"
        "def test_probe():\n"
        f"    print({token!r})\n"
    )

    denied = _run_probe(probe)
    authorized = _run_probe(probe, option)

    assert denied.returncode == 0, denied.stdout + denied.stderr
    assert token not in denied.stdout
    assert "1 skipped" in denied.stdout
    assert authorized.returncode == 0, authorized.stdout + authorized.stderr
    assert token in authorized.stdout
    assert "1 passed" in authorized.stdout


# A composed test requires every authorization attached to its marker set.
def test_composed_cost_markers_require_all_matching_authorizations(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "test_composed_probe.py"
    probe.write_text(
        "import pytest\n\n"
        "@pytest.mark.network\n"
        "@pytest.mark.docker\n"
        "def test_probe():\n"
        "    print('EXECUTED_COMPOSED')\n"
    )

    denied = _run_probe(probe)
    partial = _run_probe(probe, "--run-network")
    authorized = _run_probe(probe, "--run-network", "--run-docker")

    for result in (denied, partial):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "EXECUTED_COMPOSED" not in result.stdout
        assert "1 skipped" in result.stdout
    assert authorized.returncode == 0, authorized.stdout + authorized.stderr
    assert "EXECUTED_COMPOSED" in authorized.stdout
    assert "1 passed" in authorized.stdout
