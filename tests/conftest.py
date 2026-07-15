"""Shared pytest fixtures and explicit cost-test authorization."""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from tests.acceptance_scenarios import (
    RELEASE_SCENARIOS,
    AcceptanceScenario,
    ScenarioClass,
)

_COST_AUTHORIZATIONS = {
    "network": "--run-network",
    "docker": "--run-docker",
    "gpu": "--run-gpu",
    "slow": "--run-slow",
}


def _selected_release_ids(config: pytest.Config) -> set[str]:
    return set(config.getoption("--acceptance-scenario"))


def _release_scenario_parameter(item: pytest.Item) -> AcceptanceScenario | None:
    if item.get_closest_marker("acceptance") is None:
        return None
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return None
    scenario = callspec.params.get("scenario")
    if not isinstance(scenario, AcceptanceScenario):
        return None
    if scenario.classification is not ScenarioClass.RELEASE:
        return None
    return scenario


def pytest_addoption(parser: pytest.Parser) -> None:
    cost = parser.getgroup("cost authorization")
    for marker, option in _COST_AUTHORIZATIONS.items():
        cost.addoption(
            option,
            action="store_true",
            default=False,
            help=f"authorize tests marked {marker}",
        )
    acceptance = parser.getgroup("acceptance selection")
    acceptance.addoption(
        "--acceptance-scenario",
        action="append",
        default=[],
        metavar="ID",
        help="select a durable release scenario (repeatable)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    selected = _selected_release_ids(config)
    if selected:
        known = {scenario.id for scenario in RELEASE_SCENARIOS}
        if unknown := selected - known:
            values = ", ".join(sorted(unknown))
            raise pytest.UsageError(f"unknown release acceptance scenario: {values}")
    for item in items:
        missing = [
            option
            for marker, option in _COST_AUTHORIZATIONS.items()
            if item.get_closest_marker(marker) is not None
            and not config.getoption(option)
        ]
        if missing:
            item.add_marker(
                pytest.mark.skip(
                    reason=f"requires explicit authorization: {' '.join(missing)}"
                )
            )


def pytest_collection_finish(session: pytest.Session) -> None:
    selected = _selected_release_ids(session.config)
    if not selected:
        return
    surviving = {
        scenario.id: scenario
        for item in session.items
        if (scenario := _release_scenario_parameter(item)) is not None
        and scenario.id in selected
    }
    if missing := selected - surviving.keys():
        values = ", ".join(sorted(missing))
        raise pytest.UsageError(
            "selected release acceptance scenario has no collected acceptance "
            f"item: {values}"
        )
    missing_inputs = {
        name
        for scenario in surviving.values()
        for name in (scenario.image_variable, scenario.context_variable)
        if name is not None and not os.environ.get(name)
    }
    if missing_inputs:
        values = ", ".join(sorted(missing_inputs))
        raise pytest.UsageError(
            f"selected release scenario requires environment input(s): {values}"
        )


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()
