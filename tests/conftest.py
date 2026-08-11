"""Shared pytest fixtures and explicit cost-test authorization."""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from tests.acceptance_scenarios import (
    RELEASE_SCENARIOS,
    AcceptanceProbe,
    AcceptanceScenario,
    Cost,
    ScenarioClass,
    required_release_probes,
)

_COST_AUTHORIZATIONS = {
    Cost.NETWORK: "--run-network",
    Cost.DOCKER: "--run-docker",
    Cost.GPU: "--run-gpu",
    Cost.SLOW: "--run-slow",
}

_VALIDATED_RELEASE_SCENARIOS = pytest.StashKey[dict[str, AcceptanceScenario]]()

_MAX_NODE_ID_CHARACTERS = 4096


def _validate_node_id_lengths(items: list[pytest.Item]) -> None:
    oversized = [
        len(item.nodeid) for item in items if len(item.nodeid) > _MAX_NODE_ID_CHARACTERS
    ]
    if not oversized:
        return
    raise pytest.UsageError(
        f"{len(oversized)} collected test node ID(s) exceed the "
        f"{_MAX_NODE_ID_CHARACTERS}-character limit; longest is "
        f"{max(oversized)} characters. Use concise explicit parametrization IDs."
    )


def _selected_release_ids(config: pytest.Config) -> set[str]:
    return set(config.getoption("--acceptance-scenario"))


def _selected_release_scenarios(
    config: pytest.Config,
) -> dict[str, AcceptanceScenario]:
    scenarios_by_id = {scenario.id: scenario for scenario in RELEASE_SCENARIOS}
    selected = _selected_release_ids(config)
    if unknown := selected - scenarios_by_id.keys():
        values = ", ".join(sorted(unknown))
        raise pytest.UsageError(f"unknown release acceptance scenario: {values}")
    return {scenario_id: scenarios_by_id[scenario_id] for scenario_id in selected}


def _release_scenario_parameter(item: pytest.Item) -> AcceptanceScenario | None:
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return None
    scenario = callspec.params.get("scenario")
    if not isinstance(scenario, AcceptanceScenario):
        return None
    if scenario.classification is not ScenarioClass.RELEASE:
        return None
    if not any(scenario is candidate for candidate in RELEASE_SCENARIOS):
        return None
    return scenario


def _function_acceptance_probes(item: pytest.Item) -> tuple[AcceptanceProbe, ...]:
    markers = tuple(
        marker
        for node, marker in item.iter_markers_with_node(name="acceptance")
        if node is item
    )
    if not markers:
        return ()
    if len(markers) != 1:
        raise pytest.UsageError(
            f"acceptance item has multiple function probe markers: {item.nodeid}"
        )
    probes = markers[0].kwargs.get("probes")
    if (
        not isinstance(probes, tuple)
        or not probes
        or any(not isinstance(probe, AcceptanceProbe) for probe in probes)
        or len(probes) != len(set(probes))
    ):
        raise pytest.UsageError(
            f"acceptance item has invalid function probes: {item.nodeid}"
        )
    return probes


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


def pytest_configure(config: pytest.Config) -> None:
    selected_scenarios = _selected_release_scenarios(config)
    config.stash[_VALIDATED_RELEASE_SCENARIOS] = selected_scenarios
    if not selected_scenarios or config.getoption("collectonly"):
        return
    missing_by_scenario = {
        scenario_id: tuple(
            sorted(
                _COST_AUTHORIZATIONS[capability]
                for capability in scenario.costs
                if not config.getoption(_COST_AUTHORIZATIONS[capability])
            )
        )
        for scenario_id, scenario in sorted(selected_scenarios.items())
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


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    _validate_node_id_lengths(items)
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
    selected_scenarios = session.config.stash[_VALIDATED_RELEASE_SCENARIOS]
    if not selected_scenarios:
        return
    actual_by_scenario = {scenario_id: set() for scenario_id in selected_scenarios}
    for item in session.items:
        scenario = _release_scenario_parameter(item)
        if scenario is None or scenario.id not in selected_scenarios:
            continue
        actual_by_scenario[scenario.id].update(_function_acceptance_probes(item))

    mismatches = []
    for scenario_id, scenario in sorted(selected_scenarios.items()):
        expected = set(required_release_probes(scenario))
        actual = actual_by_scenario[scenario_id]
        details = []
        if missing := expected - actual:
            details.append(
                "missing " + ", ".join(sorted(probe.value for probe in missing))
            )
        if unexpected := actual - expected:
            details.append(
                "unexpected " + ", ".join(sorted(probe.value for probe in unexpected))
            )
        if details:
            mismatches.append(f"{scenario_id}: {', '.join(details)}")
    if mismatches:
        raise pytest.UsageError(
            "selected release acceptance probes do not match collected items: "
            + "; ".join(mismatches)
        )

    missing_inputs = {
        name
        for scenario in selected_scenarios.values()
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
