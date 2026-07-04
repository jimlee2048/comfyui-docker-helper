"""Tests for host render context preparation and root lock artifacts."""

from __future__ import annotations

import os
import shutil
import tomllib
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

import pytest

from comfyui_docker_helper.config import (
    COMFYUI_REPO_URL,
    ComfyCliVersionCandidate,
    ComfyUIReleaseCandidate,
    LockOptions,
    RegistryInstallMetadata,
    RegistryVersionCandidate,
    SourceResolvers,
    parse_lockfile_toml,
)
from comfyui_docker_helper.host.render_service import (
    HostRenderServiceError,
    prepare_render_context,
)

COMMIT_1 = "1" * 40
COMMIT_2 = "2" * 40
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
GIT_URL = "https://example.com/custom-node.git"

CONFIG = f"""\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"

[comfyui]
version = "latest"
cli_version = "latest"

[[comfyui.custom_nodes]]
type = "registry"
id = "node"

[[comfyui.custom_nodes]]
type = "git"
url = "{GIT_URL}"
ref = "main"

[[files]]
url = "https://example.com/model.safetensors"
dir = "models/checkpoints"
filename = "model.safetensors"
"""

HOOK_CONFIG = CONFIG.replace(
    'id = "node"\n',
    'id = "node"\npre_install_scripts = ["pre.sh"]\n',
)


@dataclass(slots=True)
class FakeComfyUIProvider:
    """In-memory ComfyUI provider with controllable latest selection."""

    version: str = "0.26.0"
    commit: str = COMMIT_1
    release_calls: int = 0
    nightly_calls: int = 0

    def list_releases(self) -> Sequence[ComfyUIReleaseCandidate]:
        self.release_calls += 1
        return [ComfyUIReleaseCandidate(version=self.version, commit=self.commit)]

    def get_nightly_commit(self) -> str:
        self.nightly_calls += 1
        return COMMIT_2


@dataclass(slots=True)
class FakeComfyCliProvider:
    """In-memory comfy-cli provider with controllable latest selection."""

    version: str = "1.5.0"
    calls: int = 0

    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        self.calls += 1
        return [ComfyCliVersionCandidate(version=self.version)]


@dataclass(slots=True)
class FakeRegistryProvider:
    """In-memory registry provider with controllable latest selection."""

    version: str = "1.0.0"
    install_calls: list[tuple[str, str | None]] = field(default_factory=list)
    version_calls: list[str] = field(default_factory=list)

    def get_install_metadata(
        self,
        node_id: str,
        version: str | None = None,
    ) -> RegistryInstallMetadata:
        self.install_calls.append((node_id, version))
        return RegistryInstallMetadata(
            node_id=node_id,
            version=self.version if version is None else version,
        )

    def list_versions(self, node_id: str) -> Sequence[RegistryVersionCandidate]:
        self.version_calls.append(node_id)
        return [RegistryVersionCandidate(node_id=node_id, version=self.version)]


@dataclass(slots=True)
class FakeGitProvider:
    """In-memory Git provider with controllable ref selection."""

    commit: str = COMMIT_A
    default_calls: list[str] = field(default_factory=list)
    ref_calls: list[tuple[str, str]] = field(default_factory=list)

    def resolve_default_branch_head(self, url: str) -> str:
        self.default_calls.append(url)
        return self.commit

    def resolve_ref(self, url: str, ref: str) -> str:
        self.ref_calls.append((url, ref))
        return self.commit


@dataclass(slots=True)
class FakeResolvers:
    """Aggregate fake providers for host render service tests."""

    comfyui: FakeComfyUIProvider = field(default_factory=FakeComfyUIProvider)
    comfy_cli: FakeComfyCliProvider = field(default_factory=FakeComfyCliProvider)
    registry: FakeRegistryProvider = field(default_factory=FakeRegistryProvider)
    git: FakeGitProvider = field(default_factory=FakeGitProvider)

    def source_resolvers(self) -> SourceResolvers:
        return SourceResolvers(
            comfyui=self.comfyui,
            comfy_cli=self.comfy_cli,
            registry=self.registry,
            git=self.git,
        )

    def assert_zero_calls(self) -> None:
        assert self.comfyui.release_calls == 0
        assert self.comfyui.nightly_calls == 0
        assert self.comfy_cli.calls == 0
        assert self.registry.install_calls == []
        assert self.registry.version_calls == []
        assert self.git.default_calls == []
        assert self.git.ref_calls == []


def write_config(tmp_path: Path, content: str = CONFIG) -> Path:
    """Write a render config for service tests."""
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def render_context(
    tmp_path: Path,
    *,
    output: Path | None = None,
    working_directory: Path | None = None,
    config_content: str = CONFIG,
    scripts_dir: Path | None = None,
    hooks_dir: Path | None = None,
    resolvers: FakeResolvers | None = None,
    options: LockOptions | None = None,
    overwrite: bool = False,
) -> FakeResolvers:
    """Render a context and return the fake resolvers used."""
    providers = resolvers or FakeResolvers()
    work = tmp_path if working_directory is None else working_directory
    prepare_render_context(
        write_config(work, config_content),
        output or tmp_path / "context",
        scripts_dir=scripts_dir or work / "scripts",
        hooks_dir=hooks_dir,
        resolvers=providers.source_resolvers(),
        lock_options=options,
        overwrite=overwrite,
        working_directory=work,
    )
    return providers


def test_root_artifacts_written_after_successful_render(tmp_path: Path) -> None:
    """A successful host render writes root, runtime, and lock artifacts."""
    output = tmp_path / "context"

    render_context(tmp_path, output=output)

    config_data = tomllib.loads((output / "config.toml").read_text())
    runtime_data = tomllib.loads((output / "runtime" / "config.toml").read_text())
    lockfile = parse_lockfile_toml((output / "config.lock.toml").read_text())
    assert config_data["comfyui"]["version"] == "latest"
    assert config_data["comfyui"]["custom_nodes"][0] == {
        "type": "registry",
        "id": "node",
        "pre_install_scripts": [],
        "post_install_scripts": [],
    }
    assert lockfile.comfyui.repo == COMFYUI_REPO_URL
    assert lockfile.comfyui.version == "0.26.0"
    assert lockfile.comfyui.commit == COMMIT_1
    assert lockfile.custom_nodes[0].version == "1.0.0"
    assert lockfile.custom_nodes[1].commit == COMMIT_A
    assert runtime_data["comfyui"] == {
        "listen": "0.0.0.0",
        "port": 8188,
        "extra_args": [],
    }
    assert runtime_data["cdh"]["default_downloader"] == "aria2"
    assert runtime_data["cdh"]["default_download_mode"] == "sync"
    assert "downloader" in runtime_data["cdh"]
    assert runtime_data["files"] == [
        {
            "url": "https://example.com/model.safetensors",
            "dir": "models/checkpoints",
            "filename": "model.safetensors",
            "overwrite": False,
        }
    ]
    assert "custom_nodes" not in runtime_data["comfyui"]
    assert not (output / "runtime" / "hooks").exists()
    dockerfile = (output / "Dockerfile").read_text(encoding="utf-8")
    assert "comfy-cli==1.5.0" in dockerfile
    assert "COPY runtime/hooks /opt/cdh/runtime/hooks" not in dockerfile
    assert "      --version \\\n      0.26.0 \\" in dockerfile
    expected_verify = (
        'RUN comfyui_commit="$(git -C "$COMFYUI_PATH" rev-parse HEAD)" && '
        f'test "$comfyui_commit" = {COMMIT_1}'
    )
    assert expected_verify in dockerfile


def test_root_artifacts_omitted_during_dry_run(tmp_path: Path) -> None:
    """Dry-run resolves the lock preview but writes no context artifacts."""
    output = tmp_path / "context"

    render_context(
        tmp_path,
        output=output,
        options=LockOptions(dry_run=True),
    )

    assert not output.exists()


def test_root_artifacts_are_deterministic_across_repeated_renders(
    tmp_path: Path,
) -> None:
    """Repeated renders with reused compatible locks keep root artifact bytes stable."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    first = (
        (output / "config.toml").read_bytes(),
        (output / "config.lock.toml").read_bytes(),
        (output / "runtime" / "config.toml").read_bytes(),
    )

    render_context(tmp_path, output=output, overwrite=True)

    assert (
        (output / "config.toml").read_bytes(),
        (output / "config.lock.toml").read_bytes(),
        (output / "runtime" / "config.toml").read_bytes(),
    ) == first


def test_default_render_reuses_existing_lock_without_provider_calls(
    tmp_path: Path,
) -> None:
    """Default mode reuses compatible context lock entries."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    providers = FakeResolvers()

    render_context(tmp_path, output=output, resolvers=providers, overwrite=True)

    providers.assert_zero_calls()


def test_locked_mode_requires_existing_lock_and_avoids_provider_calls(
    tmp_path: Path,
) -> None:
    """Locked mode consumes the existing context lock without resolver calls."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    providers = FakeResolvers()

    render_context(
        tmp_path,
        output=output,
        resolvers=providers,
        options=LockOptions(locked=True),
        overwrite=True,
    )

    providers.assert_zero_calls()


def test_locked_mode_fails_when_lock_is_missing(tmp_path: Path) -> None:
    """Locked mode fails closed instead of resolving a missing lock."""
    with pytest_raises_host_error("lockfile.required"):
        render_context(tmp_path, options=LockOptions(locked=True))


def test_locked_mode_reads_relative_output_lock_from_working_directory(
    tmp_path: Path,
) -> None:
    """Relative output lookup uses working_directory, matching context writes."""
    work = tmp_path / "work"
    work.mkdir()
    render_context(tmp_path, output=Path("context"), working_directory=work)
    providers = FakeResolvers()

    render_context(
        tmp_path,
        output=Path("context"),
        working_directory=work,
        resolvers=providers,
        options=LockOptions(locked=True),
        overwrite=True,
    )

    providers.assert_zero_calls()
    assert (work / "context" / "config.lock.toml").is_file()


# Check-mode tests compare the managed artifact set without mutating the
# rendered context, including stale files under current managed trees.
def test_check_mode_is_non_mutating_and_detects_current_root_artifacts(
    tmp_path: Path,
) -> None:
    """Check mode validates root artifacts without rewriting the context."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    before = file_contents(output)

    render_context(tmp_path, output=output, options=LockOptions(check=True))

    assert file_contents(output) == before


def test_check_mode_reads_relative_output_artifacts_from_working_directory(
    tmp_path: Path,
) -> None:
    """Relative check lookup uses working_directory, not the process cwd."""
    work = tmp_path / "work"
    work.mkdir()
    render_context(tmp_path, output=Path("context"), working_directory=work)
    before = file_contents(work / "context")

    render_context(
        tmp_path,
        output=Path("context"),
        working_directory=work,
        options=LockOptions(check=True),
    )

    assert file_contents(work / "context") == before


def test_check_mode_reports_root_artifact_drift(tmp_path: Path) -> None:
    """Check mode fails when a root artifact differs from the expected render."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    (output / "config.toml").write_text("changed = true\n", encoding="utf-8")

    with pytest_raises_host_error("render.check_changed"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_check_mode_reports_runtime_config_drift(tmp_path: Path) -> None:
    """Check mode compares the managed baked runtime config."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    (output / "runtime" / "config.toml").write_text(
        '[comfyui]\nlisten = "127.0.0.1"\n',
        encoding="utf-8",
    )

    with pytest_raises_host_error("render.check_changed"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_check_mode_reports_dockerfile_drift(tmp_path: Path) -> None:
    """Check mode compares the generated Dockerfile, not only root TOML."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    (output / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    with pytest_raises_host_error("render.check_changed"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_check_mode_reports_package_projection_drift(tmp_path: Path) -> None:
    """Check mode compares managed package projection files."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    package_pyproject = output / "packages" / "cdh" / "pyproject.toml"
    package_pyproject.write_text('[project]\nname = "changed"\n', encoding="utf-8")

    with pytest_raises_host_error("render.check_changed"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_check_mode_reports_stale_file_under_package_source_tree(
    tmp_path: Path,
) -> None:
    """Check mode catches actual-only files inside managed package trees."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    stale = output / "packages" / "cdh" / "src" / "stale.py"
    stale.write_text("stale\n", encoding="utf-8")

    with pytest_raises_host_error("render.check_changed"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_check_mode_reports_symlink_package_projection_file(
    tmp_path: Path,
) -> None:
    """Check mode treats symlinked managed package files as drift."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    package_pyproject = output / "packages" / "cdh" / "pyproject.toml"
    real_pyproject = tmp_path / "real-package-pyproject.toml"
    real_pyproject.write_bytes(package_pyproject.read_bytes())
    package_pyproject.unlink()
    package_pyproject.symlink_to(real_pyproject)

    with pytest_raises_host_error("render.check_changed"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_check_mode_reports_symlink_package_projection_dir(tmp_path: Path) -> None:
    """Check mode treats symlinked managed package directories as drift."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    package_src = output / "packages" / "cdh" / "src"
    real_src = tmp_path / "real-package-src"
    package_src.rename(real_src)
    package_src.symlink_to(real_src, target_is_directory=True)

    with pytest_raises_host_error("render.check_changed"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_check_mode_reports_scripts_drift_when_hooks_are_present(
    tmp_path: Path,
) -> None:
    """Check mode compares copied scripts when the render plan has hooks."""
    output = tmp_path / "context"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    render_context(
        tmp_path,
        output=output,
        config_content=HOOK_CONFIG,
        scripts_dir=scripts,
    )
    (output / "scripts" / "pre.sh").write_text("changed\n", encoding="utf-8")

    with pytest_raises_host_error("render.check_changed"):
        render_context(
            tmp_path,
            output=output,
            config_content=HOOK_CONFIG,
            scripts_dir=scripts,
            options=LockOptions(check=True),
        )


def test_check_mode_reports_stale_file_under_scripts_when_hooks_are_present(
    tmp_path: Path,
) -> None:
    """Check mode catches actual-only files inside the managed scripts tree."""
    output = tmp_path / "context"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    render_context(
        tmp_path,
        output=output,
        config_content=HOOK_CONFIG,
        scripts_dir=scripts,
    )
    stale = output / "scripts" / "stale.sh"
    stale.write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest_raises_host_error("render.check_changed"):
        render_context(
            tmp_path,
            output=output,
            config_content=HOOK_CONFIG,
            scripts_dir=scripts,
            options=LockOptions(check=True),
        )


def test_check_mode_reports_stale_scripts_tree_when_hooks_are_removed(
    tmp_path: Path,
) -> None:
    """Check mode catches a previously managed scripts tree after hooks are removed."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    stale_scripts = output / "scripts"
    stale_scripts.mkdir()
    (stale_scripts / "pre.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(HostRenderServiceError) as error:
        render_context(
            tmp_path,
            output=output,
            options=LockOptions(check=True),
        )
    assert [
        diagnostic.path
        for diagnostic in error.value.diagnostics
        if diagnostic.code == "render.check_changed"
    ] == [("scripts",)]


# Runtime hook render/check coverage protects source validation, copied hook
# artifacts, and drift detection when hooks are added, removed, or changed.
def test_omitted_runtime_hooks_dir_is_copied_when_default_exists(
    tmp_path: Path,
) -> None:
    """An omitted --hooks-dir activates ./hooks when the tree exists."""
    output = tmp_path / "context"
    hooks = write_runtime_hook_tree(tmp_path / "hooks")

    render_context(tmp_path, output=output)

    assert (output / "runtime" / "hooks" / "pre-start.d" / "10-pre.sh").read_text(
        encoding="utf-8"
    ) == "#!/bin/sh\n"
    assert (output / "runtime" / "hooks" / "post-start.d" / "20-post.py").read_text(
        encoding="utf-8"
    ) == "print('post')\n"
    assert (output / "runtime" / "hooks" / "stop.d" / "30-stop.sh").is_file()
    dockerfile = (output / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY runtime/hooks /opt/cdh/runtime/hooks" in dockerfile
    assert hooks.resolve() != (output / "runtime" / "hooks").resolve()


def test_explicit_runtime_hooks_dir_must_exist(tmp_path: Path) -> None:
    """An explicit --hooks-dir fails when the source path is missing."""
    missing = tmp_path / "missing-hooks"

    with pytest.raises(HostRenderServiceError) as error:
        render_context(tmp_path, hooks_dir=missing)

    assert locations_and_codes(error.value) == [
        (("hooks_dir",), "runtime_hooks.source_not_directory")
    ]


def test_runtime_hooks_reject_unknown_top_level_entries(tmp_path: Path) -> None:
    """Active runtime hook sources are a closed top-level phase directory set."""
    hooks = tmp_path / "runtime-hooks"
    hooks.mkdir()
    (hooks / "README.md").write_text("not a phase\n", encoding="utf-8")

    with pytest.raises(HostRenderServiceError) as error:
        render_context(tmp_path, hooks_dir=hooks)

    assert locations_and_codes(error.value) == [
        (("hooks_dir", "README.md"), "runtime_hooks.unknown_top_level")
    ]


def test_runtime_hooks_reject_phase_name_as_file(tmp_path: Path) -> None:
    """Known runtime hook phase entries must be directories."""
    hooks = tmp_path / "runtime-hooks"
    hooks.mkdir()
    (hooks / "pre-start.d").write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(HostRenderServiceError) as error:
        render_context(tmp_path, hooks_dir=hooks)

    assert locations_and_codes(error.value) == [
        (("hooks_dir", "pre-start.d"), "runtime_hooks.phase_not_directory")
    ]


def test_runtime_hooks_reject_symlinks(tmp_path: Path) -> None:
    """Runtime hook source trees must not contain symlinks."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    hooks = tmp_path / "runtime-hooks"
    hooks.mkdir()
    real_phase = tmp_path / "real-pre-start"
    real_phase.mkdir()
    (hooks / "pre-start.d").symlink_to(real_phase, target_is_directory=True)

    with pytest.raises(HostRenderServiceError) as error:
        render_context(tmp_path, hooks_dir=hooks)

    assert locations_and_codes(error.value) == [
        (("hooks_dir", "pre-start.d"), "runtime_hooks.symlink")
    ]


def test_runtime_hooks_reject_special_files(tmp_path: Path) -> None:
    """Runtime hook phase entries must be regular files."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("fifo special files are not supported on this platform")
    hooks = tmp_path / "runtime-hooks"
    phase = hooks / "pre-start.d"
    phase.mkdir(parents=True)
    os.mkfifo(phase / "pipe.sh")

    with pytest.raises(HostRenderServiceError) as error:
        render_context(tmp_path, hooks_dir=hooks)

    assert locations_and_codes(error.value) == [
        (("hooks_dir", "pre-start.d", "pipe.sh"), "runtime_hooks.special_file")
    ]


def test_runtime_hooks_reject_phase_subdirectories(tmp_path: Path) -> None:
    """Runtime hook phase entries must not contain nested directories."""
    hooks = tmp_path / "runtime-hooks"
    nested = hooks / "pre-start.d" / "nested"
    nested.mkdir(parents=True)

    with pytest.raises(HostRenderServiceError) as error:
        render_context(tmp_path, hooks_dir=hooks)

    assert locations_and_codes(error.value) == [
        (("hooks_dir", "pre-start.d", "nested"), "runtime_hooks.entry_not_file")
    ]


def test_runtime_hooks_reject_unsupported_extensions(tmp_path: Path) -> None:
    """Runtime hook phase files must be shell or Python scripts."""
    hooks = tmp_path / "runtime-hooks"
    phase = hooks / "pre-start.d"
    phase.mkdir(parents=True)
    (phase / "note.txt").write_text("nope\n", encoding="utf-8")

    with pytest.raises(HostRenderServiceError) as error:
        render_context(tmp_path, hooks_dir=hooks)

    assert locations_and_codes(error.value) == [
        (
            ("hooks_dir", "pre-start.d", "note.txt"),
            "runtime_hooks.unsupported_extension",
        )
    ]


def test_runtime_hooks_reject_output_nested_inside_source(tmp_path: Path) -> None:
    """Render output must not be nested inside the runtime hook source."""
    hooks = write_runtime_hook_tree(tmp_path / "runtime-hooks")

    with pytest.raises(HostRenderServiceError) as error:
        render_context(tmp_path, output=hooks / "context", hooks_dir=hooks)

    assert locations_and_codes(error.value) == [
        (("render",), "render.context_write_failed")
    ]
    assert "runtime hooks source" in error.value.diagnostics[0].message


def test_check_mode_reports_stale_runtime_hooks_tree_when_hooks_are_removed(
    tmp_path: Path,
) -> None:
    """Check mode catches a previously managed runtime hook tree after removal."""
    output = tmp_path / "context"
    hooks = write_runtime_hook_tree(tmp_path / "hooks")
    render_context(tmp_path, output=output)
    shutil.rmtree(hooks)

    with pytest.raises(HostRenderServiceError) as error:
        render_context(tmp_path, output=output, options=LockOptions(check=True))

    changed_paths = [
        diagnostic.path
        for diagnostic in error.value.diagnostics
        if diagnostic.code == "render.check_changed"
    ]
    assert ("runtime", "hooks") in changed_paths


def test_check_mode_rejects_runtime_hooks_source_inside_output(
    tmp_path: Path,
) -> None:
    """Check mode must not derive expected hooks from the rendered output."""
    output = tmp_path / "context"
    hooks = write_runtime_hook_tree(tmp_path / "hooks")
    render_context(tmp_path, output=output, hooks_dir=hooks)

    with pytest.raises(HostRenderServiceError) as error:
        render_context(
            tmp_path,
            output=output,
            hooks_dir=output / "runtime" / "hooks",
            options=LockOptions(check=True),
        )

    assert locations_and_codes(error.value) == [
        (("render",), "render.context_write_failed")
    ]
    assert "ancestor of runtime hooks source" in error.value.diagnostics[0].message


def test_dry_run_rejects_output_nested_inside_runtime_hooks_source(
    tmp_path: Path,
) -> None:
    """Early-return render modes still enforce hook source ancestry safety."""
    hooks = write_runtime_hook_tree(tmp_path / "hooks")

    with pytest.raises(HostRenderServiceError) as error:
        render_context(
            tmp_path,
            output=hooks / "pre-start.d" / "context",
            hooks_dir=hooks,
            options=LockOptions(dry_run=True),
        )

    assert locations_and_codes(error.value) == [
        (("render",), "render.context_write_failed")
    ]
    assert (
        "nested inside the runtime hooks source" in error.value.diagnostics[0].message
    )


def test_check_mode_reports_symlink_script_when_hooks_are_present(
    tmp_path: Path,
) -> None:
    """Check mode treats symlinked managed hook scripts as drift."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    output = tmp_path / "context"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    render_context(
        tmp_path,
        output=output,
        config_content=HOOK_CONFIG,
        scripts_dir=scripts,
    )
    real_script = tmp_path / "real-pre.sh"
    real_script.write_bytes((output / "scripts" / "pre.sh").read_bytes())
    (output / "scripts" / "pre.sh").unlink()
    (output / "scripts" / "pre.sh").symlink_to(real_script)

    with pytest_raises_host_error("render.check_changed"):
        render_context(
            tmp_path,
            output=output,
            config_content=HOOK_CONFIG,
            scripts_dir=scripts,
            options=LockOptions(check=True),
        )


def test_check_mode_reports_missing_scripts_when_hooks_are_present(
    tmp_path: Path,
) -> None:
    """Check mode fails when a managed scripts tree is missing."""
    output = tmp_path / "context"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pre.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    render_context(
        tmp_path,
        output=output,
        config_content=HOOK_CONFIG,
        scripts_dir=scripts,
    )
    (output / "scripts" / "pre.sh").unlink()
    (output / "scripts").rmdir()

    with pytest_raises_host_error("render.check_changed"):
        render_context(
            tmp_path,
            output=output,
            config_content=HOOK_CONFIG,
            scripts_dir=scripts,
            options=LockOptions(check=True),
        )


def test_check_mode_ignores_unmanaged_extras_and_keeps_target_unchanged(
    tmp_path: Path,
) -> None:
    """Check mode neither reports nor deletes files outside managed artifacts."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    extra = output / "unmanaged" / "extra.txt"
    extra.parent.mkdir()
    extra.write_text("keep me\n", encoding="utf-8")
    before = file_contents(output)

    render_context(tmp_path, output=output, options=LockOptions(check=True))

    assert file_contents(output) == before
    assert extra.read_text(encoding="utf-8") == "keep me\n"


def test_check_mode_cleans_temporary_expected_contexts(tmp_path: Path) -> None:
    """Check mode removes temporary expected contexts after success and failure."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)

    render_context(tmp_path, output=output, options=LockOptions(check=True))
    assert check_temp_dirs(tmp_path) == []

    (output / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    with pytest_raises_host_error("render.check_changed"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))
    assert check_temp_dirs(tmp_path) == []


def test_check_locked_mode_reuses_compatible_lock_without_provider_calls(
    tmp_path: Path,
) -> None:
    """Check plus locked mode keeps the strict no-resolution lock behavior."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    providers = FakeResolvers()

    render_context(
        tmp_path,
        output=output,
        resolvers=providers,
        options=LockOptions(check=True, locked=True),
    )

    providers.assert_zero_calls()


def test_check_mode_rejects_missing_context(tmp_path: Path) -> None:
    """Check mode fails before accepting absent output paths."""
    with pytest_raises_host_error("render.context_missing"):
        render_context(tmp_path, options=LockOptions(check=True))


def test_check_mode_rejects_unmarked_context(tmp_path: Path) -> None:
    """Check mode requires the existing rendered-context marker."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    (output / ".cdh-rendered").unlink()

    with pytest_raises_host_error("render.context_unmarked"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_check_mode_rejects_symlink_output_dir(tmp_path: Path) -> None:
    """Check mode refuses to follow a symlinked output directory."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    output_link = tmp_path / "context-link"
    output_link.symlink_to(output, target_is_directory=True)

    with pytest_raises_host_error("render.output_symlink"):
        render_context(tmp_path, output=output_link, options=LockOptions(check=True))


def test_check_mode_rejects_symlink_root_config_artifact(tmp_path: Path) -> None:
    """Check mode treats symlinked root config.toml as unsafe drift."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    real_config = tmp_path / "real-config.toml"
    real_config.write_bytes((output / "config.toml").read_bytes())
    (output / "config.toml").unlink()
    (output / "config.toml").symlink_to(real_config)

    with pytest_raises_host_error("render.check_changed"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_check_mode_rejects_symlink_root_lock_artifact(tmp_path: Path) -> None:
    """Check mode refuses symlinked config.lock.toml before lock consumption."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    real_lock = tmp_path / "real-config.lock.toml"
    real_lock.write_bytes((output / "config.lock.toml").read_bytes())
    (output / "config.lock.toml").unlink()
    (output / "config.lock.toml").symlink_to(real_lock)

    with pytest_raises_host_error("lockfile.invalid_path"):
        render_context(tmp_path, output=output, options=LockOptions(check=True))


def test_upgrade_lock_refreshes_moving_source_selections(tmp_path: Path) -> None:
    """Upgrade mode intentionally re-resolves moving selectors."""
    output = tmp_path / "context"
    render_context(tmp_path, output=output)
    providers = FakeResolvers(
        comfyui=FakeComfyUIProvider(version="0.27.0", commit=COMMIT_2),
        comfy_cli=FakeComfyCliProvider(version="2.0.0"),
        registry=FakeRegistryProvider(version="2.0.0"),
        git=FakeGitProvider(commit=COMMIT_B),
    )

    render_context(
        tmp_path,
        output=output,
        resolvers=providers,
        options=LockOptions(upgrade_lock=True),
        overwrite=True,
    )

    lockfile = parse_lockfile_toml((output / "config.lock.toml").read_text())
    assert lockfile.comfyui.version == "0.27.0"
    assert lockfile.comfyui.cli_version == "2.0.0"
    assert lockfile.custom_nodes[0].version == "2.0.0"
    assert lockfile.custom_nodes[1].commit == COMMIT_B


def file_contents(root: Path) -> dict[str, bytes]:
    """Return relative file content for non-mutating checks."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def check_temp_dirs(parent: Path) -> list[Path]:
    """Return leaked render-check temporary context directories."""
    return sorted(parent.glob(".cdh-check-*"))


def write_runtime_hook_tree(root: Path) -> Path:
    """Write a valid runtime hook source tree and return its root."""
    (root / "pre-start.d").mkdir(parents=True)
    (root / "post-start.d").mkdir()
    (root / "stop.d").mkdir()
    (root / "pre-start.d" / "10-pre.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "post-start.d" / "20-post.py").write_text(
        "print('post')\n",
        encoding="utf-8",
    )
    (root / "stop.d" / "30-stop.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def locations_and_codes(
    error: HostRenderServiceError,
) -> list[tuple[tuple[object, ...], str]]:
    """Return diagnostic paths and codes for concise assertions."""
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


class pytest_raises_host_error(AbstractContextManager[None]):
    """Assert that a host render service error includes a diagnostic code."""

    def __init__(self, code: str) -> None:
        self.code = code

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del traceback
        assert exc_type is HostRenderServiceError
        assert isinstance(exc, HostRenderServiceError)
        assert any(diagnostic.code == self.code for diagnostic in exc.diagnostics)
        return True
