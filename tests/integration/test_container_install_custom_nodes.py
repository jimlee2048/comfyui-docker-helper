"""Container custom-node installation orchestration tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.artifact_helpers import write_root_artifacts
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.container import install_custom_nodes as installer
from comfyui_docker_helper.container.install_custom_nodes import install_custom_nodes
from comfyui_docker_helper.container.runners import (
    ContainerCommandError,
    ContainerRuntime,
)


def write_config(tmp_path: Path, body: str) -> Path:
    """Write root config and lock artifacts for custom-node tests."""
    normalized = body.lstrip()
    if "[comfyui]\nversion" not in normalized:
        normalized = normalized.replace(
            "[comfyui]\n",
            '[comfyui]\nversion = "latest"\n',
            1,
        )
    config, _ = write_root_artifacts(tmp_path, normalized)
    return config


def make_runtime(tmp_path: Path) -> ContainerRuntime:
    """Create a deterministic fake container runtime."""
    return ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )


def test_registry_nodes_update_cache_once_before_ordered_installs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run one fatal cache update before all ordered node installs."""
    config = write_config(
        tmp_path,
        """
[comfyui]

[[comfyui.custom_nodes]]
type = "registry"
id = "first"

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/second.git"
ref = "stable"

[[comfyui.custom_nodes]]
type = "registry"
id = "third"
version = "1.0.0"
""",
    )
    runtime = make_runtime(tmp_path)
    calls: list[tuple[str, list[str], str, dict[str, str]]] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        calls.append((description, argv, str(cwd), env))
        if description == "custom-node git clone https://example.com/second.git@stable":
            (runtime.comfyui_path / "custom_nodes" / "second").mkdir(parents=True)

    monkeypatch.setattr(installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(installer, "_git_output", lambda *_, **__: "abc123")

    install_custom_nodes(
        config,
        config.with_name("config.lock.toml"),
        runtime=runtime,
        log=lambda _: None,
    )

    assert [call[0] for call in calls] == [
        "custom-node registry cache update",
        "custom-node install first",
        "custom-node git clone https://example.com/second.git@stable",
        "custom-node git checkout https://example.com/second.git@stable",
        "custom-node git submodules https://example.com/second.git@stable",
        "custom-node install third@1.0.0",
    ]
    assert calls[0][1] == [str(runtime.python), "-m", "cm_cli", "update-cache"]
    assert calls[1][1] == [
        "comfy",
        "--skip-prompt",
        "--workspace",
        str(runtime.comfyui_path),
        "node",
        "install",
        "--exit-on-fail",
        "--fast-deps",
        "first",
    ]
    assert calls[2][1] == [
        "git",
        "clone",
        "--recursive",
        "https://example.com/second.git",
        str(runtime.comfyui_path / "custom_nodes" / "second"),
    ]
    assert calls[3][1] == [
        "git",
        "-C",
        str(runtime.comfyui_path / "custom_nodes" / "second"),
        "checkout",
        "--detach",
        "stable",
    ]
    assert calls[4][1] == [
        "git",
        "-C",
        str(runtime.comfyui_path / "custom_nodes" / "second"),
        "submodule",
        "update",
        "--init",
        "--recursive",
    ]
    assert calls[5][1][-1] == "third@1.0.0"
    assert {call[2] for call in calls} == {str(runtime.comfyui_path)}
    assert calls[0][3]["COMFYUI_PATH"] == str(runtime.comfyui_path)


def test_git_only_nodes_skip_cache_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not update registry cache when every node is a direct Git target."""
    config = write_config(
        tmp_path,
        """
[comfyui]

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/only"
""",
    )
    calls: list[str] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        del argv, cwd, env
        calls.append(description)
        if description == "custom-node git clone https://example.com/only":
            (tmp_path / "workspace" / "ComfyUI" / "custom_nodes" / "only").mkdir(
                parents=True
            )

    monkeypatch.setattr(installer, "run_argv", fake_run_argv)

    install_custom_nodes(
        config,
        config.with_name("config.lock.toml"),
        runtime=make_runtime(tmp_path),
        log=lambda _: None,
    )

    assert calls == ["custom-node git clone https://example.com/only"]


def test_git_node_clones_to_explicit_target_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use target_dir as the clone destination below ComfyUI/custom_nodes."""
    config = write_config(
        tmp_path,
        """
[comfyui]

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/upstream.git"
target_dir = "custom-name"
""",
    )
    runtime = make_runtime(tmp_path)
    calls: list[list[str]] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        del cwd, env
        calls.append(argv)
        if description == "custom-node git clone https://example.com/upstream.git":
            (runtime.comfyui_path / "custom_nodes" / "custom-name").mkdir(parents=True)

    monkeypatch.setattr(installer, "run_argv", fake_run_argv)

    install_custom_nodes(
        config,
        config.with_name("config.lock.toml"),
        runtime=runtime,
        log=lambda _: None,
    )

    assert calls == [
        [
            "git",
            "clone",
            "--recursive",
            "https://example.com/upstream.git",
            str(runtime.comfyui_path / "custom_nodes" / "custom-name"),
        ]
    ]


def test_git_node_installs_requirements_and_install_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install a git node with ref, requirements, and install.py in strict order."""
    ref = "609f3afaa74b2f88ef9ce8d939626065e3247469"
    config = write_config(
        tmp_path,
        f"""
[comfyui]

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/ComfyUI-Example.git"
ref = "{ref}"
""",
    )
    runtime = make_runtime(tmp_path)
    repo_path = runtime.comfyui_path / "custom_nodes" / "ComfyUI-Example"
    calls: list[tuple[str, list[str], Path]] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        del env
        calls.append((description, argv, cwd))
        if (
            description
            == f"custom-node git clone https://example.com/ComfyUI-Example.git@{ref}"
        ):
            repo_path.mkdir(parents=True)
            (repo_path / "requirements.txt").write_text("example\n", encoding="utf-8")
            (repo_path / "install.py").write_text(
                "print('install')\n", encoding="utf-8"
            )

    monkeypatch.setattr(installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(installer, "_git_output", lambda *_, **__: ref)

    install_custom_nodes(
        config,
        config.with_name("config.lock.toml"),
        runtime=runtime,
        log=lambda _: None,
    )

    assert [call[0] for call in calls] == [
        f"custom-node git clone https://example.com/ComfyUI-Example.git@{ref}",
        f"custom-node git checkout https://example.com/ComfyUI-Example.git@{ref}",
        f"custom-node git submodules https://example.com/ComfyUI-Example.git@{ref}",
        f"custom-node git requirements https://example.com/ComfyUI-Example.git@{ref}",
        f"custom-node git install.py https://example.com/ComfyUI-Example.git@{ref}",
    ]
    assert calls[0][1] == [
        "git",
        "clone",
        "--recursive",
        "https://example.com/ComfyUI-Example.git",
        str(repo_path),
    ]
    assert calls[1][1][-2:] == ["--detach", ref]
    assert calls[3][1] == [
        "uv",
        "pip",
        "install",
        "--python",
        str(runtime.python),
        "-r",
        str(repo_path / "requirements.txt"),
    ]
    assert calls[3][2] == repo_path
    assert calls[4][1] == [str(runtime.python), str(repo_path / "install.py")]
    assert calls[4][2] == repo_path


def test_git_full_commit_ref_mismatch_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail the build if a full commit ref does not match the checked-out HEAD."""
    ref = "609f3afaa74b2f88ef9ce8d939626065e3247469"
    config = write_config(
        tmp_path,
        f"""
[comfyui]

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/bad.git"
ref = "{ref}"
""",
    )
    runtime = make_runtime(tmp_path)
    events: list[str] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        del argv, cwd, env
        events.append(description)
        if description == f"custom-node git clone https://example.com/bad.git@{ref}":
            (runtime.comfyui_path / "custom_nodes" / "bad").mkdir(parents=True)

    monkeypatch.setattr(installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(
        installer,
        "_git_output",
        lambda *_, **__: "0000000000000000000000000000000000000000",
    )

    with pytest.raises(ContainerCommandError, match="ref verification failed"):
        install_custom_nodes(
            config,
            config.with_name("config.lock.toml"),
            runtime=runtime,
            log=lambda _: None,
        )

    assert events == [
        f"custom-node git clone https://example.com/bad.git@{ref}",
        f"custom-node git checkout https://example.com/bad.git@{ref}",
        f"custom-node git submodules https://example.com/bad.git@{ref}",
    ]


def test_hooks_run_pre_install_post_for_each_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run hooks around each individual node install."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for hook in ("pre.sh", "post.py"):
        (scripts / hook).write_text("pass\n", encoding="utf-8")
    config = write_config(
        tmp_path,
        """
[comfyui]

[[comfyui.custom_nodes]]
type = "registry"
id = "node"
pre_install_scripts = ["pre.sh"]
post_install_scripts = ["post.py"]
""",
    )
    events: list[str] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        del argv, cwd, env
        events.append(description)

    def fake_run_hooks(
        hooks: tuple[str, ...],
        *,
        scripts_dir: Path,
        runtime: ContainerRuntime,
        env: dict[str, str],
    ) -> None:
        del scripts_dir, runtime, env
        events.append(f"hooks:{','.join(hooks)}")

    monkeypatch.setattr(installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(installer, "run_hooks", fake_run_hooks)

    install_custom_nodes(
        config,
        config.with_name("config.lock.toml"),
        scripts_dir=scripts,
        runtime=make_runtime(tmp_path),
        log=lambda _: None,
    )

    assert events == [
        "custom-node registry cache update",
        "hooks:pre.sh",
        "custom-node install node",
        "hooks:post.py",
    ]


@pytest.mark.parametrize(
    "fail_description",
    ["custom-node registry cache update", "custom-node install node"],
)
def test_failures_stop_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_description: str,
) -> None:
    """Stop at cache or install failure without continuing later work."""
    config = write_config(
        tmp_path,
        """
[comfyui]

[[comfyui.custom_nodes]]
type = "registry"
id = "node"
post_install_scripts = ["post.sh"]

[[comfyui.custom_nodes]]
type = "registry"
id = "next"
""",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "post.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    events: list[str] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        del argv, cwd, env
        events.append(description)
        if description == fail_description:
            raise ContainerCommandError(description)

    def fake_run_hooks(
        hooks: tuple[str, ...],
        *,
        scripts_dir: Path,
        runtime: ContainerRuntime,
        env: dict[str, str],
    ) -> None:
        del scripts_dir, runtime, env
        events.append(f"hooks:{','.join(hooks)}")

    monkeypatch.setattr(installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(installer, "run_hooks", fake_run_hooks)

    with pytest.raises(ContainerCommandError, match=fail_description):
        install_custom_nodes(
            config,
            config.with_name("config.lock.toml"),
            scripts_dir=scripts,
            runtime=make_runtime(tmp_path),
            log=lambda _: None,
        )

    if fail_description == "custom-node registry cache update":
        assert events == ["custom-node registry cache update"]
    else:
        assert events == [
            "custom-node registry cache update",
            "custom-node install node",
        ]


def test_pre_hook_failure_stops_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing pre hook prevents the node install and later nodes."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    config = write_config(
        tmp_path,
        """
[comfyui]

[[comfyui.custom_nodes]]
type = "registry"
id = "node"
pre_install_scripts = ["pre.sh"]

[[comfyui.custom_nodes]]
type = "registry"
id = "next"
""",
    )
    events: list[str] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        del argv, cwd, env
        events.append(description)

    def fake_run_hooks(
        hooks: tuple[str, ...],
        *,
        scripts_dir: Path,
        runtime: ContainerRuntime,
        env: dict[str, str],
    ) -> None:
        del scripts_dir, runtime, env
        events.append(f"hooks:{','.join(hooks)}")
        raise ContainerCommandError("pre hook failed")

    monkeypatch.setattr(installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(installer, "run_hooks", fake_run_hooks)

    with pytest.raises(ContainerCommandError, match="pre hook failed"):
        install_custom_nodes(
            config,
            config.with_name("config.lock.toml"),
            scripts_dir=scripts,
            runtime=make_runtime(tmp_path),
            log=lambda _: None,
        )

    assert events == ["custom-node registry cache update", "hooks:pre.sh"]


def test_post_hook_failure_stops_before_next_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing post hook prevents later nodes from installing."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "post.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    config = write_config(
        tmp_path,
        """
[comfyui]

[[comfyui.custom_nodes]]
type = "registry"
id = "node"
post_install_scripts = ["post.sh"]

[[comfyui.custom_nodes]]
type = "registry"
id = "next"
""",
    )
    events: list[str] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        del argv, cwd, env
        events.append(description)

    def fake_run_hooks(
        hooks: tuple[str, ...],
        *,
        scripts_dir: Path,
        runtime: ContainerRuntime,
        env: dict[str, str],
    ) -> None:
        del scripts_dir, runtime, env
        events.append(f"hooks:{','.join(hooks)}")
        raise ContainerCommandError("post hook failed")

    monkeypatch.setattr(installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(installer, "run_hooks", fake_run_hooks)

    with pytest.raises(ContainerCommandError, match="post hook failed"):
        install_custom_nodes(
            config,
            config.with_name("config.lock.toml"),
            scripts_dir=scripts,
            runtime=make_runtime(tmp_path),
            log=lambda _: None,
        )

    assert events == [
        "custom-node registry cache update",
        "custom-node install node",
        "hooks:post.sh",
    ]


def test_cli_registers_install_custom_nodes_command(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invoke the supported container helper and pass options through."""
    config = tmp_path / "custom-nodes.toml"
    lock = tmp_path / "config.lock.toml"
    scripts = tmp_path / "scripts"
    seen: dict[str, Path | None | ContainerRuntime] = {}

    def fake_install_custom_nodes(
        config_path: Path,
        lock_path: Path,
        *,
        scripts_dir: Path | None = None,
        runtime: ContainerRuntime,
    ) -> None:
        seen["config"] = config_path
        seen["lock"] = lock_path
        seen["scripts_dir"] = scripts_dir
        seen["runtime"] = runtime

    monkeypatch.setattr(
        "comfyui_docker_helper.container.cli.install_custom_nodes",
        fake_install_custom_nodes,
    )
    monkeypatch.setenv("WORKSPACE", "/srv/work")
    monkeypatch.setenv("COMFYUI_PATH", "/opt/comfy")

    result = cli_runner.invoke(
        app,
        [
            "container",
            "install-custom-nodes",
            "--config",
            str(config),
            "--lock",
            str(lock),
            "--scripts-dir",
            str(scripts),
        ],
    )

    assert result.exit_code == 0
    assert seen["config"] == config
    assert seen["lock"] == lock
    assert seen["scripts_dir"] == scripts
    assert isinstance(seen["runtime"], ContainerRuntime)
    assert seen["runtime"].workspace == Path("/srv/work")
    assert seen["runtime"].comfyui_path == Path("/opt/comfy")


def test_cli_install_custom_nodes_fails_without_container_paths(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require Docker-managed path environment before helper execution."""
    config = tmp_path / "custom-nodes.toml"
    lock = tmp_path / "config.lock.toml"
    config.write_text("", encoding="utf-8")
    lock.write_text("", encoding="utf-8")
    monkeypatch.delenv("WORKSPACE", raising=False)
    monkeypatch.delenv("COMFYUI_PATH", raising=False)

    result = cli_runner.invoke(
        app,
        [
            "container",
            "install-custom-nodes",
            "--config",
            str(config),
            "--lock",
            str(lock),
        ],
    )

    assert result.exit_code == 1
    assert "missing required container environment" in result.output
