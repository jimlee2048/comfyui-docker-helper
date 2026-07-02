"""End-to-end host build integration tests with fake Docker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.container.custom_nodes import load_custom_nodes_plan
from comfyui_docker_helper.container.download_files import load_file_download_plan
from comfyui_docker_helper.host.buildx import BuildxBuildError, BuildxBuildResult
from comfyui_docker_helper.rendering import has_valid_context_marker
from comfyui_docker_helper.rendering.dockerfile import serialize_dockerfile_word

MINIMAL_CONFIG = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"
"""


@dataclass(frozen=True, slots=True)
class FixtureCase:
    """One host-build integration fixture."""

    name: str
    extra_config: str
    scripts: dict[str, str]
    expect_custom_nodes: bool = False
    expect_files: bool = False
    expect_scripts: bool = False


FIXTURE_CASES = (
    FixtureCase(
        name="minimal",
        extra_config="""
[comfyui]
version = "latest"
""",
        scripts={},
    ),
    FixtureCase(
        name="node",
        extra_config="""
[comfyui]
version = "latest"

[[comfyui.custom_nodes]]
type = "registry"
id = "comfyui-impact-pack"
version = "latest"
""",
        scripts={},
        expect_custom_nodes=True,
    ),
    FixtureCase(
        name="file",
        extra_config="""
[comfyui]
version = "latest"

[cdh]
default_downloader = "httpx"

[[files]]
url = "https://example.com/model.safetensors"
dir = "models/checkpoints"
filename = "model.safetensors"
""",
        scripts={},
        expect_files=True,
    ),
    FixtureCase(
        name="hook",
        extra_config="""
[comfyui]
version = "latest"

[[comfyui.custom_nodes]]
type = "registry"
id = "hook-node"
pre_install_scripts = ["nested/pre hook.sh"]
""",
        scripts={"nested/pre hook.sh": "#!/bin/sh\n"},
        expect_custom_nodes=True,
        expect_scripts=True,
    ),
    FixtureCase(
        name="full",
        extra_config="""
[system]
workspace = "/work dir"
comfyui_path = "/work dir/Comfy UI"

[system.env]
SAFE_VALUE = 'space $cash "quote" \\ backtick` ;'

[comfyui]
version = "latest"
launch_args = ["--listen", 'value "quoted" $cash \\ path']

[cdh]
default_downloader = "httpx"

[[comfyui.custom_nodes]]
type = "registry"
id = "full-node"
pre_install_scripts = ["pre.sh"]
post_install_scripts = ["post.py"]

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/ComfyUI-Full.git"
ref = "v1.2.3"

[[files]]
url = "https://example.com/full.bin"
dir = "models/full"
filename = "full model.bin"
downloader = "httpx"
""",
        scripts={
            "pre.sh": "#!/bin/sh\n",
            "post.py": "print('post')\n",
        },
        expect_custom_nodes=True,
        expect_files=True,
        expect_scripts=True,
    ),
)


@pytest.mark.parametrize("case", FIXTURE_CASES, ids=lambda case: case.name)
def test_host_build_fixture_matrix_renders_context_before_fake_docker(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: FixtureCase,
) -> None:
    """Build main fixture combinations through validation and render."""
    config = tmp_path / f"{case.name}.toml"
    config.write_text(MINIMAL_CONFIG + case.extra_config, encoding="utf-8")
    context = tmp_path / f"context {case.name}"
    scripts_dir = tmp_path / f"scripts {case.name}"
    if case.scripts:
        for relative, content in case.scripts.items():
            path = scripts_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    docker_calls: list[tuple[str, Path, Path]] = []

    def fake_buildx(
        *,
        image_tag: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        del log
        assert has_valid_context_marker(context_dir)
        assert (context_dir / "Dockerfile").is_file()
        docker_calls.append((image_tag, context_dir, cwd))
        return BuildxBuildResult(
            argv=(
                "docker",
                "buildx",
                "build",
                "--load",
                "-t",
                image_tag,
                str(context_dir),
            ),
            context_dir=context_dir,
            image_tag=image_tag,
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        fake_buildx,
    )

    args = [
        "host",
        "build",
        "-f",
        str(config),
        "-t",
        f"demo:{case.name}",
        "--context-dir",
        str(context),
    ]
    if case.scripts:
        args.extend(["--scripts-dir", str(scripts_dir)])

    result = cli_runner.invoke(app, args)

    assert result.exit_code == 0
    assert docker_calls == [(f"demo:{case.name}", context, Path.cwd())]
    assert (context / "config.toml").is_file()
    assert (context / "config.lock.toml").is_file()
    assert not (context / "config" / "custom-nodes.toml").exists()
    assert not (context / "config" / "files.toml").exists()
    assert (context / "scripts").exists() is case.expect_scripts

    if case.expect_custom_nodes:
        custom_nodes = load_custom_nodes_plan(
            context / "config.toml",
            context / "config.lock.toml",
            scripts_dir=context / "scripts" if case.expect_scripts else None,
        )
        assert custom_nodes.items
    if case.expect_files:
        files = load_file_download_plan(
            context / "config.toml",
            context / "config.lock.toml",
            comfyui_path="/work dir/Comfy UI" if case.name == "full" else None,
        )
        assert files.items


def test_host_build_full_fixture_preserves_quoting_env_and_cmd(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise special env/CMD serialization through host build."""
    case = next(item for item in FIXTURE_CASES if item.name == "full")
    config = tmp_path / "full.toml"
    config.write_text(MINIMAL_CONFIG + case.extra_config, encoding="utf-8")
    scripts_dir = tmp_path / "scripts with space"
    for relative, content in case.scripts.items():
        path = scripts_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    context = tmp_path / "context with space"

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: BuildxBuildResult(
            argv=("docker",),
            context_dir=kwargs["context_dir"],
            image_tag=kwargs["image_tag"],
        ),
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "demo:full",
            "--scripts-dir",
            str(scripts_dir),
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 0
    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    safe_value = 'space $cash "quote" \\ backtick` ;'
    assert f"ENV SAFE_VALUE={serialize_dockerfile_word(safe_value)}" in dockerfile
    assert f"WORKDIR {serialize_dockerfile_word('/work dir')}" in dockerfile
    assert (
        "CMD "
        + json.dumps(
            [
                "python",
                "/work dir/Comfy UI/main.py",
                "--listen",
                'value "quoted" $cash \\ path',
            ],
            ensure_ascii=False,
        )
        in dockerfile
    )


def test_host_build_failure_after_context_completion_keeps_full_context(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagate fake Docker failure after rendering and retain the context."""
    case = next(item for item in FIXTURE_CASES if item.name == "full")
    config = tmp_path / "full.toml"
    config.write_text(MINIMAL_CONFIG + case.extra_config, encoding="utf-8")
    scripts_dir = tmp_path / "scripts"
    for relative, content in case.scripts.items():
        path = scripts_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    context = tmp_path / "context"

    def fail_after_context(*, context_dir: Path, **kwargs) -> None:
        del kwargs
        assert has_valid_context_marker(context_dir)
        assert (context_dir / "config.toml").is_file()
        assert (context_dir / "config.lock.toml").is_file()
        assert not (context_dir / "config" / "custom-nodes.toml").exists()
        assert not (context_dir / "config" / "files.toml").exists()
        assert (context_dir / "scripts" / "pre.sh").is_file()
        raise BuildxBuildError("fake docker failed after render")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        fail_after_context,
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "demo:fail",
            "--scripts-dir",
            str(scripts_dir),
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 1
    assert "fake docker failed after render" in result.stderr
    assert has_valid_context_marker(context)
    assert (context / "config.toml").is_file()
    assert (context / "config.lock.toml").is_file()
    assert not (context / "config" / "custom-nodes.toml").exists()
    assert not (context / "config" / "files.toml").exists()
    assert (context / "scripts" / "post.py").is_file()
