"""End-to-end integration coverage for ``cdh host render`` root artifacts."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
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
)
from comfyui_docker_helper.container.custom_nodes import load_custom_nodes_plan
from comfyui_docker_helper.container.download_files import load_file_download_plan
from comfyui_docker_helper.rendering import has_valid_context_marker

COMMIT_1 = "1" * 40
COMMIT_2 = "2" * 40
COMMIT_3 = "3" * 40
COMMIT_4 = "4" * 40

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

MINIMAL_CONFIG_WITHOUT_COMFYUI = """\
[compute_platform]
type = "cuda"

[compute_platform.cuda]
version = "12.9.2"

[pytorch]
version = "2.10"
"""


@dataclass(slots=True)
class RenderWorkflowComfyUIProvider:
    """Configurable ComfyUI resolver for host render workflow tests."""

    releases: tuple[ComfyUIReleaseCandidate, ...] = (
        ComfyUIReleaseCandidate(version="0.27.0", commit=COMMIT_2),
        ComfyUIReleaseCandidate(version="0.26.0", commit=COMMIT_1),
    )
    nightly_commit: str = COMMIT_3
    calls: list[str] = field(default_factory=list)

    def list_releases(self) -> Sequence[ComfyUIReleaseCandidate]:
        self.calls.append("list_releases")
        return self.releases

    def get_nightly_commit(self) -> str:
        self.calls.append("get_nightly_commit")
        return self.nightly_commit


@dataclass(slots=True)
class RenderWorkflowComfyCliProvider:
    """Configurable comfy-cli resolver for host render workflow tests."""

    versions: tuple[ComfyCliVersionCandidate, ...] = (
        ComfyCliVersionCandidate(version="1.6.0"),
        ComfyCliVersionCandidate(version="1.5.0"),
    )
    calls: list[str] = field(default_factory=list)

    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        self.calls.append("list_versions")
        return self.versions


@dataclass(slots=True)
class RenderWorkflowRegistryProvider:
    """Configurable Comfy Registry resolver for host render workflow tests."""

    versions: dict[str, tuple[RegistryVersionCandidate, ...]] = field(
        default_factory=lambda: {
            "registry-node": (
                RegistryVersionCandidate(node_id="registry-node", version="1.6.0"),
                RegistryVersionCandidate(node_id="registry-node", version="1.5.0"),
            ),
            "latest-node": (
                RegistryVersionCandidate(node_id="latest-node", version="2.0.0"),
            ),
        }
    )
    latest_versions: dict[str, str] = field(
        default_factory=lambda: {"latest-node": "2.0.0"}
    )
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def get_install_metadata(
        self,
        node_id: str,
        version: str | None = None,
    ) -> RegistryInstallMetadata:
        self.calls.append(("get_install_metadata", node_id, version))
        resolved = version or self.latest_versions[node_id]
        return RegistryInstallMetadata(node_id=node_id, version=resolved)

    def list_versions(self, node_id: str) -> Sequence[RegistryVersionCandidate]:
        self.calls.append(("list_versions", node_id, None))
        return self.versions[node_id]


@dataclass(slots=True)
class RenderWorkflowGitProvider:
    """Configurable Git resolver for host render workflow tests."""

    refs: dict[tuple[str, str], str] = field(
        default_factory=lambda: {
            ("https://example.com/ComfyUI-Git-Node.git", "main"): COMMIT_3,
            ("https://example.com/ComfyUI-Full-Node.git", "release"): COMMIT_4,
        }
    )
    default_heads: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def resolve_default_branch_head(self, url: str) -> str:
        self.calls.append(("resolve_default_branch_head", url, None))
        return self.default_heads[url]

    def resolve_ref(self, url: str, ref: str) -> str:
        self.calls.append(("resolve_ref", url, ref))
        return self.refs[(url, ref)]


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


@dataclass(frozen=True, slots=True)
class RenderFixtureCase:
    """One representative render fixture."""

    name: str
    extra_config: str
    expected_config_selectors: dict[str, object]
    expected_lock: dict[str, object]
    base_config: str = MINIMAL_CONFIG
    expect_custom_nodes: bool = False
    expect_files: bool = False


def _resolvers(
    *,
    comfyui: object | None = None,
    comfy_cli: object | None = None,
    registry: object | None = None,
    git: object | None = None,
) -> SourceResolvers:
    return SourceResolvers(
        comfyui=comfyui or RenderWorkflowComfyUIProvider(),
        comfy_cli=comfy_cli or RenderWorkflowComfyCliProvider(),
        registry=registry or RenderWorkflowRegistryProvider(),
        git=git or RenderWorkflowGitProvider(),
    )


def _failing_resolvers() -> SourceResolvers:
    return SourceResolvers(
        comfyui=FailingComfyUIProvider(),
        comfy_cli=FailingComfyCliProvider(),
        registry=FailingRegistryProvider(),
        git=FailingGitProvider(),
    )


def _write_config(root: Path, document: str) -> Path:
    path = root / "config.toml"
    path.write_text(document, encoding="utf-8")
    return path


def _parse_toml(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _install_resolvers(
    monkeypatch: pytest.MonkeyPatch,
    resolvers: SourceResolvers,
) -> None:
    monkeypatch.setattr(
        "comfyui_docker_helper.host.cli.create_default_source_resolvers",
        lambda: resolvers,
    )


def _assert_context_tree_shape(context: Path) -> None:
    assert has_valid_context_marker(context)
    assert (context / ".cdh-rendered").is_file()
    assert (context / "Dockerfile").is_file()
    assert (context / "config.toml").is_file()
    assert (context / "config.lock.toml").is_file()
    assert (context / "packages" / "cdh" / "pyproject.toml").is_file()
    assert (context / "packages" / "cdh" / "src").is_dir()
    assert not (context / "config" / "custom-nodes.toml").exists()
    assert not (context / "config" / "files.toml").exists()


def _collect_schema_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested_key
            for nested_value in value.values()
            for nested_key in _collect_schema_keys(nested_value)
        }
    if isinstance(value, list):
        return {
            nested_key
            for nested_value in value
            for nested_key in _collect_schema_keys(nested_value)
        }
    return set()


def _assert_lock_uses_resolved_source_schema(lock_data: dict[str, object]) -> None:
    """Lock artifacts persist resolved source fields, not request selectors."""
    forbidden_keys = {
        "requested_version",
        "requested_ref",
        "selector",
        "version_selector",
        "ref",
    }
    assert _collect_schema_keys(lock_data).isdisjoint(forbidden_keys)


RENDER_FIXTURE_CASES = (
    # Matrix covers selector forms while keeping all resolver inputs deterministic.
    RenderFixtureCase(
        name="minimal",
        extra_config="",
        expected_config_selectors={"comfyui_version": "latest"},
        expected_lock={"comfyui_version": "0.27.0", "comfyui_commit": COMMIT_2},
    ),
    RenderFixtureCase(
        name="registry",
        extra_config="""
[[comfyui.custom_nodes]]
type = "registry"
id = "latest-node"
version = "latest"
""",
        expected_config_selectors={"registry_version": "latest"},
        expected_lock={"registry_version": "2.0.0"},
        expect_custom_nodes=True,
    ),
    RenderFixtureCase(
        name="git",
        extra_config="""
[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/ComfyUI-Git-Node.git"
ref = "main"
""",
        expected_config_selectors={"git_ref": "main"},
        expected_lock={"git_commit": COMMIT_3},
        expect_custom_nodes=True,
    ),
    RenderFixtureCase(
        name="constraint",
        extra_config="""
[comfyui]
version = ">=0.26,<0.28"
cli_version = ">=1.5,<2"

[[comfyui.custom_nodes]]
type = "registry"
id = "registry-node"
version = ">=1.5,<2"
""",
        expected_config_selectors={
            "comfyui_version": ">=0.26,<0.28",
            "cli_version": ">=1.5,<2",
            "registry_version": ">=1.5,<2",
        },
        expected_lock={
            "comfyui_version": "0.27.0",
            "cli_version": "1.6.0",
            "registry_version": "1.6.0",
        },
        base_config=MINIMAL_CONFIG_WITHOUT_COMFYUI,
        expect_custom_nodes=True,
    ),
    RenderFixtureCase(
        name="full",
        extra_config="""
[system]
comfyui_path = "/workspace/ComfyUI"

[cdh]
default_downloader = "httpx"

[comfyui]
version = "nightly"
cli_version = ">=1.5,<2"

[[comfyui.custom_nodes]]
type = "registry"
id = "registry-node"
version = ">=1.5,<2"

[[comfyui.custom_nodes]]
type = "git"
url = "https://example.com/ComfyUI-Full-Node.git"
ref = "release"

[[files]]
url = "https://example.com/model.safetensors"
dir = "models/checkpoints"
filename = "model.safetensors"
downloader = "httpx"
""",
        expected_config_selectors={
            "comfyui_version": "nightly",
            "cli_version": ">=1.5,<2",
            "registry_version": ">=1.5,<2",
            "git_ref": "release",
        },
        expected_lock={
            "comfyui_version": None,
            "comfyui_commit": COMMIT_3,
            "cli_version": "1.6.0",
            "registry_version": "1.6.0",
            "git_commit": COMMIT_4,
        },
        base_config=MINIMAL_CONFIG_WITHOUT_COMFYUI,
        expect_custom_nodes=True,
        expect_files=True,
    ),
)


@pytest.mark.parametrize("case", RENDER_FIXTURE_CASES, ids=lambda case: case.name)
def test_host_render_representative_contexts_write_root_artifacts(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: RenderFixtureCase,
) -> None:
    """Render representative contexts through the CLI without live resolvers."""
    _install_resolvers(monkeypatch, _resolvers())
    config = _write_config(tmp_path, case.base_config + case.extra_config)
    context = tmp_path / f"context-{case.name}"

    result = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(context)],
    )

    assert result.exit_code == 0
    assert result.output == ""
    _assert_context_tree_shape(context)

    rendered_config = _parse_toml(context / "config.toml")
    rendered_lock = _parse_toml(context / "config.lock.toml")
    _assert_lock_uses_resolved_source_schema(rendered_lock)

    comfyui_config = rendered_config["comfyui"]
    comfyui_lock = rendered_lock["comfyui"]
    expected_selectors = case.expected_config_selectors
    expected_lock = case.expected_lock
    if "comfyui_version" in expected_selectors:
        assert comfyui_config["version"] == expected_selectors["comfyui_version"]
    if "cli_version" in expected_selectors:
        assert comfyui_config["cli_version"] == expected_selectors["cli_version"]
    expected_comfyui_version = expected_lock.get("comfyui_version", "0.27.0")
    assert comfyui_lock.get("version") == expected_comfyui_version
    assert comfyui_lock["commit"] == expected_lock.get("comfyui_commit", COMMIT_2)
    assert comfyui_lock["cli_version"] == expected_lock.get("cli_version", "1.6.0")

    custom_nodes_config = comfyui_config.get("custom_nodes", [])
    custom_nodes_lock = rendered_lock.get("custom_nodes", [])
    if "registry_version" in expected_selectors:
        registry_config = next(
            node for node in custom_nodes_config if node["type"] == "registry"
        )
        registry_lock = next(
            node for node in custom_nodes_lock if node["type"] == "registry"
        )
        assert registry_config["version"] == expected_selectors["registry_version"]
        assert registry_lock["version"] == expected_lock["registry_version"]
    if "git_ref" in expected_selectors:
        git_config = next(node for node in custom_nodes_config if node["type"] == "git")
        git_lock = next(node for node in custom_nodes_lock if node["type"] == "git")
        assert git_config["ref"] == expected_selectors["git_ref"]
        assert git_lock["commit"] == expected_lock["git_commit"]

    if case.expect_custom_nodes:
        custom_nodes = load_custom_nodes_plan(
            context / "config.toml",
            context / "config.lock.toml",
        )
        assert custom_nodes.items
        for item in custom_nodes.items:
            if item.type == "registry":
                assert item.version == expected_lock["registry_version"]
            if item.type == "git":
                assert item.ref == expected_lock["git_commit"]
    if case.expect_files:
        files = load_file_download_plan(
            context / "config.toml",
            context / "config.lock.toml",
            comfyui_path="/workspace/ComfyUI",
        )
        runtime_config = _parse_toml(context / "runtime" / "config.toml")
        assert len(files.items) == 1
        assert files.items[0].downloader == "httpx"
        assert runtime_config["files"][0]["url"] == files.items[0].url
        assert runtime_config["files"][0]["downloader"] == "httpx"


def _write_registry_latest_config(tmp_path: Path) -> Path:
    return _write_config(
        tmp_path,
        MINIMAL_CONFIG
        + """
[[comfyui.custom_nodes]]
type = "registry"
id = "registry-node"
version = "latest"
""",
    )


def _render_registry_context(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "1.5.0",
) -> tuple[Path, Path, str, str]:
    config = _write_registry_latest_config(tmp_path)
    context = tmp_path / "context"
    first_registry = RenderWorkflowRegistryProvider(
        latest_versions={"registry-node": version}
    )
    _install_resolvers(monkeypatch, _resolvers(registry=first_registry))

    rendered = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(context)],
    )
    assert rendered.exit_code == 0
    _assert_context_tree_shape(context)
    first_lock = (context / "config.lock.toml").read_text(encoding="utf-8")
    first_dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    assert version in first_lock
    return config, context, first_lock, first_dockerfile


def test_host_render_locked_reuses_existing_lock_without_resolution(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locked mode reuses the existing artifact lock at the CLI boundary."""
    config, context, first_lock, _ = _render_registry_context(
        cli_runner,
        tmp_path,
        monkeypatch,
    )
    _install_resolvers(monkeypatch, _failing_resolvers())

    locked = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(context),
            "--locked",
            "--overwrite",
        ],
    )
    assert locked.exit_code == 0
    assert (context / "config.lock.toml").read_text(encoding="utf-8") == first_lock


def test_host_render_check_mode_is_non_mutating_and_reports_dockerfile_drift(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check mode validates rendered artifacts without modifying caller files."""
    config, context, first_lock, first_dockerfile = _render_registry_context(
        cli_runner,
        tmp_path,
        monkeypatch,
    )
    untouched = tmp_path / "untouched.txt"
    untouched.write_text("caller-owned\n", encoding="utf-8")

    _install_resolvers(monkeypatch, _failing_resolvers())
    checked = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(context), "--check"],
    )
    assert checked.exit_code == 0
    assert untouched.read_text(encoding="utf-8") == "caller-owned\n"
    assert (context / "config.lock.toml").read_text(encoding="utf-8") == first_lock
    assert (context / "Dockerfile").read_text(encoding="utf-8") == first_dockerfile

    drifted_dockerfile = first_dockerfile + "\n# caller drift\n"
    (context / "Dockerfile").write_text(drifted_dockerfile, encoding="utf-8")
    drift_check = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(context), "--check"],
    )
    assert drift_check.exit_code == 1
    assert "render.check_changed" in drift_check.stderr
    assert "Dockerfile would be changed by render" in drift_check.stderr
    assert (context / "Dockerfile").read_text(encoding="utf-8") == drifted_dockerfile


def test_host_render_upgrade_lock_refreshes_moving_registry_selection(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrade mode refreshes moving registry selections in config.lock.toml."""
    config, context, first_lock, _ = _render_registry_context(
        cli_runner,
        tmp_path,
        monkeypatch,
    )
    upgraded_registry = RenderWorkflowRegistryProvider(
        latest_versions={"registry-node": "1.6.0"}
    )
    _install_resolvers(monkeypatch, _resolvers(registry=upgraded_registry))
    upgraded = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(config),
            "-o",
            str(context),
            "--upgrade-lock",
            "--overwrite",
        ],
    )
    assert upgraded.exit_code == 0
    upgraded_lock = (context / "config.lock.toml").read_text(encoding="utf-8")
    assert upgraded_lock != first_lock
    assert "1.6.0" in upgraded_lock


def test_host_render_dry_run_writes_no_context_artifacts(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run mode resolves successfully without creating the output context."""
    config = _write_registry_latest_config(tmp_path)
    registry = RenderWorkflowRegistryProvider(
        latest_versions={"registry-node": "1.5.0"}
    )
    _install_resolvers(monkeypatch, _resolvers(registry=registry))
    dry_run_context = tmp_path / "dry-run-context"
    before = sorted(item.name for item in tmp_path.iterdir())

    dry_run = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(dry_run_context), "--dry-run"],
    )
    after = sorted(item.name for item in tmp_path.iterdir())
    assert dry_run.exit_code == 0
    assert dry_run_context.name not in after
    assert before == after


def test_host_render_locked_reports_missing_and_incompatible_lock(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surface strict lock diagnostics without resolver calls."""
    config = _write_config(tmp_path, MINIMAL_CONFIG)
    missing_context = tmp_path / "missing-context"
    _install_resolvers(monkeypatch, _failing_resolvers())

    missing = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(missing_context), "--locked"],
    )
    assert missing.exit_code == 1
    assert "lockfile.required" in missing.stderr
    assert "--locked requires an existing config.lock.toml" in missing.stderr
    assert not missing_context.exists()

    _install_resolvers(monkeypatch, _resolvers())
    context = tmp_path / "context"
    rendered = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(config), "-o", str(context)],
    )
    assert rendered.exit_code == 0

    changed_config = _write_config(
        tmp_path,
        MINIMAL_CONFIG.replace('version = "latest"', 'version = "nightly"'),
    )
    _install_resolvers(monkeypatch, _failing_resolvers())
    incompatible = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(changed_config),
            "-o",
            str(context),
            "--locked",
        ],
    )
    assert incompatible.exit_code == 1
    assert "lockfile.digest_mismatch" in incompatible.stderr
    assert "config.lock.toml was created for different lock inputs" in (
        incompatible.stderr
    )

    exact_config = _write_config(
        tmp_path,
        MINIMAL_CONFIG.replace('version = "latest"', 'version = "0.27.0"'),
    )
    exact_context = tmp_path / "exact-context"
    _install_resolvers(monkeypatch, _resolvers())
    exact_rendered = cli_runner.invoke(
        app,
        ["host", "render", "-f", str(exact_config), "-o", str(exact_context)],
    )
    assert exact_rendered.exit_code == 0
    exact_lock_path = exact_context / "config.lock.toml"
    exact_lock = exact_lock_path.read_text(encoding="utf-8")
    assert 'version = "0.27.0"' in exact_lock
    exact_lock_path.write_text(
        exact_lock.replace('version = "0.27.0"', 'version = "0.26.0"', 1),
        encoding="utf-8",
    )

    _install_resolvers(monkeypatch, _failing_resolvers())
    tampered_lock = cli_runner.invoke(
        app,
        [
            "host",
            "render",
            "-f",
            str(exact_config),
            "-o",
            str(exact_context),
            "--locked",
        ],
    )
    assert tampered_lock.exit_code == 1
    assert "lockfile.comfyui_incompatible" in tampered_lock.stderr
    assert "locked ComfyUI source is missing or does not satisfy selector '0.27.0'" in (
        tampered_lock.stderr
    )
    assert "lockfile.digest_mismatch" not in tampered_lock.stderr
