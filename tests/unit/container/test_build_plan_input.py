"""Authenticated BuildPlan admission for Linux image helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from comfyui_docker_helper.config.build_plan import (
    BuildPlan,
    build_plan_digest,
    dump_build_plan_json,
)
from comfyui_docker_helper.container.build_plan_input import BuildPlanInputAdmission
from tests.build_plan_support import accepted_resolution, build_plan, final_config


def _write_plan(path: Path) -> tuple[BuildPlan, str]:
    plan = build_plan(final_config(), accepted_resolution())
    path.write_bytes(dump_build_plan_json(plan))
    return plan, build_plan_digest(plan)


def test_admission_projects_authenticated_plan_for_container_consumers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "build-plan.json"
    plan, digest = _write_plan(path)

    admission = BuildPlanInputAdmission.from_path(
        path,
        expected_build_plan_digest=digest,
    )

    assert admission.comfyui_install() == (plan.application, plan.toolchain)
    assert admission.custom_node_install() == (
        plan.custom_nodes,
        plan.application,
    )
    assert admission.git_credential_routes() == plan.custom_nodes.git_credentials
    assert admission.file_downloads() == (
        plan.files,
        plan.application.paths.comfyui,
    )


def test_admission_rejects_changed_plan_under_literal_digest(tmp_path: Path) -> None:
    path = tmp_path / "build-plan.json"
    _plan, digest = _write_plan(path)
    document = json.loads(path.read_bytes())
    document["runtime"]["environment"][0]["value"] = "changed"
    path.write_text(json.dumps(document))

    with pytest.raises(ValueError, match="expected digest"):
        BuildPlanInputAdmission.from_path(
            path,
            expected_build_plan_digest=digest,
        )


def test_admission_rejects_leaf_and_ancestor_symlinks(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir(mode=0o700)
    path = context / "build-plan.json"
    _plan, digest = _write_plan(path)

    leaf_link = tmp_path / "build-plan-link.json"
    leaf_link.symlink_to(path)
    with pytest.raises(ValueError, match="could not read canonical BuildPlan"):
        BuildPlanInputAdmission.from_path(
            leaf_link,
            expected_build_plan_digest=digest,
        )

    ancestor_link = tmp_path / "context-link"
    ancestor_link.symlink_to(context, target_is_directory=True)
    with pytest.raises(ValueError, match="could not read canonical BuildPlan"):
        BuildPlanInputAdmission.from_path(
            ancestor_link / "build-plan.json",
            expected_build_plan_digest=digest,
        )


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX FIFO")
def test_admission_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "build-plan.fifo"
    os.mkfifo(fifo)
    script = """
import sys
from comfyui_docker_helper.container.build_plan_input import BuildPlanInputAdmission

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
