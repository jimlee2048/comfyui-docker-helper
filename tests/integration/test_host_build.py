"""CLI tests for ``cdh host build``."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.config import (
    ComfyCliVersionCandidate,
    ComfyUIReleaseCandidate,
    RegistryInstallMetadata,
    RegistryVersionCandidate,
    SourceResolvers,
    load_validate_plan_result,
    parse_lockfile_toml,
)
from comfyui_docker_helper.host.buildx import BuildxBuildResult
from comfyui_docker_helper.rendering import has_valid_context_marker

COMMIT_1 = "1" * 40
COMMIT_2 = "2" * 40

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


@dataclass(frozen=True, slots=True)
class BuildWorkflowComfyUIProvider:
    """Configurable ComfyUI resolver for host build workflow tests."""

    version: str = "0.26.0"
    commit: str = COMMIT_1

    def list_releases(self) -> Sequence[ComfyUIReleaseCandidate]:
        return [ComfyUIReleaseCandidate(version=self.version, commit=self.commit)]

    def get_nightly_commit(self) -> str:
        return self.commit


@dataclass(frozen=True, slots=True)
class BuildWorkflowComfyCliProvider:
    """Configurable comfy-cli resolver for host build workflow tests."""

    version: str = "1.5.0"

    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        return [ComfyCliVersionCandidate(version=self.version)]


class FailingComfyUIProvider:
    """Provider that fails if strict locked mode performs resolution."""

    def list_releases(self) -> Sequence[ComfyUIReleaseCandidate]:
        raise AssertionError("ComfyUI resolver should not be called")

    def get_nightly_commit(self) -> str:
        raise AssertionError("ComfyUI resolver should not be called")


class FailingComfyCliProvider:
    """Provider that fails if strict locked mode performs resolution."""

    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        raise AssertionError("comfy-cli resolver should not be called")


class FailingRegistryProvider:
    """Provider that fails if strict locked mode performs resolution."""

    def get_install_metadata(
        self,
        node_id: str,
        version: str | None = None,
    ) -> RegistryInstallMetadata:
        del node_id, version
        raise AssertionError("registry resolver should not be called")

    def list_versions(self, node_id: str) -> Sequence[RegistryVersionCandidate]:
        del node_id
        raise AssertionError("registry resolver should not be called")


class FailingGitProvider:
    """Provider that fails if strict locked mode performs resolution."""

    def resolve_default_branch_head(self, url: str) -> str:
        del url
        raise AssertionError("git resolver should not be called")

    def resolve_ref(self, url: str, ref: str) -> str:
        del url, ref
        raise AssertionError("git resolver should not be called")


def failing_source_resolvers() -> SourceResolvers:
    """Return resolvers that fail on every provider boundary call."""
    return SourceResolvers(
        comfyui=FailingComfyUIProvider(),
        comfy_cli=FailingComfyCliProvider(),
        registry=FailingRegistryProvider(),
        git=FailingGitProvider(),
    )


# Build orchestration tests keep render, config precedence, and Buildx argv
# handoff aligned at the CLI boundary.
def test_build_help_exposes_current_options(cli_runner: CliRunner) -> None:
    """Expose the supported build command options."""
    result = cli_runner.invoke(app, ["host", "build", "--help"])

    assert result.exit_code == 0
    assert "Usage: cdh host build" in result.stdout
    assert "-f" in result.stdout
    assert "--file" in result.stdout
    assert "-t" in result.stdout
    assert "--tag" in result.stdout
    normalized_stdout = " ".join(result.stdout.split())
    for token in ("May", "repeated", "replaces", "config", "build", "tags"):
        assert token in normalized_stdout
    assert "--load" in result.stdout
    assert "--push" in result.stdout
    assert "--locked" in result.stdout
    assert "--upgrade-lock" in result.stdout
    assert "--scripts-dir" in result.stdout
    assert "--hooks-dir" in result.stdout
    assert "--context-dir" in result.stdout
    assert "--output" not in result.stdout
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
    assert calls == [(("demo:dev",), "load", Path(".cdh/build/current"), tmp_path)]
    assert "Build context: .cdh/build/current" in result.stdout
    assert "fake buildx loaded demo:dev" in result.stdout


def test_build_uses_one_validated_config_snapshot_for_tags_output_and_render(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not reload config after tags/output have been resolved."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
listen = "127.0.0.1"

[build]
tags = ["demo:from-first-load"]
output = "push"
""",
    )
    context = tmp_path / "context"
    real_load_validate_plan_result = load_validate_plan_result
    load_calls = 0
    build_calls: list[tuple[tuple[str, ...], str]] = []

    def mutating_load(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        result = real_load_validate_plan_result(*args, **kwargs)
        config.write_text("not valid toml = [\n", encoding="utf-8")
        return result

    def fake_buildx(
        *,
        image_tags: tuple[str, ...],
        output: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        del context_dir, cwd, log
        build_calls.append((image_tags, output))
        return BuildxBuildResult(
            argv=("docker", "buildx", "build", "--push", *image_tags),
            context_dir=context,
            image_tags=image_tags,
            output=output,
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.load_validate_plan_result",
        mutating_load,
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.create_default_source_resolvers",
        lambda: SourceResolvers(
            comfyui=BuildWorkflowComfyUIProvider(),
            comfy_cli=BuildWorkflowComfyCliProvider(),
            registry=FailingRegistryProvider(),
            git=FailingGitProvider(),
        ),
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
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 0
    assert load_calls == 1
    assert build_calls == [(("demo:from-first-load",), "push")]
    runtime_config = tomllib.loads((context / "runtime" / "config.toml").read_text())
    assert runtime_config["comfyui"]["listen"] == "127.0.0.1"
    assert (context / "Dockerfile").is_file()


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


def test_build_with_runtime_download_mode_renders_and_invokes_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bake async runtime scheduling while warning that host downloads are sync."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[cdh]
default_download_mode = "async"

[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
download_mode = "async"
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
    assert "host_build.download_scheduling_ignored" in result.stderr
    assert "downloads run synchronously" in result.stderr
    assert has_valid_context_marker(context)
    runtime_config = tomllib.loads(
        (context / "runtime" / "config.toml").read_text(encoding="utf-8")
    )
    assert runtime_config["cdh"]["default_download_mode"] == "async"
    assert runtime_config["files"][0]["download_mode"] == "async"
    assert calls == [("demo:warning",)]


def test_build_time_downloads_do_not_require_runtime_state(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host build file downloads render without runtime state wiring."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
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
            str(config),
            "-t",
            "demo:files",
            "--context-dir",
            str(context),
        ],
    )

    assert result.exit_code == 0
    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    assert "cdh container download-files" in dockerfile
    assert "/var/lib/cdh/runtime/state.json" not in dockerfile
    assert ".cdh-staging" not in dockerfile


# Precedence tests cover CLI overrides; config-only tags/output are covered by the
# single validated snapshot test above.
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


# Lock-mode build tests ensure resolver policy is settled before Buildx starts.
def test_build_locked_reuses_existing_context_lock_without_resolver_calls(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict locked host build reuses the context lock and still calls Buildx."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[build]
tags = ["locked:one", "locked:two"]
output = "push"
""",
    )
    context = tmp_path / "context"
    calls: list[tuple[tuple[str, ...], str, Path]] = []

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: calls.append(
            (kwargs["image_tags"], kwargs["output"], kwargs["context_dir"])
        ),
    )

    initial = cli_runner.invoke(
        app,
        ["host", "build", "-f", str(config), "--context-dir", str(context)],
    )
    assert initial.exit_code == 0
    assert calls == [(("locked:one", "locked:two"), "push", context)]
    original_lock = (context / "config.lock.toml").read_text(encoding="utf-8")

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.create_default_source_resolvers",
        failing_source_resolvers,
    )

    locked = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "--locked",
            "-f",
            str(config),
            "--context-dir",
            str(context),
        ],
    )

    assert locked.exit_code == 0
    assert calls == [
        (("locked:one", "locked:two"), "push", context),
        (("locked:one", "locked:two"), "push", context),
    ]
    assert (context / "config.lock.toml").read_text(encoding="utf-8") == original_lock


def test_build_upgrade_lock_updates_context_lock_before_buildx(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrade-lock host build refreshes moving selections before Buildx."""
    config = write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[build]
tags = ["upgrade:locked"]
output = "load"
""",
    )
    context = tmp_path / "context"

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.create_default_source_resolvers",
        lambda: SourceResolvers(
            comfyui=BuildWorkflowComfyUIProvider(version="0.26.0", commit=COMMIT_1),
            comfy_cli=BuildWorkflowComfyCliProvider(version="1.5.0"),
            registry=FailingRegistryProvider(),
            git=FailingGitProvider(),
        ),
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        lambda **kwargs: BuildxBuildResult(
            argv=("docker",),
            context_dir=kwargs["context_dir"],
            image_tags=kwargs["image_tags"],
            output=kwargs["output"],
        ),
    )

    initial = cli_runner.invoke(
        app,
        ["host", "build", "-f", str(config), "--context-dir", str(context)],
    )
    assert initial.exit_code == 0
    original_lock = parse_lockfile_toml(
        (context / "config.lock.toml").read_text(encoding="utf-8")
    )
    assert original_lock.comfyui.version == "0.26.0"
    assert original_lock.comfyui.commit == COMMIT_1

    calls: list[tuple[tuple[str, ...], str]] = []

    def assert_upgraded_lock_before_buildx(
        *,
        image_tags: tuple[str, ...],
        output: str,
        context_dir: Path,
        cwd: Path,
        log,
    ) -> BuildxBuildResult:
        del cwd, log
        lockfile = parse_lockfile_toml(
            (context_dir / "config.lock.toml").read_text(encoding="utf-8")
        )
        assert lockfile.comfyui.version == "0.27.0"
        assert lockfile.comfyui.commit == COMMIT_2
        assert lockfile.comfyui.cli_version == "2.0.0"
        calls.append((image_tags, output))
        return BuildxBuildResult(
            argv=("docker",),
            context_dir=context_dir,
            image_tags=image_tags,
            output=output,
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.create_default_source_resolvers",
        lambda: SourceResolvers(
            comfyui=BuildWorkflowComfyUIProvider(version="0.27.0", commit=COMMIT_2),
            comfy_cli=BuildWorkflowComfyCliProvider(version="2.0.0"),
            registry=FailingRegistryProvider(),
            git=FailingGitProvider(),
        ),
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.build_image_with_buildx",
        assert_upgraded_lock_before_buildx,
    )

    upgraded = cli_runner.invoke(
        app,
        [
            "host",
            "build",
            "--upgrade-lock",
            "-f",
            str(config),
            "--context-dir",
            str(context),
        ],
    )

    assert upgraded.exit_code == 0
    assert calls == [(("upgrade:locked",), "load")]
    upgraded_lock = parse_lockfile_toml(
        (context / "config.lock.toml").read_text(encoding="utf-8")
    )
    assert upgraded_lock.comfyui.version == "0.27.0"
    assert upgraded_lock.comfyui.commit == COMMIT_2


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
    ("tag", "expected_message"),
    [
        ("", "must be non-empty"),
        ("bad tag", "whitespace or control characters"),
    ],
)
def test_build_rejects_invalid_cli_tag_before_rendering(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
    expected_message: str,
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
    assert expected_message in result.output
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
