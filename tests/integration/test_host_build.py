"""CLI tests for ``cdh host build``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.host.buildx import BuildxBuildError, BuildxBuildResult
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


def write_config(root: Path, document: str = MINIMAL_CONFIG) -> Path:
    """Write a test config document."""
    path = root / "config.toml"
    path.write_text(document, encoding="utf-8")
    return path


def test_build_help_exposes_current_options(cli_runner: CliRunner) -> None:
    """Expose the supported build command options."""
    result = cli_runner.invoke(app, ["host", "build", "--help"])

    assert result.exit_code == 0
    assert "Usage: cdh host build" in result.stdout
    assert "-f" in result.stdout
    assert "--file" in result.stdout
    assert "-t" in result.stdout
    assert "--tag" in result.stdout
    assert "--load" in result.stdout
    assert "--push" in result.stdout
    assert "--scripts-dir" in result.stdout
    assert "--context-dir" in result.stdout
    assert "--clean-context" not in result.stdout
    assert "--force" not in result.stdout
    assert "--overwrite" not in result.stdout


def test_build_renders_default_context_and_invokes_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use .cdh/build/current by default and invoke Buildx after rendering."""
    config = write_config(tmp_path)
    calls: list[tuple[tuple[str, ...], str, Path, Path]] = []

    def fake_buildx(
        *,
        image_tags: tuple[str, ...],
        output: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        log(f"fake buildx loaded {', '.join(image_tags)}")
        calls.append((image_tags, output, context_dir, cwd))
        return BuildxBuildResult(
            argv=(
                "docker",
                "buildx",
                "build",
                "--load",
                "-t",
                *image_tags,
                str(context_dir),
            ),
            context_dir=context_dir,
            image_tags=image_tags,
            output="load",
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        fake_buildx,
    )

    result = cli_runner.invoke(
        app,
        ["host", "build", "-f", str(config), "-t", "demo:dev"],
    )

    assert result.exit_code == 0
    context = tmp_path / ".cdh" / "build" / "current"
    assert has_valid_context_marker(context)
    assert (context / "Dockerfile").is_file()
    assert calls == [(("demo:dev",), "load", Path(".cdh/build/current"), tmp_path)]
    assert "Build context: .cdh/build/current" in result.stdout
    assert "fake buildx loaded demo:dev" in result.stdout


def test_build_uses_custom_context_dir(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render and build the caller-provided context directory."""
    config = write_config(tmp_path)
    context = tmp_path / "custom context"
    calls: list[Path] = []

    def fake_buildx(
        *,
        image_tags: tuple[str, ...],
        output: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        del cwd, log
        calls.append(context_dir)
        return BuildxBuildResult(
            argv=(
                "docker",
                "buildx",
                "build",
                "--load",
                "-t",
                image_tags[0],
                str(context_dir),
            ),
            context_dir=context_dir,
            image_tags=image_tags,
            output=output,
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        fake_buildx,
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "demo:custom",
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 0
    assert has_valid_context_marker(context)
    assert calls == [context]


def test_build_overwrites_existing_marked_context(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal overwrite replaces valid rendered contexts before Buildx."""
    config = write_config(tmp_path)
    context = tmp_path / "context"
    context.mkdir()
    (context / ".cdh-rendered").write_text(
        '{"kind":"build-context","tool":"comfyui-docker-helper","version":"0.1"}\n',
        encoding="utf-8",
    )
    (context / "old.txt").write_text("replace\n", encoding="utf-8")
    calls: list[Path] = []

    def fake_buildx(
        *,
        image_tags: tuple[str, ...],
        output: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        del image_tags, output, cwd, log
        calls.append(context_dir)
        return BuildxBuildResult(
            argv=("docker",),
            context_dir=context_dir,
            image_tags=("demo:overwrite",),
            output="load",
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        fake_buildx,
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "demo:overwrite",
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 0
    assert has_valid_context_marker(context)
    assert not (context / "old.txt").exists()
    assert calls == [context]


def test_build_accepts_repeated_file_options_in_cli_order(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merge layered configs before rendering and fake Buildx invocation."""
    base = write_config(tmp_path)
    override = tmp_path / "override.toml"
    override.write_text(
        """
[system]
workspace = "/srv"
""",
        encoding="utf-8",
    )
    context = tmp_path / "context"

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: BuildxBuildResult(
            argv=("docker",),
            context_dir=kwargs["context_dir"],
            image_tags=kwargs["image_tags"],
            output=kwargs["output"],
        ),
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(base),
            "-f",
            str(override),
            "-t",
            "demo:layered",
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 0
    assert has_valid_context_marker(context)
    assert "ENV WORKSPACE=/srv" in (context / "Dockerfile").read_text(encoding="utf-8")


def test_build_with_host_warnings_still_renders_and_invokes_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Print host-context warnings without blocking the v0.2 build flow."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[cdh]
default_download_mode = "sync"
""",
    )
    context = tmp_path / "context"
    calls: list[tuple[str, ...]] = []

    def fake_buildx(
        *,
        image_tags: tuple[str, ...],
        output: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        del context_dir, output, cwd, log
        calls.append(image_tags)
        return BuildxBuildResult(
            argv=("docker",),
            context_dir=context,
            image_tags=image_tags,
            output="load",
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        fake_buildx,
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "demo:warning",
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 0
    assert "Configuration has warnings:" in result.stderr
    assert "[cdh.default_download_mode]" in result.stderr
    assert has_valid_context_marker(context)
    assert calls == [("demo:warning",)]


def test_build_accepts_repeated_tags_and_passes_them_in_cli_order(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated tag options are valid Buildx tags."""
    config = write_config(tmp_path)
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: calls.append(kwargs["image_tags"]),
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "one:tag",
            "-t",
            "two:tag",
            "--context-dir",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 0
    assert calls == [("one:tag", "two:tag")]


def test_build_uses_config_tags_when_cli_tags_are_absent(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read image tags from [build].tags when --tag is not provided."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[build]
tags = ["config:one", "config:two"]
""",
    )
    context = tmp_path / "context"
    calls: list[tuple[tuple[str, ...], str]] = []

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: calls.append((kwargs["image_tags"], kwargs["output"])),
    )

    result = cli_runner.invoke(
        app,
        ["host", "build", "-f", str(config), "--context-dir", str(context)],
    )

    assert result.exit_code == 0
    assert has_valid_context_marker(context)
    assert calls == [(("config:one", "config:two"), "load")]


def test_build_cli_tags_replace_config_tags_and_output_overrides_config(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI build settings take precedence over config build settings."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[build]
tags = ["config:tag"]
output = "push"
""",
    )
    calls: list[tuple[tuple[str, ...], str]] = []

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: calls.append((kwargs["image_tags"], kwargs["output"])),
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "cli:one",
            "-t",
            "cli:two",
            "--load",
            "--context-dir",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 0
    assert calls == [(("cli:one", "cli:two"), "load")]


def test_build_uses_config_push_output(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use [build].output when no CLI output override is provided."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[build]
tags = ["registry.example.com/demo:push"]
output = "push"
""",
    )
    calls: list[str] = []

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: calls.append(kwargs["output"]),
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "--context-dir",
            str(tmp_path / "context"),
        ],
    )

    assert result.exit_code == 0
    assert calls == ["push"]


def test_build_rejects_load_and_push_together_before_loading(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject mutually exclusive output flags before validation or rendering."""
    called = False

    def fail_if_called(*args, **kwargs) -> None:
        del args, kwargs
        nonlocal called
        called = True

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.prepare_render_context",
        fail_if_called,
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            "config.toml",
            "-t",
            "demo:tag",
            "--load",
            "--push",
        ],
    )

    assert result.exit_code == 2
    assert "must not be used together" in result.output
    assert called is False


def test_build_requires_effective_tag(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject builds that have neither CLI tags nor config tags."""
    config = write_config(tmp_path)
    context = tmp_path / "context"

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: pytest.fail("buildx should not run"),
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 2
    assert "must provide at least one image tag" in result.output
    assert not context.exists()


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "bad tag",
        "bad\ttag",
        "bad\ntag",
        "bad\x7ftag",
    ],
)
def test_build_rejects_invalid_cli_tag_before_rendering(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
) -> None:
    """Reject CLI tags that config [build].tags validation would reject."""
    config = write_config(tmp_path)
    context = tmp_path / "context"

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: pytest.fail("buildx should not run"),
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            tag,
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 2
    assert "must be non-empty" in result.output
    assert "whitespace or control characters" in result.output
    assert not context.exists()


def test_build_invalid_input_uses_same_rich_diagnostics(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Share validation diagnostics with validate/render."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG.replace('type = "cuda"', 'type = "rocm"'),
    )
    context = tmp_path / "context"

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "demo:bad",
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Configuration is invalid:" in result.stderr
    assert "[compute_platform.type]" in result.stderr
    assert not context.exists()


def test_build_rejects_unmarked_context_without_public_overwrite_flag(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal overwrite still refuses to replace unsafe unmarked directories."""
    config = write_config(tmp_path)
    context = tmp_path / "context"
    context.mkdir()
    (context / "keep.txt").write_text("keep\n", encoding="utf-8")

    def fail_if_called(*args, **kwargs) -> None:
        del args, kwargs
        raise AssertionError("buildx should not run")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        fail_if_called,
    )

    result = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "-f",
            str(config),
            "-t",
            "demo:safe",
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 1
    assert "not a valid cdh build context" in result.stderr
    assert (context / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_buildx_failure_propagates_and_retains_context(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the rendered context when Buildx fails."""
    config = write_config(tmp_path)
    context = tmp_path / "context"

    def fail_buildx(
        *,
        image_tags: tuple[str, ...],
        output: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> None:
        del image_tags, output, context_dir, cwd, log
        raise BuildxBuildError("docker failed")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        fail_buildx,
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
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 1
    assert "docker failed" in result.stderr
    assert has_valid_context_marker(context)
    assert (context / "Dockerfile").is_file()
