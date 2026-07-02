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
    calls: list[tuple[str, Path, Path]] = []

    def fake_buildx(
        *,
        image_tag: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        log(f"fake buildx loaded {image_tag}")
        calls.append((image_tag, context_dir, cwd))
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
    assert calls == [("demo:dev", Path(".cdh/build/current"), tmp_path)]
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
        image_tag: str,
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
        image_tag: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        del image_tag, cwd, log
        calls.append(context_dir)
        return BuildxBuildResult(
            argv=("docker",),
            context_dir=context_dir,
            image_tag="demo:overwrite",
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
            image_tag=kwargs["image_tag"],
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
    calls: list[str] = []

    def fake_buildx(
        *,
        image_tag: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        del context_dir, cwd, log
        calls.append(image_tag)
        return BuildxBuildResult(
            argv=("docker",),
            context_dir=context,
            image_tag=image_tag,
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
    assert calls == ["demo:warning"]


def test_build_rejects_repeated_singleton_options_before_loading(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject repeated tag options before validation or rendering."""
    called = False

    def fail_if_called(*args, **kwargs) -> None:
        del args, kwargs
        nonlocal called
        called = True

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.load_validate_plan_result",
        fail_if_called,
    )

    result = cli_runner.invoke(
        app,
        ["host", "build", "-f", "one.toml", "-t", "one:tag", "-t", "two:tag"],
    )

    assert result.exit_code == 2
    assert "must be provided exactly once" in result.output
    assert "Usage: cdh host build" in result.output
    assert called is False


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

    def fail_buildx(*, image_tag: str, context_dir: Path, cwd: Path, log) -> None:
        del image_tag, context_dir, cwd, log
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
