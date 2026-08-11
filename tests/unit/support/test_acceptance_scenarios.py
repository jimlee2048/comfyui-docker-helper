"""Contracts for the single typed build-fixture acceptance catalog."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from collections import Counter

import pytest
from packaging.version import Version

from comfyui_docker_helper.config import load_validate_config_result
from tests.acceptance_scenarios import (
    ACCEPTANCE_SCENARIOS,
    RELEASE_PYTHON_PROFILES,
    RELEASE_SCENARIOS,
    AcceptanceProbe,
    AcceptanceScenario,
    Capability,
    Cost,
    ScenarioClass,
    required_release_probes,
)
from tests.project_paths import FIXTURES_ROOT, PROJECT_ROOT

_FIXTURE_ROOT = FIXTURES_ROOT / "comfyui-build"
_CONFIG_ROOT = _FIXTURE_ROOT / "configs"
_BUILD_HOOK_ROOT = _FIXTURE_ROOT / "build-hooks"
_PROJECT_ROOT = PROJECT_ROOT


def _document(scenario: AcceptanceScenario) -> dict[str, object]:
    with (_CONFIG_ROOT / scenario.fixture).open("rb") as file:
        return tomllib.load(file)


# The catalog owns every retained config exactly once without duplicate aliases.
def test_catalog_has_unique_total_fixture_ownership() -> None:
    ids = [scenario.id for scenario in ACCEPTANCE_SCENARIOS]
    fixtures = [scenario.fixture for scenario in ACCEPTANCE_SCENARIOS]
    retained = {path.name for path in _CONFIG_ROOT.glob("*.toml")}

    assert len(ids) == len(set(ids))
    assert len(fixtures) == len(set(fixtures))
    assert set(fixtures) == retained
    documents = [(_CONFIG_ROOT / name).read_bytes() for name in fixtures]
    assert len(documents) == len(set(documents))


# The durable matrix is six GPU-accepted images with one non-default CUDA selector.
def test_release_matrix_has_exact_dispositions_and_selector_allocation() -> None:
    assert len(RELEASE_SCENARIOS) == 6
    assert Counter(scenario.python_version for scenario in RELEASE_SCENARIOS) == {
        "3.12.13": 1,
        "3.13.14": 4,
        "3.14.6": 1,
    }
    py313 = {
        frozenset(
            capability
            for capability in scenario.capabilities
            if capability
            in {Capability.CLI, Capability.MANAGER, Capability.CUSTOM_NODES}
        )
        for scenario in RELEASE_SCENARIOS
        if scenario.python_version == "3.13.14"
    }
    assert py313 == {
        frozenset({Capability.CLI, Capability.MANAGER, Capability.CUSTOM_NODES}),
        frozenset(),
        frozenset({Capability.CLI}),
        frozenset({Capability.MANAGER, Capability.CUSTOM_NODES}),
    }
    nondefault = [
        scenario
        for scenario in ACCEPTANCE_SCENARIOS
        if (scenario.image_flavor, scenario.image_distro)
        != ("cudnn-devel", "ubuntu24.04")
    ]
    assert [
        (scenario.id, scenario.image_flavor, scenario.image_distro)
        for scenario in nondefault
    ] == [("py312-full", "cudnn-devel", "ubuntu22.04")]
    assert all(
        scenario.image_variable and scenario.context_variable
        for scenario in RELEASE_SCENARIOS
    )
    artifact_inputs = [
        (scenario.image_variable, scenario.context_variable)
        for scenario in RELEASE_SCENARIOS
    ]
    assert len(artifact_inputs) == len(set(artifact_inputs))
    assert all(Capability.GPU_AUDIO in item.capabilities for item in RELEASE_SCENARIOS)
    assert all(Cost.GPU in item.costs for item in RELEASE_SCENARIOS)


# Live release-profile gates consume each catalog-owned version once in release order.
def test_release_python_profiles_are_unique_and_version_ordered() -> None:
    expected = {scenario.python_version for scenario in RELEASE_SCENARIOS}

    assert set(RELEASE_PYTHON_PROFILES) == expected
    assert len(RELEASE_PYTHON_PROFILES) == len(set(RELEASE_PYTHON_PROFILES))
    assert tuple(sorted(RELEASE_PYTHON_PROFILES, key=Version)) == (
        RELEASE_PYTHON_PROFILES
    )


# Release probes are unique typed identities derived from each catalog capability set.
def test_release_probe_policy_is_typed_unique_and_capability_derived() -> None:
    for scenario in RELEASE_SCENARIOS:
        probes = required_release_probes(scenario)
        assert len(probes) == len(set(probes))
        assert all(isinstance(probe, AcceptanceProbe) for probe in probes)
        assert {
            AcceptanceProbe.CONTEXT,
            AcceptanceProbe.ENTRYPOINT_TOPOLOGY,
            AcceptanceProbe.IMAGE_ENVIRONMENT,
        } <= set(probes)
        assert (AcceptanceProbe.CLI_BRIDGE in probes) is (
            Capability.CLI in scenario.capabilities
        )
        assert (AcceptanceProbe.CUDA_AUDIO in probes) is (
            Capability.GPU_AUDIO in scenario.capabilities
        )


# Moving ComfyUI selectors are non-release canaries, never blocking releases.
def test_moving_inputs_are_classified_only_as_canaries() -> None:
    for scenario in ACCEPTANCE_SCENARIOS:
        comfyui = _document(scenario)["comfyui"]
        version = comfyui["version"]
        if version in {"latest", "nightly"}:
            assert scenario.classification is ScenarioClass.CANARY
        if scenario.classification is ScenarioClass.CANARY:
            assert version in {"latest", "nightly"}
            assert scenario not in RELEASE_SCENARIOS


# Unknown selections stop at the public pytest boundary with one usage diagnostic.
def test_unknown_release_scenario_reports_concise_usage_error() -> None:
    diagnostic = "unknown release acceptance scenario: does-not-exist"
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--collect-only",
            "--acceptance-scenario",
            "does-not-exist",
            "tests/smoke/test_application_acceptance_live.py",
        ),
        cwd=_PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert completed.stdout == ""
    assert completed.stderr.strip() == f"ERROR: {diagnostic}"


# Cost admission precedes artifact admission so one actionable preflight error wins.
def test_selected_release_reports_cost_before_missing_artifact_inputs() -> None:
    scenario = next(item for item in RELEASE_SCENARIOS if item.id == "py313-full")
    environment = os.environ.copy()
    assert scenario.image_variable is not None
    assert scenario.context_variable is not None
    environment.pop(scenario.image_variable, None)
    environment.pop(scenario.context_variable, None)
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--run-network",
            "--run-docker",
            "--run-slow",
            "--acceptance-scenario",
            scenario.id,
            "-k",
            scenario.id,
            "tests/smoke/test_application_acceptance_live.py",
        ),
        cwd=_PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    diagnostic = (
        "selected release acceptance scenario requires cost authorization(s): "
        f"{scenario.id}: --run-gpu"
    )
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert completed.stdout == ""
    assert completed.stderr.strip() == f"ERROR: {diagnostic}"


# Selected releases fail closed before external work when either input is absent.
@pytest.mark.parametrize("missing_kind", ["image", "context"])
def test_selected_release_scenario_requires_all_artifact_inputs(
    missing_kind: str,
) -> None:
    scenario = next(item for item in RELEASE_SCENARIOS if item.id == "py313-zero")
    inputs = {
        "image": scenario.image_variable,
        "context": scenario.context_variable,
    }
    missing = inputs[missing_kind]
    present = inputs[{"image": "context", "context": "image"}[missing_kind]]
    assert missing is not None
    assert present is not None
    environment = os.environ.copy()
    environment.pop(missing, None)
    environment[present] = "unused-artifact-input"
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--run-network",
            "--run-docker",
            "--run-gpu",
            "--run-slow",
            "--acceptance-scenario",
            scenario.id,
            "-k",
            scenario.id,
            "tests/smoke/test_application_acceptance_live.py",
        ),
        cwd=_PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    output = completed.stdout + completed.stderr
    assert (
        f"selected release scenario requires environment input(s): {missing}" in output
    )


# Selected releases must retain exactly their catalog-required real probes.
@pytest.mark.parametrize(
    ("selected_id", "expression", "expected_fragment"),
    [
        ("py313-full", "py313-full", None),
        ("py313-full", "py313-full and not cuda_audio", "missing cuda-audio"),
        (
            "py313-full",
            "py313-full and not default_entrypoint",
            "missing entrypoint-topology",
        ),
        (
            "py313-full",
            "py313-full and not image_has_exact_environment",
            "missing image-environment",
        ),
        ("py313-zero", "py313-full", "py313-zero: missing"),
    ],
)
def test_selected_release_collection_matches_required_probes(
    selected_id: str,
    expression: str,
    expected_fragment: str | None,
) -> None:
    selected = next(item for item in RELEASE_SCENARIOS if item.id == selected_id)
    environment = os.environ.copy()
    assert selected.image_variable is not None
    assert selected.context_variable is not None
    environment[selected.image_variable] = "unused-image"
    environment[selected.context_variable] = "unused-context"
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--collect-only",
            "--acceptance-scenario",
            selected_id,
            "-k",
            expression,
            "tests/smoke/test_application_acceptance_live.py",
        ),
        cwd=_PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    expected_status = 0 if expected_fragment is None else 4
    output = completed.stdout + completed.stderr
    assert completed.returncode == expected_status, output
    if expected_fragment is not None:
        assert expected_fragment in output


# Every public fixture validates offline and every local hook reference exists.
@pytest.mark.parametrize(
    "scenario", ACCEPTANCE_SCENARIOS, ids=lambda scenario: scenario.id
)
def test_fixture_is_formally_valid_with_existing_references(
    scenario: AcceptanceScenario,
) -> None:
    result = load_validate_config_result(
        _CONFIG_ROOT / scenario.fixture,
        build_hooks_dir=_BUILD_HOOK_ROOT,
    )
    config = result.config
    capabilities = scenario.capabilities
    assert config.python.version == scenario.python_version
    assert config.compute_platform.cuda.image_flavor == scenario.image_flavor
    assert config.compute_platform.cuda.image_distro == scenario.image_distro
    assert Capability.APPLICATION in capabilities
    assert (Capability.CLI in capabilities) is config.comfyui.install_cli
    assert (Capability.MANAGER in capabilities) is config.comfyui.install_manager
    assert (Capability.CUSTOM_NODES in capabilities) is bool(
        config.comfyui.custom_nodes
    )
    hooks = [
        name
        for node in config.comfyui.custom_nodes
        for name in (*node.pre_install_hooks, *node.post_install_hooks)
    ]
    assert (Capability.HOOKS in capabilities) is bool(hooks)
    assert (Capability.FILES in capabilities) is bool(config.files)
    assert all((_BUILD_HOOK_ROOT / name).is_file() for name in hooks)
    has_gpu_acceptance = Capability.GPU_AUDIO in capabilities
    assert has_gpu_acceptance is (Cost.GPU in scenario.costs)
    assert not has_gpu_acceptance or Capability.CPU_AUDIO in capabilities
    expected_costs = {Cost.NETWORK, Cost.DOCKER, Cost.SLOW}
    if has_gpu_acceptance:
        expected_costs.add(Cost.GPU)
    assert scenario.costs == expected_costs
