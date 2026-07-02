"""Render-to-container-helper integration tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.artifact_helpers import COMMIT_A
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.container import install_custom_nodes as installer
from comfyui_docker_helper.container.install_custom_nodes import install_custom_nodes
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.rendering import has_valid_context_marker

MINIMAL_CONFIG = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"

[comfyui]
version = "latest"
"""


def test_rendered_custom_node_context_feeds_container_installer(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render public root artifacts, then consume them with scripts."""
    config = tmp_path / "config.toml"
    config.write_text(
        MINIMAL_CONFIG
        + """
[[comfyui.custom_nodes]]
type = "registry"
id = "registry-node"
version = "1.2.3"
pre_install_scripts = ["pre.sh"]

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/git-node.git"
ref = "stable"
target_dir = "explicit-git-node"
post_install_scripts = ["post.py"]
""",
        encoding="utf-8",
    )
    hook_log = tmp_path / "hook.log"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre.sh").write_text(
        'printf "pre:%s:%s:%s:%s\\n" "$PWD" "$COMFYUI_PATH" "$WORKSPACE" '
        '"$VIRTUAL_ENV" >> "$HOOK_LOG"\n',
        encoding="utf-8",
    )
    (scripts / "post.py").write_text(
        "import os, pathlib\n"
        "pathlib.Path(os.environ['HOOK_LOG']).open('a', encoding='utf-8').write("
        "f\"post:{os.getcwd()}:{os.environ['COMFYUI_PATH']}:"
        "{os.environ['WORKSPACE']}:{os.environ['VIRTUAL_ENV']}\\n\")\n",
        encoding="utf-8",
    )
    output = tmp_path / "context"

    render = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(output),
            "--scripts-dir",
            str(scripts),
        ],
    )

    assert render.exit_code == 0
    assert has_valid_context_marker(output)
    assert (output / "config.toml").is_file()
    assert (output / "config.lock.toml").is_file()
    assert not (output / "config" / "custom-nodes.toml").exists()
    assert (output / "scripts" / "pre.sh").is_file()
    assert (output / "scripts" / "post.py").is_file()
    dockerfile = (output / "Dockerfile").read_text(encoding="utf-8")
    assert "comfy-cli==" in dockerfile
    assert '--version "$COMFYUI_VERSION"' not in dockerfile
    assert 'if [ "$COMFY_CLI_VERSION" = latest ]' not in dockerfile
    assert 'git -C "$COMFYUI_PATH" rev-parse HEAD' in dockerfile
    assert "source=config.toml" in dockerfile
    assert "source=config.lock.toml" in dockerfile
    assert "source=config/custom-nodes.toml" not in dockerfile
    assert "source=scripts,target=/tmp/cdh/scripts" in dockerfile
    assert "cdh container install-custom-nodes" in dockerfile

    workspace = tmp_path / "workspace"
    comfyui_path = workspace / "ComfyUI"
    comfyui_path.mkdir(parents=True)
    runtime = ContainerRuntime(
        workspace=workspace,
        comfyui_path=comfyui_path,
        virtual_env=Path(sys.executable).resolve().parent.parent,
    )
    subprocess_calls: list[tuple[str, list[str]]] = []

    def fake_run_argv(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        description: str,
    ) -> None:
        assert cwd == comfyui_path
        assert env["COMFYUI_PATH"] == str(comfyui_path)
        subprocess_calls.append((description, argv))
        if (
            description
            == f"custom-node git clone https://example.com/git-node.git@{COMMIT_A}"
        ):
            (comfyui_path / "custom_nodes" / "explicit-git-node").mkdir(parents=True)

    monkeypatch.setenv("HOOK_LOG", str(hook_log))
    monkeypatch.setattr(installer, "run_argv", fake_run_argv)
    monkeypatch.setattr(installer, "_git_output", lambda *_, **__: COMMIT_A)

    install_custom_nodes(
        output / "config.toml",
        output / "config.lock.toml",
        scripts_dir=output / "scripts",
        runtime=runtime,
        log=lambda _: None,
    )

    assert [call[0] for call in subprocess_calls] == [
        "custom-node registry cache update",
        "custom-node install registry-node@1.2.3",
        f"custom-node git clone https://example.com/git-node.git@{COMMIT_A}",
        f"custom-node git checkout https://example.com/git-node.git@{COMMIT_A}",
        f"custom-node git submodules https://example.com/git-node.git@{COMMIT_A}",
    ]
    assert subprocess_calls[0][1] == [
        str(runtime.python),
        "-m",
        "cm_cli",
        "update-cache",
    ]
    assert subprocess_calls[1][1][-1] == "registry-node@1.2.3"
    assert subprocess_calls[2][1] == [
        "git",
        "clone",
        "--recursive",
        "https://example.com/git-node.git",
        str(comfyui_path / "custom_nodes" / "explicit-git-node"),
    ]
    assert subprocess_calls[3][1][-2:] == ["--detach", COMMIT_A]
    assert hook_log.read_text(encoding="utf-8").splitlines() == [
        f"pre:{comfyui_path}:{comfyui_path}:{workspace}:{runtime.virtual_env}",
        f"post:{comfyui_path}:{comfyui_path}:{workspace}:{runtime.virtual_env}",
    ]
