"""Custom-node git policy contracts."""

from __future__ import annotations

import os
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


@pytest.mark.parametrize("url", ["-option", "file:///tmp/node.git", "bad"])
def test_runtime_rejects_forged_unsupported_git_locator(
    tmp_path: Path,
    url: str,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = _phase(runtime, (_git_node(runtime, url=url),))

    with pytest.raises(CustomNodeInstallError, match="URL is invalid"):
        custom_node_installer._validate_inputs(custom_nodes, application, runtime)


def test_git_installer_runs_only_root_requirements_then_install_py(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path / "custom_nodes/direct"
    target.mkdir()
    target.joinpath("requirements.txt").write_text(
        "nvidia-ml-py # for GPU util/power/temp only\n"
    )
    target.joinpath("install.py").write_text("print('root')\n")
    nested = target / "nested"
    nested.mkdir()
    nested.joinpath("requirements.txt").write_text("must-not-run==9\n")
    nested.joinpath("install.py").write_text("raise RuntimeError\n")
    node = _git_node(runtime)
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    events: list[str] = []

    def run(argv, **kwargs):
        commands.append((tuple(os.fspath(item) for item in argv), kwargs))
        events.append("requirements" if "--requirements" in argv else "install.py")

    monkeypatch.setattr(git_installer, "run_argv", run)
    constraints = tmp_path / "constraints.txt"
    python_environment = {
        "BUILD_ENV_SENTINEL": "kept",
        "PIP_CONSTRAINT": str(constraints),
        "UV_CONSTRAINT": str(constraints),
    }
    git_installer._install_git_root_surfaces(
        node,
        target,
        application,
        runtime,
        Path("/usr/local/bin/uv"),
        constraints,
        python_environment,
    )

    assert len(commands) == 2
    assert events == ["requirements", "install.py"]
    requirements_argv, requirements_kwargs = commands[0]
    assert requirements_argv == (
        "/usr/local/bin/uv",
        "--no-config",
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--no-python-downloads",
        "--default-index",
        application.python_index_url,
        "--constraint",
        str(constraints),
        "--requirements",
        str(target / "requirements.txt"),
    )
    assert requirements_kwargs["close_stdin"] is True
    assert requirements_kwargs["env"] == python_environment
    install_argv, install_kwargs = commands[1]
    assert install_argv == ("/opt/venv/bin/python", str(target / "install.py"))
    assert install_kwargs["close_stdin"] is True
    for command_kwargs in (requirements_kwargs, install_kwargs):
        assert command_kwargs["env"] == python_environment


def test_git_root_installer_rejects_symlinked_surface(tmp_path: Path) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path / "custom_nodes/direct"
    target.mkdir()
    outside = tmp_path / "requirements.txt"
    outside.write_text("example==1.0\n")
    target.joinpath("requirements.txt").symlink_to(outside)

    with pytest.raises(CustomNodeInstallError, match="one regular file"):
        git_installer._install_git_root_surfaces(
            _git_node(runtime),
            target,
            application,
            runtime,
            Path("/usr/local/bin/uv"),
            tmp_path / "constraints.txt",
            {},
        )


@pytest.mark.parametrize(
    "requirement",
    [
        "--index-url https://packages.invalid/simple\nexample==1\n",
        "--extra-index-url https://packages.invalid/simple\nexample==1\n",
        "-r nested.txt\n",
        "-c constraints.txt\n",
        "example @ https://packages.invalid/example.whl\n",
    ],
)
def test_git_requirements_reject_source_control_before_any_install_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requirement: str,
) -> None:
    application, runtime = _application(tmp_path)
    target = runtime.comfyui_path / "custom_nodes/direct"
    target.mkdir()
    target.joinpath("requirements.txt").write_text(requirement)
    target.joinpath("install.py").write_text("raise RuntimeError\n")
    monkeypatch.setattr(
        git_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("invalid requirements must not execute"),
    )

    with pytest.raises(CustomNodeInstallError, match="requirements are invalid"):
        git_installer._install_git_root_surfaces(
            _git_node(runtime),
            target,
            application,
            runtime,
            Path("/usr/local/bin/uv"),
            tmp_path / "constraints.txt",
            {},
        )


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
    monkeypatch.setattr(
        git_installer, "_install_git_root_surfaces", lambda *_args: None
    )

    git_installer._install_git_node(
        node,
        runtime.comfyui_path / "custom_nodes",
        application,
        runtime,
        Path("/usr/bin/git"),
        Path("/usr/local/bin/uv"),
        tmp_path / "constraints.txt",
        environment,
        {},
    )

    descriptions = {item for item, _env in observed}
    assert {
        "Git node direct clone",
        "Git node direct exact checkout",
        "Git node direct recursive submodule checkout",
        "provenance",
    }.issubset(descriptions)
    for _description, git_environment in observed:
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
