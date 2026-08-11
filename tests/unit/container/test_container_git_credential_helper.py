"""Image-side Git credential helper boundary contracts."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_docker_helper.config.build_plan import (
    GitCredentialRoutePlan,
    build_plan_digest,
    dump_build_plan_json,
)
from comfyui_docker_helper.container import git_credential_helper
from tests.build_plan_support import accepted_resolution, build_plan, final_config


def _credential_plan(path: Path):
    plan = build_plan(final_config(), accepted_resolution())
    route = GitCredentialRoutePlan(
        match="https://example.test/team",
        username="token-user",
        secret_id="cdh-git-credential-private_git",
    )
    plan = plan.model_copy(
        update={
            "custom_nodes": plan.custom_nodes.model_copy(
                update={"git_credentials": (route,)}
            )
        }
    )
    path.write_bytes(dump_build_plan_json(plan))
    return plan


def test_container_helper_preserves_exact_selected_password_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "build-plan.json"
    plan = _credential_plan(plan_path)
    secret = tmp_path / "mounted-secret"
    secret.write_bytes(b"\xff=synthetic password")
    monkeypatch.setattr(
        git_credential_helper,
        "git_credential_secret_target",
        lambda _secret_id: str(secret),
    )

    response = git_credential_helper._credential_response(
        "get",
        b"protocol=https\nhost=example.test\npath=team/repository.git\n\n",
        environment={
            git_credential_helper.GIT_CREDENTIAL_BUILD_PLAN_DIGEST_ENV: (
                build_plan_digest(plan)
            )
        },
        build_plan_path=plan_path,
    )

    assert response == (b"username=token-user\npassword=\xff=synthetic password\n")


def test_non_get_does_not_admit_a_plan_or_read_a_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        git_credential_helper.BuildPlanInputAdmission,
        "from_path",
        lambda *_args, **_kwargs: pytest.fail("non-get admitted the BuildPlan"),
    )
    monkeypatch.setattr(
        git_credential_helper,
        "read_bounded_regular_absolute_file",
        lambda *_args, **_kwargs: pytest.fail("non-get read a Secret mount"),
    )

    assert (
        git_credential_helper._credential_response(
            "store", b"malformed\0payload", environment={}
        )
        is None
    )


def test_unmatched_context_does_not_read_a_secret_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "build-plan.json"
    plan = _credential_plan(plan_path)
    monkeypatch.setattr(
        git_credential_helper,
        "read_bounded_regular_absolute_file",
        lambda *_args, **_kwargs: pytest.fail("unmatched request read a mount"),
    )

    response = git_credential_helper._credential_response(
        "get",
        b"protocol=https\nhost=other.test\npath=team/repository.git\n\n",
        environment={
            git_credential_helper.GIT_CREDENTIAL_BUILD_PLAN_DIGEST_ENV: (
                build_plan_digest(plan)
            )
        },
        build_plan_path=plan_path,
    )

    assert response is None


@pytest.mark.parametrize(
    "content",
    [None, b"invalid\npassword", b"x" * 65_526],
)
def test_expected_mount_failures_are_silent_at_the_process_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes | None,
) -> None:
    plan_path = tmp_path / "build-plan.json"
    plan = _credential_plan(plan_path)
    secret = tmp_path / "mounted-secret"
    if content is not None:
        secret.write_bytes(content)
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    monkeypatch.setattr(
        git_credential_helper, "MATERIALIZED_BUILD_PLAN_PATH", plan_path
    )
    monkeypatch.setattr(
        git_credential_helper,
        "git_credential_secret_target",
        lambda _secret_id: str(secret),
    )
    monkeypatch.setenv(
        git_credential_helper.GIT_CREDENTIAL_BUILD_PLAN_DIGEST_ENV,
        build_plan_digest(plan),
    )
    monkeypatch.setattr(git_credential_helper.sys, "argv", ["helper", "get"])
    monkeypatch.setattr(
        git_credential_helper.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                b"protocol=https\nhost=example.test\npath=team/repository.git\n\n"
            )
        ),
    )
    monkeypatch.setattr(
        git_credential_helper.sys, "stdout", SimpleNamespace(buffer=stdout)
    )
    monkeypatch.setattr(
        git_credential_helper.sys, "stderr", SimpleNamespace(buffer=stderr)
    )

    assert git_credential_helper.main() == 1
    assert stdout.getvalue() == b""
    assert stderr.getvalue() == b""
