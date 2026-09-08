"""Custom-node git policy contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfyui_docker_helper.config.planning.build_plan import (
    GitCredentialRoutePlan,
)
from comfyui_docker_helper.container.build.custom_nodes import (
    git as git_installer,
)
from comfyui_docker_helper.container.build.custom_nodes import (
    orchestrator as custom_node_installer,
)
from comfyui_docker_helper.container.build.custom_nodes import (
    root_install,
)
from comfyui_docker_helper.container.build.custom_nodes.contracts import (
    CustomNodeInstallError,
)
from tests.container_installer_support import (
    _git_node,
)
from tests.container_installer_support import (
    application as _application,
)
from tests.container_installer_support import (
    custom_nodes_phase as _phase,
)
from tests.container_installer_support import (
    patch_phases as _patch_phases,
)


@pytest.mark.parametrize("url", ["-option", "file:///tmp/node.git", "bad"])
def test_runtime_rejects_forged_unsupported_git_locator(
    tmp_path: Path,
    url: str,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, (_git_node(runtime, url=url),))

    with pytest.raises(CustomNodeInstallError, match="URL is invalid"):
        custom_node_installer._validate_inputs(custom_nodes, application, runtime)


def test_git_credential_policy_covers_install_and_provenance_with_ssh_coexistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    node = _git_node(runtime)
    route = GitCredentialRoutePlan(
        match="https://example.invalid/Raw",
        username="token-user",
        secret_id="cdh-git-credential-private_git",
    )
    phase = _phase(runtime, (node,), git_credentials=(route,))
    _patch_phases(monkeypatch, application, phase)
    environment = custom_node_installer._git_environment(
        phase,
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.ssh://mirror/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://example.invalid/",
            "GIT_SSH_COMMAND": "ssh -F none",
            "GIT_SSL_CAINFO": "/custom/ca.pem",
        },
        build_plan_digest=f"sha256:{'a' * 64}",
    )
    observed: list[tuple[str, dict[str, str]]] = []

    def run_git(_argv, *, env, description, **_kwargs):
        observed.append((description, dict(env)))
        return b""

    def verify(_node, _target, _root, _git_path, env):
        observed.append(("provenance", dict(env)))

    monkeypatch.setattr(git_installer, "_run_git", run_git)
    monkeypatch.setattr(git_installer, "_verify_git_provenance", verify)
    monkeypatch.setattr(root_install, "install_root_surfaces", lambda *_args: None)

    custom_node_installer.install_custom_nodes(
        phase,
        application,
        runtime=runtime,
        environ={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.ssh://mirror/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://example.invalid/",
            "GIT_SSH_COMMAND": "ssh -F none",
            "GIT_SSL_CAINFO": "/custom/ca.pem",
        },
        build_plan_digest=f"sha256:{'a' * 64}",
    )

    descriptions = {item for item, _env in observed}
    assert {
        "Git node direct clone",
        "Git node direct exact checkout",
        "Git node direct recursive submodule checkout",
        "provenance",
    }.issubset(descriptions)
    for _description, git_environment in observed:
        for key, value in environment.items():
            assert git_environment[key] == value
        assert git_environment["GIT_CONFIG_COUNT"] == "5"
        assert git_environment["GIT_CONFIG_KEY_0"] == ("url.ssh://mirror/.insteadOf")
        assert git_environment["GIT_CONFIG_VALUE_0"] == ("https://example.invalid/")
        assert git_environment["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert git_environment["GIT_CONFIG_VALUE_1"] == ""
        assert git_environment["GIT_CONFIG_KEY_2"] == "credential.helper"
        assert (
            "container.build.git_credential_helper"
            in git_environment["GIT_CONFIG_VALUE_2"]
        )
        assert git_environment["GIT_CONFIG_KEY_3"] == "credential.useHttpPath"
        assert git_environment["GIT_CONFIG_VALUE_3"] == "true"
        assert git_environment["GIT_CONFIG_KEY_4"] == "credential.interactive"
        assert git_environment["GIT_CONFIG_VALUE_4"] == "false"
        assert git_environment["GIT_TERMINAL_PROMPT"] == "0"
        assert git_environment["GIT_ASKPASS"] == ""
        assert git_environment["GIT_SSH_COMMAND"] == "ssh -F none"
        assert git_environment["GIT_SSL_CAINFO"] == "/custom/ca.pem"


def test_git_credential_policy_rejects_an_invalid_ambient_config_count(
    tmp_path: Path,
) -> None:
    _application_phase, runtime = _application(tmp_path)
    route = GitCredentialRoutePlan(
        match="https://example.invalid/Raw",
        username="token-user",
        secret_id="cdh-git-credential-private_git",
    )
    phase = _phase(runtime, (_git_node(runtime),), git_credentials=(route,))

    with pytest.raises(CustomNodeInstallError) as raised:
        custom_node_installer._git_environment(
            phase,
            {"GIT_CONFIG_COUNT": "1_0"},
            build_plan_digest=f"sha256:{'a' * 64}",
        )

    assert str(raised.value) == "Git credential process policy is invalid"
