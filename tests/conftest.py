"""Shared pytest fixtures and explicit cost-test authorization."""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from tests.acceptance_scenarios import (
    RELEASE_SCENARIOS,
    AcceptanceScenario,
    Cost,
    ScenarioClass,
)

_COST_AUTHORIZATIONS = {
    Cost.NETWORK: "--run-network",
    Cost.DOCKER: "--run-docker",
    Cost.GPU: "--run-gpu",
    Cost.SLOW: "--run-slow",
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
    for capability, option in _COST_AUTHORIZATIONS.items():
        cost.addoption(
            option,
            action="store_true",
            default=False,
            help=f"authorize tests marked {capability.value}",
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
        scenarios_by_id = {scenario.id: scenario for scenario in RELEASE_SCENARIOS}
        known = scenarios_by_id.keys()
        if unknown := selected - known:
            values = ", ".join(sorted(unknown))
            raise pytest.UsageError(f"unknown release acceptance scenario: {values}")
        if not config.getoption("collectonly"):
            missing_by_scenario = {
                scenario_id: tuple(
                    sorted(
                        _COST_AUTHORIZATIONS[capability]
                        for capability in scenarios_by_id[scenario_id].costs
                        if not config.getoption(_COST_AUTHORIZATIONS[capability])
                    )
                )
                for scenario_id in sorted(selected)
            }
            missing_by_scenario = {
                scenario_id: options
                for scenario_id, options in missing_by_scenario.items()
                if options
            }
            if missing_by_scenario:
                details = "; ".join(
                    f"{scenario_id}: {', '.join(options)}"
                    for scenario_id, options in missing_by_scenario.items()
                )
                raise pytest.UsageError(
                    "selected release acceptance scenario requires cost "
                    f"authorization(s): {details}"
                )
    for item in items:
        missing = [
            option
            for capability, option in _COST_AUTHORIZATIONS.items()
            if item.get_closest_marker(capability.value) is not None
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
