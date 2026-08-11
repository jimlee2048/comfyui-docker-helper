"""Execution contracts for explicit authorization of cost-marked tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.acceptance_scenarios import RELEASE_SCENARIOS
from tests.project_paths import PROJECT_ROOT

_PROJECT_ROOT = PROJECT_ROOT


def _run_probe(
    path: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
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
        env=environment,
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


def _write_release_probe(path: Path, scenario_id: str) -> str:
    token = f"EXECUTED_{scenario_id.upper().replace('-', '_')}"
    path.write_text(
        "import pytest\n"
        "from tests.acceptance_scenarios import (\n"
        "    RELEASE_SCENARIOS,\n"
        "    required_release_probes,\n"
        ")\n\n"
        "scenario = next(item for item in RELEASE_SCENARIOS "
        f"if item.id == {scenario_id!r})\n\n"
        "@pytest.mark.acceptance(probes=required_release_probes(scenario))\n"
        "@pytest.mark.parametrize('scenario', [scenario], ids=lambda item: item.id)\n"
        "def test_probe(scenario):\n"
        f"    print({token!r})\n"
    )
    return token


def _release_environment(scenario_id: str) -> dict[str, str]:
    scenario = next(item for item in RELEASE_SCENARIOS if item.id == scenario_id)
    environment = os.environ.copy()
    assert scenario.image_variable is not None
    assert scenario.context_variable is not None
    environment[scenario.image_variable] = "unused-image"
    environment[scenario.context_variable] = "unused-context"
    return environment


# A selected release uses its catalog cost declaration even when item selection
# would otherwise omit a GPU-marked proof.
def test_selected_release_requires_missing_catalog_authorization_before_execution(
    tmp_path: Path,
) -> None:
    scenario_id = "py313-full"
    probe = tmp_path / "test_release_probe.py"
    token = _write_release_probe(probe, scenario_id)

    completed = _run_probe(
        probe,
        "--acceptance-scenario",
        scenario_id,
        "--run-network",
        "--run-docker",
        "--run-slow",
        environment=_release_environment(scenario_id),
    )

    assert completed.returncode == 4, completed.stdout + completed.stderr
    assert token not in completed.stdout
    assert f"{scenario_id}: --run-gpu" in completed.stdout + completed.stderr


# The failure reports the complete missing subset without testing every flag
# combination, while the full declared set permits execution.
def test_selected_release_reports_missing_costs_and_runs_when_complete(
    tmp_path: Path,
) -> None:
    scenario_id = "py313-zero"
    probe = tmp_path / "test_release_probe.py"
    token = _write_release_probe(probe, scenario_id)
    environment = _release_environment(scenario_id)

    partial = _run_probe(
        probe,
        "--acceptance-scenario",
        scenario_id,
        "--run-network",
        environment=environment,
    )
    complete = _run_probe(
        probe,
        "--acceptance-scenario",
        scenario_id,
        "--run-network",
        "--run-docker",
        "--run-gpu",
        "--run-slow",
        environment=environment,
    )

    assert partial.returncode == 4, partial.stdout + partial.stderr
    assert token not in partial.stdout
    assert (
        f"{scenario_id}: --run-docker, --run-gpu, --run-slow"
        in partial.stdout + partial.stderr
    )
    assert complete.returncode == 0, complete.stdout + complete.stderr
    assert token in complete.stdout
    assert "1 passed" in complete.stdout


# Collection remains an offline planning operation and does not consume cost
# authorizations for the selected catalog entry.
def test_selected_release_collect_only_does_not_require_cost_authorization(
    tmp_path: Path,
) -> None:
    scenario_id = "py313-full"
    probe = tmp_path / "test_release_probe.py"
    token = _write_release_probe(probe, scenario_id)

    completed = _run_probe(
        probe,
        "--collect-only",
        "--acceptance-scenario",
        scenario_id,
        environment=_release_environment(scenario_id),
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert token not in completed.stdout
    assert "1 test collected" in completed.stdout


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
