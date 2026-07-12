"""Tests for low-level deterministic build-context materialization."""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from dataclasses import replace
from importlib import metadata
from pathlib import Path

import pytest
from tests.artifact_helpers import make_lockfile

import comfyui_docker_helper.rendering.context as context_module
from comfyui_docker_helper.config import (
    Config,
    FileConfig,
    OutputArtifact,
    OutputManifest,
    RegistryCustomNodeConfig,
    RuntimeHooksPlan,
    build_render_plan,
    with_runtime_hooks_plan,
)
from comfyui_docker_helper.config.plan import ArtifactKind
from comfyui_docker_helper.rendering import (
    ContextWriteError,
    MaterializationError,
    has_valid_context_marker,
    materialize_build_context,
    materialize_expected_build_context,
    render_dockerfile,
    write_build_context,
)


def make_config() -> Config:
    """Return a fresh minimal valid public configuration."""
    return Config.model_validate(
        {
            "compute_platform": {
                "type": "cuda",
                "cuda": {"version": "12.9.2"},
            },
            "pytorch": {"version": "2.10"},
            "comfyui": {"version": "latest"},
        }
    )


def add_node(config: Config, *, hooks: bool = False) -> None:
    """Add one registry node, optionally with one hook."""
    data: dict[str, object] = {"type": "registry", "id": "node-one"}
    if hooks:
        data["pre_install_scripts"] = ["pre.sh"]
    config.comfyui.custom_nodes = [RegistryCustomNodeConfig.model_validate(data)]


def add_file(config: Config) -> None:
    """Add one file with downloader defaults left for plan normalization."""
    config.files = [
        FileConfig(
            url="https://example.com/models/model.safetensors",
            dir="models/checkpoints",
            filename="model.safetensors",
        )
    ]


def materialize(
    tmp_path: Path,
    config: Config,
    *,
    name: str = "staging",
    scripts: Path | None = None,
) -> tuple[Path, object]:
    """Build a plan and materialize it into a fresh staging directory."""
    plan = build_render_plan(config, scripts_dir=scripts)
    staging = tmp_path / name
    staging.mkdir()
    materialize_build_context(
        plan,
        staging,
        config=config,
        lockfile=make_lockfile(config),
    )
    return staging, plan


def write_context(
    tmp_path: Path,
    config: Config,
    output: Path,
    *,
    overwrite: bool = False,
    scripts: Path | None = None,
    config_file: Path | None = None,
) -> None:
    """Build a plan and write a marked context relative to a controlled cwd."""
    plan = build_render_plan(config, scripts_dir=scripts)
    write_build_context(
        plan,
        output,
        config=config,
        lockfile=make_lockfile(config),
        overwrite=overwrite,
        working_directory=tmp_path,
        config_file=config_file,
    )


def write_valid_marker(directory: Path) -> None:
    """Write the current context marker used by write_build_context."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".cdh-rendered").write_text(
        '{"kind":"build-context","tool":"comfyui-docker-helper","version":"0.1"}\n',
        encoding="utf-8",
    )


def context_inventory(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    """Return a deterministic file/dir inventory with content hashes and modes."""
    entries: list[tuple[str, str, int, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.stat(follow_symlinks=False).st_mode
        permissions = stat.S_IMODE(mode)
        if stat.S_ISDIR(mode):
            entries.append((relative, "dir", permissions, ""))
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((relative, "file", permissions, digest))
    return tuple(entries)


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and expose useful output when it fails."""
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "command failed: "
            + " ".join(args)
            + "\nstdout:\n"
            + result.stdout
            + "\nstderr:\n"
            + result.stderr
        )
    return result


def test_minimal_context_projects_running_distribution_without_readme(
    tmp_path: Path,
) -> None:
    """Project package resources and generated metadata, not a repository checkout."""
    staging, plan = materialize(tmp_path, make_config())

    assert (staging / "Dockerfile").read_text() == render_dockerfile(
        plan,
        lockfile=make_lockfile(make_config()),
    )
    package = staging / "packages" / "cdh"
    assert {path.name for path in package.iterdir()} == {"pyproject.toml", "src"}
    assert not (package / "README.md").exists()
    assert (
        package / "src" / "comfyui_docker_helper" / "templates" / "Dockerfile.j2"
    ).is_file()
    assert not any(
        path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
        for path in package.rglob("*")
    )
    assert not any(
        path.name.endswith((".dist-info", ".egg-info")) for path in package.rglob("*")
    )
    assert not (staging / "config").exists()
    assert not (staging / "scripts").exists()
    assert not (staging / ".cdh-rendered").exists()
    assert not (staging / "normalized.toml").exists()


def test_generated_pyproject_matches_installed_distribution_metadata(
    tmp_path: Path,
) -> None:
    """Emit a minimal installable pyproject derived from installed metadata."""
    staging, _ = materialize(tmp_path, make_config())
    generated = tomllib.loads(
        (staging / "packages" / "cdh" / "pyproject.toml").read_text()
    )
    distribution = metadata.distribution("comfyui-docker-helper")
    package_metadata = distribution.metadata
    project_pyproject = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert list(generated) == ["project", "build-system", "tool"]
    assert "readme" not in generated["project"]
    assert generated["project"] == {
        "name": package_metadata["Name"],
        "version": package_metadata["Version"],
        "description": package_metadata.get("Summary", ""),
        "requires-python": package_metadata["Requires-Python"],
        "dependencies": list(distribution.requires or ()),
        "scripts": {"cdh": "comfyui_docker_helper.cli:app"},
    }
    assert generated["build-system"] == project_pyproject["build-system"]
    assert generated["tool"]["uv"]["build-backend"] == {
        "module-name": "comfyui_docker_helper"
    }


def test_materialization_is_byte_and_permission_deterministic(tmp_path: Path) -> None:
    """Two materializations produce identical inventories, hashes, and modes."""
    first, _ = materialize(tmp_path, make_config(), name="first")
    second, _ = materialize(tmp_path, make_config(), name="second")

    assert context_inventory(first) == context_inventory(second)


def test_materialized_directories_ignore_process_umask(tmp_path: Path) -> None:
    """Created context directories keep fixed permissions under restrictive umask."""
    config = make_config()
    add_file(config)
    old_umask = os.umask(0o077)
    try:
        staging, _ = materialize(tmp_path, config)
    finally:
        os.umask(old_umask)

    directory_modes = {
        path.relative_to(staging).as_posix(): stat.S_IMODE(path.stat().st_mode)
        for path in staging.rglob("*")
        if path.is_dir()
    }

    assert directory_modes["packages"] == 0o755
    assert directory_modes["packages/cdh"] == 0o755
    assert directory_modes["packages/cdh/src"] == 0o755
    assert directory_modes["packages/cdh/src/comfyui_docker_helper"] == 0o755


# Marker and overwrite tests guard the boundary between CDH-managed
# contexts and caller-owned filesystem content.
def test_write_build_context_recursively_creates_parent_and_marker(
    tmp_path: Path,
) -> None:
    """A missing output path creates parent directories and writes marker last."""
    output = tmp_path / "missing" / "parent" / "context"

    write_context(tmp_path, make_config(), output)

    assert has_valid_context_marker(output)
    assert (output / "Dockerfile").is_file()
    assert (output / "packages" / "cdh" / "pyproject.toml").is_file()
    assert not list(output.parent.glob(".context.cdh-*"))


def test_write_build_context_rejects_existing_unmarked_directory(
    tmp_path: Path,
) -> None:
    """Unmarked directories are never cleared or replaced."""
    output = tmp_path / "context"
    output.mkdir()
    (output / "caller-owned.txt").write_text("preserve\n")

    with pytest.raises(ContextWriteError, match="not a valid cdh build context"):
        write_context(tmp_path, make_config(), output, overwrite=True)

    assert (output / "caller-owned.txt").read_text() == "preserve\n"
    assert not (output / "Dockerfile").exists()
    assert not list(tmp_path.glob(".context.cdh-*"))


def test_write_build_context_rejects_existing_output_file(tmp_path: Path) -> None:
    """Existing non-directory output paths are preserved and rejected."""
    output = tmp_path / "context"
    output.write_text("preserve\n")

    with pytest.raises(ContextWriteError, match="not a directory"):
        write_context(tmp_path, make_config(), output)

    assert output.read_text() == "preserve\n"


def test_write_build_context_rejects_marked_directory_without_overwrite(
    tmp_path: Path,
) -> None:
    """A valid context is preserved unless overwrite is explicitly enabled."""
    output = tmp_path / "context"
    write_valid_marker(output)
    (output / "old.txt").write_text("preserve\n")

    with pytest.raises(ContextWriteError, match="pass --overwrite"):
        write_context(tmp_path, make_config(), output)

    assert (output / "old.txt").read_text() == "preserve\n"


def test_write_build_context_overwrite_replaces_marked_directory_safely(
    tmp_path: Path,
) -> None:
    """Overwriting removes old context contents without following symlinks."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    output = tmp_path / "context"
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete\n")
    write_valid_marker(output)
    (output / "old.txt").write_text("replace\n")
    (output / "outside-link").symlink_to(outside)

    write_context(tmp_path, make_config(), output, overwrite=True)

    assert has_valid_context_marker(output)
    assert (output / "Dockerfile").is_file()
    assert not (output / "old.txt").exists()
    assert not (output / "outside-link").exists()
    assert outside.read_text() == "do not delete\n"


def test_write_build_context_rejects_malformed_and_foreign_markers(
    tmp_path: Path,
) -> None:
    """Malformed or foreign markers never authorize replacement."""
    for name, marker in {
        "malformed": "{not-json",
        "foreign": '{"tool":"other","kind":"build-context","version":"0.1"}',
    }.items():
        output = tmp_path / name
        output.mkdir()
        (output / ".cdh-rendered").write_text(marker, encoding="utf-8")
        (output / "caller-owned.txt").write_text("preserve\n")

        assert not has_valid_context_marker(output)
        with pytest.raises(ContextWriteError, match="not a valid cdh build context"):
            write_context(tmp_path, make_config(), output, overwrite=True)

        assert (output / "caller-owned.txt").read_text() == "preserve\n"


def test_has_valid_context_marker_accepts_unknown_fields(tmp_path: Path) -> None:
    """Forward-compatible marker fields do not invalidate current markers."""
    output = tmp_path / "context"
    output.mkdir()
    (output / ".cdh-rendered").write_text(
        (
            '{"extra":"ignored","kind":"build-context",'
            '"tool":"comfyui-docker-helper","version":"0.1"}\n'
        ),
        encoding="utf-8",
    )

    assert has_valid_context_marker(output)


def test_write_build_context_rejects_marker_directory(
    tmp_path: Path,
) -> None:
    """Non-regular markers never authorize overwrite replacement."""
    output = tmp_path / "context"
    output.mkdir()
    (output / ".cdh-rendered").mkdir()
    (output / "caller-owned.txt").write_text("preserve\n")

    assert not has_valid_context_marker(output)
    with pytest.raises(ContextWriteError, match="not a valid cdh build context"):
        write_context(tmp_path, make_config(), output, overwrite=True)

    assert (output / "caller-owned.txt").read_text() == "preserve\n"


def test_has_valid_context_marker_rejects_symlink_marker(tmp_path: Path) -> None:
    """A symlink marker is never trusted."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    output = tmp_path / "context"
    target = tmp_path / "target-marker"
    output.mkdir()
    target.write_text(
        '{"kind":"build-context","tool":"comfyui-docker-helper","version":"0.1"}\n',
        encoding="utf-8",
    )
    (output / ".cdh-rendered").symlink_to(target)

    assert not has_valid_context_marker(output)


def test_write_build_context_rejects_destructive_output_paths(
    tmp_path: Path,
) -> None:
    """Protect the filesystem root, cwd, config input, scripts source, and symlinks."""
    config = make_config()
    add_node(config, hooks=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre.sh").write_text("#!/bin/sh\n")
    config_file = tmp_path / "config.toml"
    config_file.write_text("[compute_platform]\n")
    symlink_output = tmp_path / "output-link"
    symlink_output.symlink_to(tmp_path)
    plan = build_render_plan(config, scripts_dir=scripts)

    cases = [
        (Path(tmp_path.anchor), "filesystem root"),
        (tmp_path, "ancestor of working directory"),
        (tmp_path / "config-parent", "ancestor of configuration input"),
        (scripts / "nested" / "context", "nested inside the scripts source"),
        (symlink_output, "symlink"),
    ]
    (tmp_path / "config-parent").mkdir()
    nested_config = tmp_path / "config-parent" / "config.toml"
    nested_config.write_text("[compute_platform]\n")

    for output, expected in cases:
        with pytest.raises(ContextWriteError, match=expected):
            write_build_context(
                plan,
                output,
                working_directory=tmp_path,
                config_file=nested_config if "config" in expected else config_file,
            )


def test_write_build_context_rejects_output_ancestor_of_scripts_source(
    tmp_path: Path,
) -> None:
    """Scripts-source ancestry is enforced independently from cwd ancestry."""
    work = tmp_path / "work"
    output = tmp_path / "source-root"
    scripts = output / "scripts"
    work.mkdir()
    scripts.mkdir(parents=True)
    (scripts / "pre.sh").write_text("#!/bin/sh\n")
    config = make_config()
    add_node(config, hooks=True)
    plan = build_render_plan(config, scripts_dir=scripts)

    with pytest.raises(ContextWriteError, match="ancestor of scripts source"):
        write_build_context(plan, output, working_directory=work)


def test_write_build_context_rejects_script_source_symlinks_before_mutation(
    tmp_path: Path,
) -> None:
    """Invalid scripts trees fail before output parents are created."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    config = make_config()
    add_node(config, hooks=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre.sh").write_text("#!/bin/sh\n")
    (scripts / "link.sh").symlink_to(scripts / "pre.sh")
    output = tmp_path / "missing" / "parent" / "context"
    plan = build_render_plan(config, scripts_dir=scripts)

    with pytest.raises(
        ContextWriteError, match="scripts source tree contains a symlink"
    ):
        write_build_context(plan, output, working_directory=tmp_path)

    assert not (tmp_path / "missing").exists()


# Hook traversal diagnostics must stay wrapped as context errors, including
# time-of-check/time-of-use changes after plan validation.
def test_write_build_context_wraps_missing_hook_after_plan_validation(
    tmp_path: Path,
) -> None:
    """A hook removed after plan construction is reported as a context error."""
    config = make_config()
    add_node(config, hooks=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    hook = scripts / "pre.sh"
    hook.write_text("#!/bin/sh\n")
    plan = build_render_plan(config, scripts_dir=scripts)
    hook.unlink()

    with pytest.raises(ContextWriteError, match="referenced hook is missing"):
        write_build_context(
            plan,
            tmp_path / "missing" / "parent" / "context",
            working_directory=tmp_path,
        )

    assert not (tmp_path / "missing").exists()


def test_write_build_context_wraps_runtime_hook_lstat_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime hook source inspection errors are context write failures."""
    hooks = tmp_path / "hooks"
    phase = hooks / "pre-start.d"
    hook = phase / "10-pre.sh"
    phase.mkdir(parents=True)
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    plan = with_runtime_hooks_plan(
        build_render_plan(make_config()),
        RuntimeHooksPlan(has_hooks=True, source_dir=hooks),
    )
    original_lstat = Path.lstat

    def fail_hook_lstat(self: Path):
        if self == hook:
            raise PermissionError("stat denied")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", fail_hook_lstat)

    with pytest.raises(ContextWriteError, match="could not be inspected"):
        write_build_context(
            plan,
            tmp_path / "missing" / "parent" / "context",
            config=make_config(),
            lockfile=make_lockfile(make_config()),
            working_directory=tmp_path,
        )

    assert not (tmp_path / "missing").exists()


# Keep materialization aligned with the host preflight contract for allowed
# runtime hook file extensions.
def test_write_build_context_rejects_unsupported_runtime_hook_extension(
    tmp_path: Path,
) -> None:
    """Materialization validates runtime hook files against the shared contract."""
    hooks = tmp_path / "hooks"
    phase = hooks / "pre-start.d"
    phase.mkdir(parents=True)
    (phase / "note.txt").write_text("nope\n", encoding="utf-8")
    config = make_config()
    plan = with_runtime_hooks_plan(
        build_render_plan(config),
        RuntimeHooksPlan(has_hooks=True, source_dir=hooks),
    )

    with pytest.raises(
        ContextWriteError,
        match=r"runtime hook files must end in \.sh or \.py: pre-start\.d/note\.txt",
    ):
        write_build_context(
            plan,
            tmp_path / "missing" / "parent" / "context",
            config=config,
            lockfile=make_lockfile(config),
            working_directory=tmp_path,
        )

    assert not (tmp_path / "missing").exists()


def test_write_build_context_cleans_created_parents_when_materialization_fails(
    tmp_path: Path,
) -> None:
    """Failed renders remove staging and created output parent directories."""
    config = make_config()
    plan = build_render_plan(config)
    broken_manifest = OutputManifest(
        always=(
            *plan.output_manifest.always,
            OutputArtifact("required-but-unwritten", ArtifactKind.FILE),
        ),
        conditional=plan.output_manifest.conditional,
    )
    output = tmp_path / "created" / "parent" / "context"

    with pytest.raises(MaterializationError, match="missing file artifact"):
        write_build_context(
            replace(plan, output_manifest=broken_manifest),
            output,
            config=config,
            lockfile=make_lockfile(config),
            working_directory=tmp_path,
        )

    assert not (tmp_path / "created").exists()


def test_materialize_build_context_wraps_runtime_hook_copy_lstat_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime hook copy inspection errors are materialization failures."""
    hooks = tmp_path / "hooks"
    phase = hooks / "pre-start.d"
    hook = phase / "10-pre.sh"
    phase.mkdir(parents=True)
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    config = make_config()
    plan = with_runtime_hooks_plan(
        build_render_plan(config),
        RuntimeHooksPlan(has_hooks=True, source_dir=hooks),
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    original_validate = context_module._validate_runtime_hooks_source_tree
    original_lstat = Path.lstat

    def validate_before_lstat_failure(render_plan) -> None:
        original_validate(render_plan)
        monkeypatch.setattr(Path, "lstat", fail_hook_lstat)

    def fail_hook_lstat(self: Path):
        if self == hook:
            raise PermissionError("stat denied")
        return original_lstat(self)

    monkeypatch.setattr(
        context_module,
        "_validate_runtime_hooks_source_tree",
        validate_before_lstat_failure,
    )

    with pytest.raises(MaterializationError, match="could not be inspected"):
        materialize_build_context(
            plan,
            staging,
            config=config,
            lockfile=make_lockfile(config),
        )


# Rollback and temporary-backup tests protect failed renders from replacing
# the previous valid context or leaking recovery state.
def test_expected_context_cleans_temp_dir_when_materialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed check-context materialization removes the temporary directory."""
    config = make_config()
    plan = build_render_plan(config)

    def fail_after_temp_dir_exists(*args, **kwargs) -> None:
        del args, kwargs
        assert list(tmp_path.glob(".cdh-check-*"))
        raise MaterializationError("forced materialization failure")

    monkeypatch.setattr(
        context_module,
        "materialize_build_context",
        fail_after_temp_dir_exists,
    )

    with (
        pytest.raises(MaterializationError, match="forced materialization failure"),
        materialize_expected_build_context(
            plan,
            tmp_path,
            config=config,
            lockfile=make_lockfile(config),
        ),
    ):
        pytest.fail("expected context should not be yielded")

    assert not list(tmp_path.glob(".cdh-check-*"))


def test_write_build_context_preserves_marked_destination_when_materialization_fails(
    tmp_path: Path,
) -> None:
    """A failed overwrite render leaves the previous valid context in place."""
    output = tmp_path / "context"
    write_valid_marker(output)
    (output / "old.txt").write_text("preserve\n")
    config = make_config()
    plan = build_render_plan(config)
    broken_manifest = OutputManifest(
        always=(
            *plan.output_manifest.always,
            OutputArtifact("required-but-unwritten", ArtifactKind.FILE),
        ),
        conditional=plan.output_manifest.conditional,
    )

    with pytest.raises(MaterializationError, match="missing file artifact"):
        write_build_context(
            replace(plan, output_manifest=broken_manifest),
            output,
            config=config,
            lockfile=make_lockfile(config),
            overwrite=True,
            working_directory=tmp_path,
        )

    assert has_valid_context_marker(output)
    assert (output / "old.txt").read_text() == "preserve\n"
    assert not (output / "required-but-unwritten").exists()
    assert not list(tmp_path.glob(".context.cdh-*"))


def test_overwrite_restore_failure_retains_previous_context_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If rollback cannot restore the old context, keep the backup for recovery."""
    output = tmp_path / "context"
    write_valid_marker(output)
    (output / "old.txt").write_text("old context\n", encoding="utf-8")
    staging = tmp_path / ".context.cdh-staging-forced"
    staging.mkdir()
    write_valid_marker(staging)
    (staging / "new.txt").write_text("new context\n", encoding="utf-8")
    original_replace = Path.replace

    def fail_new_context_and_restore(self: Path, target: Path) -> Path:
        if self == staging and target == output:
            raise PermissionError("replace denied")
        if self.name.startswith(".context.cdh-backup-") and target == output:
            raise PermissionError("restore denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_new_context_and_restore)

    with pytest.raises(ContextWriteError) as error:
        context_module._replace_destination(
            staging,
            output,
            overwrite_existing=True,
        )

    backups = sorted(tmp_path.glob(".context.cdh-backup-*"))
    assert len(backups) == 1
    assert f"retained backup: {backups[0]}" in str(error.value)
    assert has_valid_context_marker(backups[0])
    assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "old context\n"
    assert not output.exists()


def test_overwrite_success_removes_previous_context_backup(tmp_path: Path) -> None:
    """Successful overwrite cleanup removes the temporary backup directory."""
    output = tmp_path / "context"
    write_valid_marker(output)
    (output / "old.txt").write_text("old context\n", encoding="utf-8")
    staging = tmp_path / ".context.cdh-staging-forced"
    staging.mkdir()
    write_valid_marker(staging)
    (staging / "new.txt").write_text("new context\n", encoding="utf-8")

    context_module._replace_destination(
        staging,
        output,
        overwrite_existing=True,
    )

    assert has_valid_context_marker(output)
    assert (output / "new.txt").read_text(encoding="utf-8") == "new context\n"
    assert not (output / "old.txt").exists()
    assert not list(tmp_path.glob(".context.cdh-backup-*"))


def test_write_build_context_rejects_script_source_special_files_before_mutation(
    tmp_path: Path,
) -> None:
    """Invalid special files in scripts fail before output parents are created."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are not supported on this platform")
    config = make_config()
    add_node(config, hooks=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre.sh").write_text("#!/bin/sh\n")
    os.mkfifo(scripts / "pipe")
    output = tmp_path / "missing" / "parent" / "context"
    plan = build_render_plan(config, scripts_dir=scripts)

    with pytest.raises(
        ContextWriteError, match="scripts source tree contains a special file"
    ):
        write_build_context(plan, output, working_directory=tmp_path)

    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    ("with_node", "with_file", "with_hooks", "expected"),
    [
        (False, False, False, set()),
        (True, False, False, set()),
        (False, True, False, set()),
        (
            True,
            False,
            True,
            {"scripts/pre.sh", "scripts/unused.txt"},
        ),
        (
            True,
            True,
            True,
            {"scripts/pre.sh", "scripts/unused.txt"},
        ),
    ],
    ids=("minimal", "node", "file", "hook", "full"),
)
def test_feature_artifacts_are_materialized_conditionally(
    tmp_path: Path,
    with_node: bool,
    with_file: bool,
    with_hooks: bool,
    expected: set[str],
) -> None:
    """Materialize minimal, node, file, hook, and full feature trees exactly."""
    config = make_config()
    scripts = None
    if with_node:
        add_node(config, hooks=with_hooks)
    if with_file:
        add_file(config)
    if with_hooks:
        scripts = tmp_path / "hook-source"
        scripts.mkdir()
        (scripts / "pre.sh").write_text("#!/bin/sh\n")
        (scripts / "unused.txt").write_text("whole tree\n")

    staging, _ = materialize(
        tmp_path,
        config,
        scripts=scripts,
    )

    optional = {
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file()
        and path.relative_to(staging).parts[0] in {"config", "scripts"}
    }
    assert optional == expected


# Packaging smoke coverage verifies the projected package works when treated
# as the artifact Docker builds and installs.
def test_generated_package_builds_and_installs_as_a_wheel(tmp_path: Path) -> None:
    """The projected package is independently buildable and installable."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for generated package build/install verification")
    staging, _ = materialize(tmp_path, make_config())
    package = staging / "packages" / "cdh"
    dist_dir = tmp_path / "dist"
    venv = tmp_path / "venv"

    run_command([uv, "build", "--wheel", "--out-dir", str(dist_dir), str(package)])
    wheels = sorted(dist_dir.glob("comfyui_docker_helper-*.whl"))
    assert len(wheels) == 1

    run_command([uv, "venv", str(venv)])
    python = venv / "bin" / "python"
    run_command(
        [uv, "pip", "install", "--python", str(python), "--no-deps", str(wheels[0])]
    )

    assert (venv / "bin" / "cdh").is_file()
    run_command(
        [
            str(python),
            "-c",
            (
                "from importlib import metadata, resources; "
                "assert resources.files('comfyui_docker_helper')"
                ".joinpath('templates', 'Dockerfile.j2').is_file(); "
                "dist = metadata.distribution('comfyui-docker-helper'); "
                "assert any(ep.group == 'console_scripts' and ep.name == 'cdh' "
                "and ep.value == 'comfyui_docker_helper.cli:app' "
                "for ep in dist.entry_points)"
            ),
        ]
    )


def test_project_wheel_install_materializes_from_non_repo_cwd_without_pythonpath(
    tmp_path: Path,
) -> None:
    """A wheel-installed host CLI can project itself without a source checkout."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for wheel-install materialization verification")
    project_root = Path(__file__).resolve().parents[2]
    dist_dir = tmp_path / "project-dist"
    install_target = tmp_path / "installed"
    non_repo_cwd = tmp_path / "non-repo"
    staging = tmp_path / "staging"
    non_repo_cwd.mkdir()
    staging.mkdir()

    run_command(
        [uv, "build", "--wheel", "--out-dir", str(dist_dir), str(project_root)],
        cwd=project_root,
    )
    wheels = sorted(dist_dir.glob("comfyui_docker_helper-*.whl"))
    assert len(wheels) == 1
    run_command(
        [
            uv,
            "pip",
            "install",
            "--target",
            str(install_target),
            "--no-deps",
            str(wheels[0]),
        ]
    )

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    script = """
import os
import sys
from pathlib import Path

install_target = Path(sys.argv[1]).resolve()
project_root = Path(sys.argv[2]).resolve()
staging = Path(sys.argv[3]).resolve()
sys.path.insert(0, str(install_target))

import comfyui_docker_helper
from comfyui_docker_helper.config import (
    COMFYUI_REPO_URL,
    Config,
    LockedComfyUI,
    Lockfile,
    LockManifest,
    build_render_plan,
    compute_lock_input_digest,
    compute_git_custom_nodes_input_digest,
)
from comfyui_docker_helper.rendering import materialize_build_context

package_file = Path(comfyui_docker_helper.__file__).resolve()
assert package_file.is_relative_to(install_target), package_file
assert not package_file.is_relative_to(project_root), package_file
assert "PYTHONPATH" not in os.environ

config = Config.model_validate({
    "compute_platform": {"type": "cuda", "cuda": {"version": "12.9.2"}},
    "pytorch": {"version": "2.10"},
    "comfyui": {"version": "latest"},
})
lockfile = Lockfile(
    schema_version=1,
    manifest=LockManifest(
        lock_input_digest=compute_lock_input_digest(config),
        git_custom_nodes_input_digest=compute_git_custom_nodes_input_digest(config),
    ),
    comfyui=LockedComfyUI(
        repo=COMFYUI_REPO_URL,
        version="0.26.0",
        commit="1" * 40,
        cli_version="1.5.0",
    ),
)
materialize_build_context(
    build_render_plan(config),
    staging,
    config=config,
    lockfile=lockfile,
)
package = staging / "packages" / "cdh"
assert (package / "pyproject.toml").is_file()
assert not (package / "README.md").exists()
template = package / "src" / "comfyui_docker_helper" / "templates" / "Dockerfile.j2"
assert template.is_file()
"""
    run_command(
        [
            sys.executable,
            "-c",
            script,
            str(install_target),
            str(project_root),
            str(staging),
        ],
        cwd=non_repo_cwd,
        env=env,
    )


@pytest.mark.parametrize("kind", ["missing", "file", "nonempty"])
def test_staging_precondition_requires_an_existing_empty_directory(
    tmp_path: Path,
    kind: str,
) -> None:
    """Reject invalid staging paths without deleting caller-owned content."""
    plan = build_render_plan(make_config())
    destination = tmp_path / "destination"
    if kind == "file":
        destination.write_text("caller file\n")
    elif kind == "nonempty":
        destination.mkdir()
        (destination / "caller-owned").write_text("preserve\n")

    with pytest.raises(MaterializationError, match="staging"):
        materialize_build_context(plan, destination)

    if kind == "file":
        assert destination.read_text() == "caller file\n"
    elif kind == "nonempty":
        assert (destination / "caller-owned").read_text() == "preserve\n"
    else:
        assert not destination.exists()


def test_manifest_failure_cleans_only_created_staging_contents(tmp_path: Path) -> None:
    """Keep the staging container and unrelated paths when reconciliation fails."""
    config = make_config()
    plan = build_render_plan(config)
    broken_manifest = OutputManifest(
        always=(
            *plan.output_manifest.always,
            OutputArtifact("required-but-unwritten", ArtifactKind.FILE),
        ),
        conditional=plan.output_manifest.conditional,
    )
    broken_plan = replace(plan, output_manifest=broken_manifest)
    staging = tmp_path / "staging"
    staging.mkdir()
    sibling = tmp_path / "caller-owned"
    sibling.write_text("preserve\n")

    with pytest.raises(MaterializationError, match="missing file artifact"):
        materialize_build_context(
            broken_plan,
            staging,
            config=config,
            lockfile=make_lockfile(config),
        )

    assert staging.is_dir()
    assert list(staging.iterdir()) == []
    assert sibling.read_text() == "preserve\n"


def test_manifest_reconciliation_rejects_unexpected_artifacts(tmp_path: Path) -> None:
    """Fail closed when actual materialization and the plan manifest diverge."""
    config = make_config()
    plan = build_render_plan(config)
    broken_manifest = OutputManifest(
        always=tuple(
            artifact
            for artifact in plan.output_manifest.always
            if artifact.path != "Dockerfile"
        ),
        conditional=plan.output_manifest.conditional,
    )
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(
        MaterializationError,
        match="unexpected artifacts: Dockerfile",
    ):
        materialize_build_context(
            replace(plan, output_manifest=broken_manifest),
            staging,
            config=config,
            lockfile=make_lockfile(config),
        )

    assert list(staging.iterdir()) == []


def test_package_resource_tree_rejects_symlinks(tmp_path: Path) -> None:
    """Package projection fails closed instead of following symlink resources."""
    source = tmp_path / "source"
    source.mkdir()
    target = source / "target.py"
    target.write_text("print('target')\n")
    (source / "link.py").symlink_to(target)

    with pytest.raises(MaterializationError, match="symlink"):
        context_module._copy_plain_tree(
            source,
            tmp_path / "destination",
            "package resource",
            skip_package_cache_entries=True,
        )


def test_package_resource_tree_rejects_special_files(tmp_path: Path) -> None:
    """Package projection fails closed on non-file, non-directory resources."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable on this platform")
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "fifo")

    with pytest.raises(MaterializationError, match="special file"):
        context_module._copy_plain_tree(
            source,
            tmp_path / "destination",
            "package resource",
            skip_package_cache_entries=True,
        )


def test_copy_plain_tree_wraps_read_permission_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copy failures become materialization errors instead of raw OS errors."""
    source = tmp_path / "source"
    source.mkdir()
    unreadable = source / "hook.sh"
    unreadable.write_text("#!/bin/sh\n", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def fail_read_bytes(self: Path) -> bytes:
        if self == unreadable:
            raise PermissionError("read denied")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    with pytest.raises(MaterializationError, match=r"file could not be read: hook\.sh"):
        context_module._copy_plain_tree(
            source,
            tmp_path / "destination",
            "runtime hooks",
        )
