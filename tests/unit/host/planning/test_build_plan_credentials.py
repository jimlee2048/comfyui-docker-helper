"""BuildPlan credential projection and confidentiality contracts."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError
from tests.build_plan_support import (
    accepted_resolution,
    build_plan,
    final_config,
)

from comfyui_docker_helper.config.authored.models import FinalConfig
from comfyui_docker_helper.config.planning.build_plan import (
    CustomNodesPhase,
    DownloaderCredentialRoutePlan,
    GitCredentialRoutePlan,
    build_plan_digest,
    downloader_credential_secret_ids,
    dump_build_plan_json,
    git_credential_secret_ids,
    parse_build_plan_json,
)


def test_secret_sources_and_unused_definitions_do_not_change_image_plan() -> None:
    base_document = final_config().model_dump(mode="python")
    base_document["secrets"] = {
        "private_git": {"env": "FIRST_TOKEN"},
        "unused": {"file": "/first/unused"},
    }
    base_document["cdh"]["git"]["credentials"] = [
        {
            "match": "https://EXAMPLE.com:443/team/",
            "username": "token-user",
            "password": {"secret": "private_git"},
        }
    ]
    changed_document = deepcopy(base_document)
    changed_document["secrets"] = {
        "private_git": {"file": "../second-token"},
        "another_unused": {"env": "OTHER_TOKEN"},
    }
    changed_document["cdh"]["git"]["credentials"][0]["match"] = (
        "https://example.com/team"
    )
    first_config = FinalConfig.model_validate(base_document)
    second_config = FinalConfig.model_validate(changed_document)

    first = build_plan(first_config, accepted_resolution())
    second = build_plan(second_config, accepted_resolution())

    assert first.image_config_digest == second.image_config_digest
    assert first == second


def test_git_credential_routes_project_only_safe_ordered_plan_metadata() -> None:
    document = final_config().model_dump(mode="python")
    document["secrets"] = {
        "shared": {"env": "SYNTHETIC_SHARED_TOKEN"},
        "other": {"file": "/synthetic/private-token"},
    }
    document["cdh"]["git"]["credentials"] = [
        {
            "match": "https://EXAMPLE.com:443/team/",
            "username": "first-user",
            "password": {"secret": "shared"},
        },
        {
            "match": "https://example.com/team/subgroup/",
            "username": "second-user",
            "password": {"secret": "shared"},
        },
        {
            "match": "http://git.example.com:80/other/",
            "username": "third-user",
            "password": {"secret": "other"},
        },
    ]

    plan = build_plan(
        FinalConfig.model_validate(document),
        accepted_resolution(),
    )

    assert [
        route.model_dump(mode="python") for route in plan.custom_nodes.git_credentials
    ] == [
        {
            "match": "https://example.com/team",
            "username": "first-user",
            "secret_id": "cdh-git-credential-shared",
        },
        {
            "match": "https://example.com/team/subgroup",
            "username": "second-user",
            "secret_id": "cdh-git-credential-shared",
        },
        {
            "match": "http://git.example.com/other",
            "username": "third-user",
            "secret_id": "cdh-git-credential-other",
        },
    ]
    assert git_credential_secret_ids(plan.custom_nodes) == (
        "cdh-git-credential-shared",
        "cdh-git-credential-other",
    )
    serialized = dump_build_plan_json(plan)
    assert b"SYNTHETIC_SHARED_TOKEN" not in serialized
    assert b"/synthetic/private-token" not in serialized
    assert parse_build_plan_json(serialized) == plan


def test_git_credential_secret_projection_is_inert_without_direct_git_nodes() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    phase = CustomNodesPhase(
        install_manager=plan.custom_nodes.install_manager,
        user_directory=plan.custom_nodes.user_directory,
        nodes=tuple(
            node for node in plan.custom_nodes.nodes if node.type == "registry"
        ),
        git_credentials=(
            GitCredentialRoutePlan(
                match="https://example.com/team",
                username="token-user",
                secret_id="cdh-git-credential-private_git",
            ),
        ),
    )

    assert git_credential_secret_ids(phase) == ()


def test_git_credential_plan_revalidates_protocol_bounds_and_unique_contexts() -> None:
    maximum_username = "é" * 32_762 + "a"
    route = GitCredentialRoutePlan(
        match="https://example.com/team",
        username=maximum_username,
        secret_id="cdh-git-credential-private_git",
    )

    assert len(route.username.encode("utf-8")) == 65_525
    with pytest.raises(ValidationError, match="username is invalid"):
        GitCredentialRoutePlan(
            match=route.match,
            username=maximum_username + "a",
            secret_id=route.secret_id,
        )
    with pytest.raises(ValidationError, match="Secret ID must be canonical"):
        GitCredentialRoutePlan(
            match=route.match,
            username="token-user",
            secret_id="private_git",
        )

    plan = build_plan(final_config(), accepted_resolution())
    with pytest.raises(ValidationError, match="match contexts must be unique"):
        CustomNodesPhase(
            install_manager=plan.custom_nodes.install_manager,
            user_directory=plan.custom_nodes.user_directory,
            nodes=plan.custom_nodes.nodes,
            git_credentials=(route, route),
        )


def test_downloader_credential_routes_project_only_safe_files_metadata() -> None:
    document = final_config().model_dump(mode="python")
    document["secrets"] = {
        "shared": {"env": "SYNTHETIC_MODEL_TOKEN"},
        "other": {"file": "/synthetic/private-token"},
    }
    document["cdh"]["default_downloader"] = "httpx"
    document["cdh"]["downloader"]["credentials"] = [
        {
            "match": "https://EXAMPLE.test:443/",
            "type": "bearer",
            "token": {"secret": "shared"},
        },
        {
            "match": "https://example.test/private/",
            "type": "bearer",
            "token": {"secret": "other"},
        },
    ]

    plan = build_plan(FinalConfig.model_validate(document), accepted_resolution())

    assert [route.model_dump(mode="python") for route in plan.files.credentials] == [
        {
            "match": "https://example.test/",
            "type": "bearer",
            "token": {"secret": "shared"},
            "secret_id": "cdh-downloader-credential-shared",
        },
        {
            "match": "https://example.test/private",
            "type": "bearer",
            "token": {"secret": "other"},
            "secret_id": "cdh-downloader-credential-other",
        },
    ]
    assert downloader_credential_secret_ids(plan.files) == (
        "cdh-downloader-credential-shared",
        "cdh-downloader-credential-other",
    )
    serialized = dump_build_plan_json(plan)
    assert b"SYNTHETIC_MODEL_TOKEN" not in serialized
    assert b"/synthetic/private-token" not in serialized
    assert parse_build_plan_json(serialized) == plan


def test_downloader_secret_locator_is_excluded_but_route_reference_is_identity() -> (
    None
):
    document = final_config().model_dump(mode="python")
    document["secrets"] = {
        "first": {"env": "FIRST_TOKEN"},
        "second": {"env": "SECOND_TOKEN"},
    }
    document["cdh"]["default_downloader"] = "httpx"
    document["cdh"]["downloader"]["credentials"] = [
        {
            "match": "https://EXAMPLE.test:443/",
            "type": "bearer",
            "token": {"secret": "first"},
        }
    ]
    locator_changed = deepcopy(document)
    locator_changed["secrets"]["first"] = {"file": "/other/token"}
    locator_changed["cdh"]["downloader"]["credentials"][0]["match"] = (
        "https://example.test"
    )
    reference_changed = deepcopy(document)
    reference_changed["cdh"]["downloader"]["credentials"][0]["token"] = {
        "secret": "second"
    }

    first = build_plan(FinalConfig.model_validate(document), accepted_resolution())
    same = build_plan(
        FinalConfig.model_validate(locator_changed), accepted_resolution()
    )
    changed = build_plan(
        FinalConfig.model_validate(reference_changed), accepted_resolution()
    )

    assert first == same
    assert first.image_config_digest == same.image_config_digest
    assert first.image_config_digest != changed.image_config_digest
    assert build_plan_digest(first) != build_plan_digest(changed)


def test_downloader_credential_plan_revalidates_routes_and_httpx_requirement() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    route = DownloaderCredentialRoutePlan(
        match="https://unmatched.example/models",
        type="bearer",
        token={"secret": "model_read"},
        secret_id="cdh-downloader-credential-model_read",
    )
    phase = plan.files.model_copy(update={"credentials": (route,)})
    assert downloader_credential_secret_ids(phase) == ()

    with pytest.raises(ValidationError, match="match routes must be unique"):
        type(plan.files)(
            downloader=plan.files.downloader,
            credentials=(route, route),
            default_download_mode=plan.files.default_download_mode,
            download_max_attempts=plan.files.download_max_attempts,
            files=plan.files.files,
        )

    matching = DownloaderCredentialRoutePlan(
        match="https://example.test/",
        type="bearer",
        token=route.token,
        secret_id=route.secret_id,
    )
    with pytest.raises(ValidationError, match="reference and mount ID must agree"):
        DownloaderCredentialRoutePlan(
            match="https://example.test/",
            type="bearer",
            token={"secret": "other"},
            secret_id=route.secret_id,
        )
    with pytest.raises(ValidationError, match="require the HTTPX downloader"):
        type(plan.files)(
            downloader=plan.files.downloader,
            credentials=(matching,),
            default_download_mode=plan.files.default_download_mode,
            download_max_attempts=plan.files.download_max_attempts,
            files=plan.files.files,
        )


@pytest.mark.parametrize("field", ["match", "username", "password"])
def test_effective_git_credential_behavior_changes_image_identity(field: str) -> None:
    document = final_config().model_dump(mode="python")
    document["secrets"] = {
        "first": {"env": "FIRST_TOKEN"},
        "second": {"env": "SECOND_TOKEN"},
    }
    document["cdh"]["git"]["credentials"] = [
        {
            "match": "https://example.com/team/",
            "username": "token-user",
            "password": {"secret": "first"},
        }
    ]
    changed_document = deepcopy(document)
    route = changed_document["cdh"]["git"]["credentials"][0]
    if field == "match":
        route["match"] = "https://example.com/other/"
    elif field == "username":
        route["username"] = "other-user"
    else:
        route["password"] = {"secret": "second"}

    first = build_plan(FinalConfig.model_validate(document), accepted_resolution())
    second = build_plan(
        FinalConfig.model_validate(changed_document), accepted_resolution()
    )

    assert first.image_config_digest != second.image_config_digest
