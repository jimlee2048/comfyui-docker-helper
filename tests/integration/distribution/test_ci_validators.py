"""Real Git and process-boundary coverage for the CI validators."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests.project_paths import PROJECT_ROOT

ADMISSION_TOOL = PROJECT_ROOT / "tools/ci/admit_package_build.py"
DISTRIBUTION_TOOL = PROJECT_ROOT / "tools/ci/verify_distributions.py"


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_project(project_root: Path) -> None:
    projection = (
        project_root
        / "src/comfyui_docker_helper/resources/release-projection/pyproject.toml"
    )
    projection.parent.mkdir(parents=True)
    metadata = '[project]\nname = "comfyui-docker-helper"\nversion = "1.2.3"\n'
    (project_root / "pyproject.toml").write_text(metadata, encoding="utf-8")
    projection.write_text(metadata, encoding="utf-8")


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "CI Test")
    _git(repository, "config", "user.email", "ci@example.invalid")
    _write_project(repository)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "release")
    tagged_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v1.2.3")
    (repository / "main.txt").write_text("main advanced\n", encoding="utf-8")
    _git(repository, "add", "main.txt")
    _git(repository, "commit", "-m", "advance main")
    main_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", "refs/remotes/origin/main", main_commit)
    return repository, tagged_commit, main_commit


def _run_admission(
    repository: Path, output: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ADMISSION_TOOL)],
        cwd=repository,
        env={**os.environ, **environment, "GITHUB_OUTPUT": str(output)},
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("case", "expected_success"),
    [
        ("candidate", True),
        ("candidate-peel-drift", False),
        ("formal-ancestor", True),
        ("formal-non-ancestor", False),
    ],
)
def test_admission_cli_binds_real_tag_and_main_history(
    tmp_path: Path, case: str, expected_success: bool
) -> None:
    repository, tagged_commit, main_commit = _repository(tmp_path)
    output = tmp_path / "github-output"
    environment = {
        "EVENT_NAME": "push",
        "GITHUB_REF": "refs/tags/v1.2.3",
        "GITHUB_RUN_ATTEMPT": "3",
        "INPUT_MODE": "",
        "REF_NAME": "v1.2.3",
    }
    if not expected_success:
        output.write_text("existing-output\n", encoding="utf-8")

    if case == "candidate-peel-drift":
        _git(repository, "checkout", "--detach", main_commit)
    else:
        _git(repository, "checkout", "--detach", tagged_commit)
    if case.startswith("formal"):
        environment.update(
            {
                "EVENT_ACTION": "published",
                "EVENT_NAME": "release",
                "INPUT_MODE": "formal",
                "RELEASE_PRERELEASE": "false",
                "RELEASE_TAG": "v1.2.3",
            }
        )
    if case == "formal-non-ancestor":
        tree = _git(repository, "rev-parse", f"{main_commit}^{{tree}}")
        unrelated = _git(repository, "commit-tree", tree, "-m", "unrelated main")
        _git(repository, "update-ref", "refs/remotes/origin/main", unrelated)

    completed = _run_admission(repository, output, environment)

    assert (completed.returncode == 0) is expected_success
    if expected_success:
        expected_kind = "formal" if case.startswith("formal") else "candidate"
        assert output.read_text(encoding="utf-8") == (
            f"artifact_name={expected_kind}-distributions-attempt-3\nversion=1.2.3\n"
        )
    else:
        assert output.read_text(encoding="utf-8") == "existing-output\n"


def test_distribution_verifier_cli_accepts_valid_pair(tmp_path: Path) -> None:
    _write_project(tmp_path)
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "comfyui_docker_helper-1.2.3-py3-none-any.whl").write_bytes(b"")
    package_info = tmp_path / "PKG-INFO"
    package_info.write_text(
        "Metadata-Version: 2.4\nName: comfyui-docker-helper\nVersion: 1.2.3\n\n",
        encoding="utf-8",
    )
    with tarfile.open(
        dist_dir / "comfyui_docker_helper-1.2.3.tar.gz", mode="w:gz"
    ) as archive:
        archive.add(
            package_info,
            arcname="comfyui_docker_helper-1.2.3/PKG-INFO",
        )

    completed = subprocess.run(
        [sys.executable, str(DISTRIBUTION_TOOL)],
        cwd=tmp_path,
        env={**os.environ, "DIST_DIR": str(dist_dir)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
