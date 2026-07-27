"""Admit one source, candidate, or formal package build."""

from __future__ import annotations

import os
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

PROJECT_NAME = "comfyui-docker-helper"
PROJECT_FILE = Path("pyproject.toml")
PROJECTION_FILE = Path(
    "src/comfyui_docker_helper/resources/release-projection/pyproject.toml"
)

GitOutput = Callable[[Path, str], str]
GitAncestry = Callable[[Path, str, str], bool]


@dataclass(frozen=True)
class Admission:
    """Outputs produced after a package build is admitted."""

    artifact_name: str
    version: str


def _git_output(project_root: Path, revision: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", revision],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_is_ancestor(project_root: Path, ancestor: str, descendant: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=project_root,
            check=False,
        ).returncode
        == 0
    )


def _project_version(project_root: Path) -> tuple[str, Version]:
    root = tomllib.loads((project_root / PROJECT_FILE).read_text(encoding="utf-8"))[
        "project"
    ]
    projection = tomllib.loads(
        (project_root / PROJECTION_FILE).read_text(encoding="utf-8")
    )["project"]
    if (
        root["name"] != PROJECT_NAME
        or projection["name"] != PROJECT_NAME
        or root["version"] != projection["version"]
    ):
        raise SystemExit("root and release-projection package identities differ")

    raw_version = root["version"]
    try:
        version = Version(raw_version)
    except InvalidVersion as error:
        raise SystemExit("project version is not valid PEP 440") from error
    if (
        str(version) != raw_version
        or version.epoch
        or version.post is not None
        or version.local is not None
    ):
        raise SystemExit("project version is outside the package-tag contract")
    return raw_version, version


def admit_package_build(
    environment: Mapping[str, str],
    project_root: Path,
    *,
    git_output: GitOutput = _git_output,
    git_is_ancestor: GitAncestry = _git_is_ancestor,
) -> Admission:
    """Validate the checked-out package and event identity."""

    input_mode = environment.get("INPUT_MODE", "")
    if input_mode:
        if input_mode not in {"source", "formal"}:
            raise SystemExit("workflow_call mode must be source or formal")
        mode = input_mode
    else:
        if environment["EVENT_NAME"] != "push":
            raise SystemExit("direct execution requires a tag-push event")
        mode = "candidate"

    raw_version, version = _project_version(project_root)
    expected_tag = ""
    if mode == "candidate":
        expected_tag = environment["REF_NAME"]
    elif mode == "formal":
        if (
            environment["EVENT_NAME"] != "release"
            or environment["EVENT_ACTION"] != "published"
            or environment["RELEASE_PRERELEASE"] != "false"
        ):
            raise SystemExit("formal mode requires a published stable Release")
        if version.is_prerelease:
            raise SystemExit("formal publication requires a stable final version")
        expected_tag = environment["RELEASE_TAG"]

    if expected_tag:
        head = git_output(project_root, "HEAD")
        expected_ref = f"refs/tags/{expected_tag}"
        if (
            expected_tag != f"v{raw_version}"
            or environment["GITHUB_REF"] != expected_ref
        ):
            raise SystemExit("tag, event ref, and static version differ")
        tag_commit = git_output(project_root, f"{expected_ref}^{{commit}}")
        if tag_commit != head:
            raise SystemExit("tag no longer peels to the checked-out commit")

    if mode == "formal" and not git_is_ancestor(
        project_root, "HEAD", "refs/remotes/origin/main"
    ):
        raise SystemExit("release tag commit is not in main history")

    artifact_name = (
        ""
        if mode == "source"
        else f"{mode}-distributions-attempt-{environment['GITHUB_RUN_ATTEMPT']}"
    )
    return Admission(artifact_name=artifact_name, version=raw_version)


def main() -> None:
    """Run admission from the GitHub Actions environment."""

    admission = admit_package_build(os.environ, Path.cwd())
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        print(f"artifact_name={admission.artifact_name}", file=output)
        print(f"version={admission.version}", file=output)


if __name__ == "__main__":
    main()
