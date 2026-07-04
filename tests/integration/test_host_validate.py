"""CLI tests for ``cdh host validate`` and Rich diagnostics."""

import tomllib
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from comfyui_docker_helper.cli import app
from comfyui_docker_helper.config import (
    Config,
    ConfigurationResult,
    build_render_plan,
    project_runtime_config,
)
from comfyui_docker_helper.host.diagnostics import render_plan_preview
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


def _write_config(root: Path, document: str = MINIMAL_CONFIG) -> Path:
    path = root / "config.toml"
    path.write_text(document, encoding="utf-8")
    return path


def test_validate_help_exposes_current_options(cli_runner: CliRunner) -> None:
    """Expose the current validate command options."""
    result = cli_runner.invoke(app, ["host", "validate", "--help"])

    assert result.exit_code == 0
    assert "Usage: cdh host validate" in result.stdout
    assert "-f" in result.stdout
    assert "--file" in result.stdout
    assert "--scripts-dir" in result.stdout


def test_render_help_exposes_current_options(cli_runner: CliRunner) -> None:
    """Expose render options and keep unsafe overwrite shortcuts unavailable."""
    result = cli_runner.invoke(app, ["host", "render", "--help"])

    assert result.exit_code == 0
    assert "Usage: cdh host render" in result.stdout
    assert "-f" in result.stdout
    assert "--file" in result.stdout
    assert "-o" in result.stdout
    assert "--output" in result.stdout
    assert "--scripts-dir" in result.stdout
    assert "--hooks-dir" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--overwrite" in result.stdout
    assert "--force" not in result.stdout


def test_validate_requires_at_least_one_file_option(cli_runner: CliRunner) -> None:
    """Require explicit config files instead of accepting positional input."""
    missing = cli_runner.invoke(app, ["host", "validate"])
    positional = cli_runner.invoke(app, ["host", "validate", "config.toml"])

    assert missing.exit_code == 2
    assert "Missing option" in missing.output
    assert positional.exit_code == 2
    assert "Usage: cdh host validate" in positional.output


@pytest.mark.parametrize(
    "file_args",
    [
        ["-f", "first.toml", "-f", "second.toml"],
        ["--file", "first.toml", "-f", "second.toml"],
        ["-f", "first.toml", "--file", "second.toml"],
        ["--file", "first.toml", "--file", "second.toml"],
    ],
)
def test_validate_accepts_repeated_file_options_in_order(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    file_args: list[str],
) -> None:
    """Preserve repeated -f/--file order before config loading."""
    calls: list[tuple[list[Path], Path]] = []

    def fake_load_validate_plan_result(
        config_files: list[Path], *, scripts_dir: Path
    ) -> ConfigurationResult:
        calls.append((config_files, scripts_dir))
        config = Config.model_validate(
            {
                "compute_platform": {
                    "type": "cuda",
                    "cuda": {"version": "12.9.2"},
                },
                "pytorch": {"version": "2.10"},
                "comfyui": {"version": "latest"},
            }
        )
        return ConfigurationResult(
            config=config,
            plan=build_render_plan(config),
            raw_document={},
            runtime_config=project_runtime_config(config, {}),
        )

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.load_validate_plan_result",
        fake_load_validate_plan_result,
    )

    result = cli_runner.invoke(app, ["host", "validate", *file_args])

    assert result.exit_code == 0
    assert result.output == ""
    assert calls == [([Path("first.toml"), Path("second.toml")], Path("scripts"))]


@pytest.mark.parametrize(
    "args",
    [
        ["-f", "config.toml", "-o", "one", "-o", "two"],
    ],
)
def test_render_rejects_repeated_singleton_options_before_loading(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    """Reject repeated singleton options before any filesystem reads."""
    called = False

    def fail_if_called(*args, **kwargs) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.load_validate_plan_result",
        fail_if_called,
    )

    result = cli_runner.invoke(app, ["host", "render", *args])

    assert result.exit_code == 2
    assert "must be provided exactly once" in result.output
    assert "Usage: cdh host render" in result.output
    assert called is False


def test_valid_input_is_silent_and_writes_nothing(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """Keep successful validation quiet and free of side effects."""
    path = _write_config(tmp_path)
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}

    result = cli_runner.invoke(app, ["host", "validate", "-f", str(path)])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.output == ""
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before


def test_valid_runtime_download_mode_input_warns_for_build_time_files(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """Runtime download-mode fields are valid host inputs for baked defaults."""
    path = _write_config(
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

    result = cli_runner.invoke(app, ["host", "validate", "-f", str(path)])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert "Configuration has warnings:" in result.stderr
    assert "[cdh.default_download_mode]" in result.stderr
    assert "[files.0.download_mode]" in result.stderr
    assert "host_build.download_scheduling_ignored" in result.stderr
    assert "downloads run synchronously" in result.stderr


def test_validate_renders_explicit_continue_build_file_warning(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Render host-context warnings without failing validation."""
    path = _write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[cdh]
download_failure_policy = "continue"

[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )

    result = cli_runner.invoke(app, ["host", "validate", "-f", str(path)])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert "Configuration has warnings:" in result.stderr
    assert "[cdh.download_failure_policy]" in result.stderr
    assert "host_build.download_failure_policy_continue" in result.stderr


def test_render_with_runtime_download_mode_writes_context(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """Render runtime file download-mode defaults into the build context."""
    path = _write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
download_mode = "async"
""",
    )
    output = tmp_path / "context"

    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(path), "-o", str(output)],
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert "host_build.download_scheduling_ignored" in result.stderr
    assert has_valid_context_marker(output)
    rendered_runtime = tomllib.loads(
        (output / "runtime" / "config.toml").read_text(encoding="utf-8")
    )
    assert rendered_runtime["files"][0]["download_mode"] == "async"


def test_invalid_input_renders_every_diagnostic_to_stderr(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """Render aggregated validation failures as user-facing Rich diagnostics."""
    path = _write_config(
        tmp_path,
        MINIMAL_CONFIG.replace('type = "cuda"', 'type = "rocm"')
        + """
[system]
workspace = "relative"

[system.env]
PATH = "unsafe"

[[files]]
url = "https://example.com/file"
dir = "/absolute"
filename = "file.bin"
""",
    )

    result = cli_runner.invoke(app, ["host", "validate", "--file", str(path)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Configuration is invalid:" in result.stderr
    assert "[compute_platform.type]" in result.stderr
    assert "[system.workspace]" in result.stderr
    assert "[system.env.PATH]" in result.stderr
    assert "[files.0.dir]" in result.stderr
    assert "ValidationError" not in result.stderr
    assert "input_value" not in result.stderr
    assert '"errors"' not in result.stderr


def test_render_invalid_input_uses_same_rich_diagnostics(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Share validation diagnostics between validate and render commands."""
    path = _write_config(
        tmp_path,
        MINIMAL_CONFIG.replace('type = "cuda"', 'type = "rocm"'),
    )
    output = tmp_path / "context"

    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(path), "-o", str(output)],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Configuration is invalid:" in result.stderr
    assert "[compute_platform.type]" in result.stderr
    assert not output.exists()


def test_structural_paths_are_human_facing_and_hide_union_branch_tags(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """Report config paths in terms users wrote, not Pydantic internals."""
    path = _write_config(
        tmp_path,
        MINIMAL_CONFIG.replace('version = "2.10"', "extra_packages = []")
        + """
[[comfyui.custom_nodes]]
type = "registry"
id = "node"
url = "https://example.com/node.git"
""",
    )

    result = cli_runner.invoke(app, ["host", "validate", "-f", str(path)])

    assert result.exit_code == 1
    assert "[pytorch.version]" in result.stderr
    assert "[comfyui.custom_nodes.0.url]" in result.stderr
    assert "custom_nodes.0.registry" not in result.stderr


def test_malformed_and_missing_files_use_same_rich_error_shape(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """Render TOML parse and file-read failures through the same error path."""
    malformed = _write_config(tmp_path, "[compute_platform\n")

    malformed_result = cli_runner.invoke(
        app, ["host", "validate", "-f", str(malformed)]
    )
    missing_result = cli_runner.invoke(
        app, ["host", "validate", "-f", str(tmp_path / "missing.toml")]
    )

    assert malformed_result.exit_code == 1
    assert "[config]" in malformed_result.stderr
    assert "toml.invalid_document" in malformed_result.stderr
    assert missing_result.exit_code == 1
    assert "[config]" in missing_result.stderr
    assert "config.file_not_found" in missing_result.stderr


def test_multi_file_read_errors_show_the_failing_file_path(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """Identify the failing source file when layered config loading fails."""
    base = _write_config(tmp_path)
    missing = tmp_path / "missing-override.toml"

    result = cli_runner.invoke(
        app,
        ["host", "validate", "-f", str(base), "-f", str(missing)],
    )

    assert result.exit_code == 1
    assert "Configuration is invalid:" in result.stderr
    assert missing.stem in result.stderr
    assert "config.file_not_found" in result.stderr


def test_invalid_utf8_uses_rich_stderr_and_nonzero_exit(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """Convert invalid UTF-8 into a safe user-facing config diagnostic."""
    path = tmp_path / "config.toml"
    path.write_bytes(b'[compute_platform]\ntype = "cuda"\n\xff')

    result = cli_runner.invoke(app, ["host", "validate", "-f", str(path)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Configuration is invalid:" in result.stderr
    assert "[config]" in result.stderr
    assert "configuration file must be valid UTF-8" in result.stderr
    assert "toml.invalid_encoding" in result.stderr
    assert "UnicodeDecodeError" not in result.stderr


def test_scripts_dir_option_controls_hook_validation(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    """Resolve hook scripts from the explicit scripts directory."""
    path = _write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[[comfyui.custom_nodes]]
type = "registry"
id = "node"
pre_install_scripts = ["hook.sh"]
""",
    )
    scripts = tmp_path / "hooks"
    scripts.mkdir()
    (scripts / "hook.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    result = cli_runner.invoke(
        app,
        ["host", "validate", "-f", str(path), "--scripts-dir", str(scripts)],
    )

    assert result.exit_code == 0
    assert result.output == ""


def test_no_hooks_ignore_missing_default_scripts_dir(
    cli_runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Avoid touching the default scripts directory when no hooks exist."""
    path = _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = cli_runner.invoke(app, ["host", "validate", "-f", str(path)])

    assert result.exit_code == 0
    assert result.output == ""


def test_render_dry_run_prints_preview_and_writes_nothing(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Show a full plan preview without creating an output directory."""
    path = _write_config(tmp_path)
    output = tmp_path / "missing" / "context"

    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(path), "-o", str(output), "--dry-run"],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "Build plan preview" in result.stdout
    assert "Base image: nvidia/cuda:12.9.2-cudnn-devel-ubuntu24.04" in result.stdout
    assert "Workspace: /workspace" in result.stdout
    assert "UV_IMAGE_TAG=latest" in result.stdout
    assert ".cdh-rendered [file]" in result.stdout
    assert "packages/cdh/src [tree]" in result.stdout
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    ("lock_flag", "mode", "resolution"),
    [
        ("--locked", "Mode: locked + dry-run", "Resolution: no update; no resolution"),
        (
            "--upgrade-lock",
            "Mode: upgrade + dry-run",
            "Resolution: re-resolve moving selectors",
        ),
    ],
)
def test_render_dry_run_with_lock_flags_reports_behavior_and_writes_nothing(
    cli_runner: CliRunner,
    tmp_path: Path,
    lock_flag: str,
    mode: str,
    resolution: str,
) -> None:
    """Dry-run lock flags report effective lock behavior without writing files."""
    path = _write_config(tmp_path)
    output = tmp_path / "context"
    rendered = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(path), "-o", str(output)],
    )
    assert rendered.exit_code == 0
    before = {
        item.relative_to(output).as_posix(): item.read_bytes()
        for item in output.rglob("*")
        if item.is_file()
    }

    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(path), "-o", str(output), "--dry-run", lock_flag],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "Lock:" in result.stdout
    assert mode in result.stdout
    assert resolution in result.stdout
    assert "Write: no (dry-run)" in result.stdout
    assert "ComfyUI: 0.26.0 @" in result.stdout
    assert "comfy-cli: 1.5.0" in result.stdout
    assert {
        item.relative_to(output).as_posix(): item.read_bytes()
        for item in output.rglob("*")
        if item.is_file()
    } == before


def test_render_dry_run_merges_repeated_file_options(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Apply layered config inputs to dry-run previews."""
    base = _write_config(tmp_path)
    override = tmp_path / "override.toml"
    override.write_text(
        """
[system]
workspace = "/srv"
""",
        encoding="utf-8",
    )
    output = tmp_path / "context"

    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(base),
            "-f",
            str(override),
            "-o",
            str(output),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Workspace: /srv" in result.stdout
    assert not output.exists()


@pytest.mark.parametrize(
    ("extra_flag", "message"),
    [
        ("--dry-run", "--check cannot be combined with --dry-run"),
        ("--upgrade-lock", "--check cannot be combined with --upgrade-lock"),
    ],
)
def test_render_check_rejects_incompatible_flags_at_cli_level(
    cli_runner: CliRunner,
    tmp_path: Path,
    extra_flag: str,
    message: str,
) -> None:
    """Render CLI surfaces shared lock option compatibility diagnostics."""
    path = _write_config(tmp_path)
    output = tmp_path / "context"

    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(path), "-o", str(output), "--check", extra_flag],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Configuration is invalid:" in result.stderr
    assert "lock.options_incompatible" in result.stderr
    assert message in result.stderr
    assert not output.exists()


def test_render_plan_preview_has_stable_full_shape() -> None:
    """Lock the minimal dry-run preview order and labels."""
    plan = build_render_plan(Config.model_validate(toml_minimal_config()))
    output = StringIO()
    console = Console(
        file=output,
        width=240,
        highlight=False,
        markup=False,
        color_system=None,
    )

    render_plan_preview(plan, console=console)

    expected = "\n".join(
        [
            "Build plan preview",
            "Base image: nvidia/cuda:12.9.2-cudnn-devel-ubuntu24.04",
            "",
            "Paths:",
            "  Workspace: /workspace",
            "  ComfyUI: /workspace/ComfyUI",
            "  Virtualenv: /opt/venv",
            "",
            "OS packages:",
            "  bash, ca-certificates, curl, git, build-essential, aria2",
            "",
            "Python:",
            "  Version: 3.12",
            "  uv image tag: latest",
            "  Index URL: https://pypi.org/simple",
            "  Extra packages: none",
            "",
            "PyTorch:",
            "  Version: 2.10",
            "  Wheel tag: cu129",
            "  Index URL: https://download.pytorch.org/whl/cu129",
            "  Requirements: torch==2.10",
            "",
            "ComfyUI:",
            "  comfy-cli: comfy-cli",
            "  ComfyUI version: latest",
            "  Manager: enabled",
            (
                "  Install args: --nvidia, --version, latest, "
                "--skip-torch-or-directml, --fast-deps"
            ),
            (
                "  Launch command: python, /workspace/ComfyUI/main.py, "
                "--listen, 0.0.0.0, --port, 8188, --disable-auto-launch"
            ),
            "",
            "Environment:",
            "  none",
            "",
            "Custom nodes:",
            "  Update cache: no",
            "  none",
            "",
            "Files:",
            "  Default downloader: aria2",
            "  none",
            "",
            "Build arguments:",
            "  UV_IMAGE_TAG=latest",
            "  CUDA_IMAGE_TAG=12.9.2-cudnn-devel-ubuntu24.04",
            "  PYTHON_VERSION=3.12",
            "  PYTORCH_VERSION=2.10",
            "  PYTORCH_WHEEL_TAG=cu129",
            "  COMFY_CLI_VERSION=latest",
            "  COMFYUI_VERSION=latest",
            "  UV_LINK_MODE=copy",
            "  UV_PYTHON_CACHE_DIR=/root/.cache/uv/python",
            "",
            "Layers:",
            "  - base-and-uv",
            "  - os-packages",
            "  - workspace-directories",
            "  - python-venv",
            "  - pytorch",
            "  - comfy-cli",
            "  - comfyui",
            "  - cdh",
            "  - final",
            "",
            "Output manifest:",
            "  - config.toml [file]",
            "  - config.lock.toml [file]",
            "  - Dockerfile [file]",
            "  - .cdh-rendered [file]",
            "  - runtime/config.toml [file]",
            "  - packages/cdh/pyproject.toml [file]",
            "  - packages/cdh/src [tree]",
            "",
        ]
    )
    assert output.getvalue() == expected


def test_render_writes_marked_context_with_recursive_parent_creation(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Materialize a valid marked build context under missing parents."""
    path = _write_config(tmp_path)
    output = tmp_path / "missing" / "parent" / "context"

    result = cli_runner.invoke(
        app, ["host", "render", "-f", str(path), "-o", str(output)]
    )

    assert result.exit_code == 0
    assert result.output == ""
    assert has_valid_context_marker(output)
    assert (output / "Dockerfile").is_file()
    assert (output / "packages" / "cdh" / "pyproject.toml").is_file()
    assert not (output / "normalized.toml").exists()


def test_render_cli_writes_conditional_configs_and_scripts(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Write root artifacts and scripts only when render-plan features need them."""
    path = _write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[[comfyui.custom_nodes]]
type = "registry"
id = "node-one"
pre_install_scripts = ["hook.sh"]

[[files]]
url = "https://example.com/model.safetensors"
dir = "models/checkpoints"
filename = "model.safetensors"
""",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "hook.sh").write_text("#!/bin/sh\n")
    (scripts / "unused.txt").write_text("copy whole scripts tree\n")
    output = tmp_path / "context"

    result = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(path),
            "-o",
            str(output),
            "--scripts-dir",
            str(scripts),
        ],
    )

    assert result.exit_code == 0
    assert has_valid_context_marker(output)
    assert (output / "config.toml").is_file()
    assert (output / "config.lock.toml").is_file()
    assert not (output / "config" / "custom-nodes.toml").exists()
    assert not (output / "config" / "files.toml").exists()
    assert (output / "scripts" / "hook.sh").read_text() == "#!/bin/sh\n"
    assert (output / "scripts" / "unused.txt").read_text() == (
        "copy whole scripts tree\n"
    )


def test_render_overwrite_replaces_only_valid_marked_contexts(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Protect caller-owned directories while allowing marked context replacement."""
    path = _write_config(tmp_path)
    output = tmp_path / "context"

    first = cli_runner.invoke(
        app, ["host", "render", "-f", str(path), "-o", str(output)]
    )
    assert first.exit_code == 0
    (output / "old.txt").write_text("replace\n")

    blocked = cli_runner.invoke(
        app, ["host", "render", "-f", str(path), "-o", str(output)]
    )
    assert blocked.exit_code == 1
    assert "pass --overwrite" in blocked.stderr
    assert (output / "old.txt").read_text() == "replace\n"

    overwritten = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(path), "-o", str(output), "--overwrite"],
    )
    assert overwritten.exit_code == 0
    assert has_valid_context_marker(output)
    assert not (output / "old.txt").exists()


def test_render_rejects_unmarked_output_even_with_overwrite(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Never delete an unmarked existing output directory."""
    path = _write_config(tmp_path)
    output = tmp_path / "context"
    output.mkdir()
    (output / "caller-owned.txt").write_text("preserve\n")

    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(path), "-o", str(output), "--overwrite"],
    )

    assert result.exit_code == 1
    assert "not a valid cdh build context" in result.stderr
    assert (output / "caller-owned.txt").read_text() == "preserve\n"


def test_render_rejects_force_shortcut_for_overwrite_safety(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Reject force-style overwrite shortcuts that bypass marker safety."""
    path = _write_config(tmp_path)
    output = tmp_path / "context"

    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(path), "-o", str(output), "--force"],
    )

    assert result.exit_code == 2
    assert "No such option" in result.output
    assert not output.exists()


def toml_minimal_config() -> dict[str, object]:
    """Return the minimal config document as parsed TOML-like data."""
    return {
        "compute_platform": {
            "type": "cuda",
            "cuda": {"version": "12.9.2"},
        },
        "pytorch": {"version": "2.10"},
        "comfyui": {"version": "latest"},
    }
