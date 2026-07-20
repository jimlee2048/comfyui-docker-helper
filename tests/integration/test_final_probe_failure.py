"""Final-probe subprocess failure and manifest-publication ordering."""

from __future__ import annotations

import venv
from pathlib import Path

from tests.unit.test_build_plan import accepted_resolution, build_plan, final_config
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.config.build_plan import build_plan_digest
from comfyui_docker_helper.container import cli as container_cli
from comfyui_docker_helper.container import final_manifest as final_manifest_service
from comfyui_docker_helper.container.build_plan_input import BuildPlanInputAdmission


# A real failing probe must stop the command before any named evidence exists.
def test_probe_failure_publishes_no_final_or_partial_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = build_plan(final_config(), accepted_resolution())
    virtual_env = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(virtual_env)
    workspace = tmp_path / "workspace"
    comfyui = workspace / "ComfyUI"
    comfyui.joinpath("comfy").mkdir(parents=True)
    comfyui.joinpath("folder_paths.py").write_text("")
    build_directory = tmp_path / "build"
    build_directory.mkdir()

    monkeypatch.setattr(
        container_cli,
        "_admission",
        lambda _digest: BuildPlanInputAdmission(plan),
    )
    monkeypatch.setattr(
        final_manifest_service,
        "_MANIFEST_PATH",
        build_directory / "manifest.json",
    )
    monkeypatch.setattr(
        final_manifest_service,
        "_observe_final_manifest",
        lambda observed, *, runtime: final_manifest_service._run_final_core_probe(
            observed.final_probe,
            runtime,
        ),
    )
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("COMFYUI_PATH", str(comfyui))
    monkeypatch.setenv("VIRTUAL_ENV", str(virtual_env))

    result = CliRunner().invoke(
        app,
        [
            "container",
            "emit-final-manifest",
            "--build-plan-digest",
            build_plan_digest(plan),
        ],
    )

    assert result.exit_code != 0
    assert tuple(build_directory.iterdir()) == ()
