"""Tests for host render context preparation and root lock artifacts."""

from __future__ import annotations

import os
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
    resolvers: FakeResolvers | None = None,
    options: LockOptions | None = None,
    overwrite: bool = False,
) -> FakeResolvers:
    """Render a context and return the fake resolvers used."""
    providers = resolvers or FakeResolvers()
    work = tmp_path if working_directory is None else working_directory
    prepare_render_context(
        write_config(work),
        output or tmp_path / "context",
        resolvers=providers.source_resolvers(),
        lock_options=options,
        overwrite=overwrite,
        working_directory=work,
    )
    return providers


def test_root_artifacts_written_after_successful_render(tmp_path: Path) -> None:
    """A successful host render writes root config and lock artifacts."""
    output = tmp_path / "context"

    render_context(tmp_path, output=output)

    config_data = tomllib.loads((output / "config.toml").read_text())
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
    dockerfile = (output / "Dockerfile").read_text(encoding="utf-8")
    assert "comfy-cli==1.5.0" in dockerfile
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
    )

    render_context(tmp_path, output=output, overwrite=True)

    assert (
        (output / "config.toml").read_bytes(),
        (output / "config.lock.toml").read_bytes(),
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


def test_old_helper_projections_are_omitted_for_m3_t2(tmp_path: Path) -> None:
    """M3-T2 renders root artifacts without v0.1 helper projections."""
    output = tmp_path / "context"

    render_context(tmp_path, output=output)

    assert (output / "config.toml").is_file()
    assert (output / "config.lock.toml").is_file()
    assert not (output / "config" / "custom-nodes.toml").exists()
    assert not (output / "config" / "files.toml").exists()


def file_contents(root: Path) -> dict[str, bytes]:
    """Return relative file content for non-mutating checks."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


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
