"""Unit coverage for package-build admission policy."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from tools.ci.admit_package_build import Admission, admit_package_build

GitOutput = Callable[[Path, str], str]


def _write_project(
    project_root: Path,
    *,
    version: str = "1.2.3",
    root_name: str = "comfyui-docker-helper",
    projection_name: str = "comfyui-docker-helper",
    projection_version: str | None = None,
) -> None:
    projection = (
        project_root
        / "src/comfyui_docker_helper/resources/release-projection/pyproject.toml"
    )
    projection.parent.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text(
        f'[project]\nname = "{root_name}"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    projection.write_text(
        f'[project]\nname = "{projection_name}"\n'
        f'version = "{projection_version or version}"\n',
        encoding="utf-8",
    )


def _environment(mode: str, version: str = "1.2.3") -> dict[str, str]:
    tag = f"v{version}"
    if mode == "candidate":
        return {
            "EVENT_NAME": "push",
            "GITHUB_REF": f"refs/tags/{tag}",
            "GITHUB_RUN_ATTEMPT": "2",
            "INPUT_MODE": "",
            "REF_NAME": tag,
        }
    return {
        "EVENT_ACTION": "published" if mode == "formal" else "",
        "EVENT_NAME": "release" if mode == "formal" else "workflow_call",
        "GITHUB_REF": f"refs/tags/{tag}" if mode == "formal" else "refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "2",
        "INPUT_MODE": mode,
        "REF_NAME": tag if mode == "formal" else "main",
        "RELEASE_PRERELEASE": "false",
        "RELEASE_TAG": tag,
    }


def _matching_git(project_root: Path, revision: str) -> str:
    del project_root, revision
    return "commit"


@pytest.mark.parametrize(
    ("mode", "version", "artifact_name"),
    [
        ("source", "1.2.3", ""),
        ("candidate", "1.2.3", "candidate-distributions-attempt-2"),
        ("candidate", "1.2.3rc1", "candidate-distributions-attempt-2"),
        ("candidate", "1.2.3.dev1", "candidate-distributions-attempt-2"),
        ("formal", "1.2.3", "formal-distributions-attempt-2"),
    ],
)
def test_admission_accepts_representative_modes(
    tmp_path: Path,
    mode: str,
    version: str,
    artifact_name: str,
) -> None:
    _write_project(tmp_path, version=version)

    admission = admit_package_build(
        _environment(mode, version),
        tmp_path,
        git_output=_matching_git,
        git_is_ancestor=lambda *_: True,
    )

    assert admission == Admission(artifact_name=artifact_name, version=version)


@pytest.mark.parametrize(
    "version",
    ["not-a-version", "01.2.3", "1!1.2.3", "1.2.3.post1", "1.2.3+local"],
)
def test_admission_rejects_versions_outside_policy(
    tmp_path: Path, version: str
) -> None:
    _write_project(tmp_path, version=version)

    with pytest.raises(SystemExit, match=r"PEP 440|package-tag contract"):
        admit_package_build(_environment("source", version), tmp_path)


@pytest.mark.parametrize(
    "project_options",
    [
        {"root_name": "another-project"},
        {"projection_name": "another-project"},
        {"projection_version": "1.2.4"},
    ],
)
def test_admission_rejects_project_identity_drift(
    tmp_path: Path, project_options: dict[str, str]
) -> None:
    _write_project(tmp_path, **project_options)

    with pytest.raises(SystemExit, match="package identities differ"):
        admit_package_build(_environment("source"), tmp_path)


@pytest.mark.parametrize(
    ("environment_update", "message"),
    [
        ({"INPUT_MODE": "unexpected"}, "workflow_call mode"),
        ({"EVENT_NAME": "workflow_dispatch"}, "tag-push"),
        ({"REF_NAME": "v1.2.4"}, "tag, event ref"),
        ({"GITHUB_REF": "refs/tags/v1.2.4"}, "tag, event ref"),
    ],
)
def test_admission_rejects_mode_event_or_tag_mismatch(
    tmp_path: Path,
    environment_update: dict[str, str],
    message: str,
) -> None:
    _write_project(tmp_path)
    environment = _environment("candidate")
    environment.update(environment_update)

    with pytest.raises(SystemExit, match=message):
        admit_package_build(
            environment,
            tmp_path,
            git_output=_matching_git,
        )


@pytest.mark.parametrize(
    ("version", "environment_update", "ancestor", "message"),
    [
        ("1.2.3rc1", {}, True, "stable final"),
        ("1.2.3.dev1", {}, True, "stable final"),
        ("1.2.3", {"EVENT_NAME": "push"}, True, "published stable"),
        ("1.2.3", {"EVENT_ACTION": "created"}, True, "published stable"),
        ("1.2.3", {"RELEASE_PRERELEASE": "true"}, True, "published stable"),
        ("1.2.3", {}, False, "not in main history"),
    ],
)
def test_formal_admission_rejects_unqualified_release(
    tmp_path: Path,
    version: str,
    environment_update: dict[str, str],
    ancestor: bool,
    message: str,
) -> None:
    _write_project(tmp_path, version=version)
    environment = _environment("formal", version)
    environment.update(environment_update)

    with pytest.raises(SystemExit, match=message):
        admit_package_build(
            environment,
            tmp_path,
            git_output=_matching_git,
            git_is_ancestor=lambda *_: ancestor,
        )


def test_admission_rejects_tag_that_does_not_peel_to_head(tmp_path: Path) -> None:
    _write_project(tmp_path)

    def mismatched_git(project_root: Path, revision: str) -> str:
        del project_root
        return "head" if revision == "HEAD" else "tag"

    with pytest.raises(SystemExit, match="peels to the checked-out commit"):
        admit_package_build(
            _environment("candidate"),
            tmp_path,
            git_output=mismatched_git,
        )
