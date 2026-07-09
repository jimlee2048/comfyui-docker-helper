"""Tests for service-level lock orchestration modes."""

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from comfyui_docker_helper.config import (
    COMFYUI_REPO_URL,
    ComfyCliVersionCandidate,
    ComfyUIReleaseCandidate,
    Config,
    DiagnosticSeverity,
    GitLockedCustomNode,
    LockedComfyUI,
    Lockfile,
    LockManifest,
    LockOptions,
    LockServiceError,
    RegistryInstallMetadata,
    RegistryLockedCustomNode,
    RegistryVersionCandidate,
    SourceResolvers,
    compute_git_custom_nodes_input_digest,
    compute_lock_input_digest,
    dump_lockfile_toml,
    resolve_lockfile,
)

COMMIT_1 = "1" * 40
COMMIT_2 = "2" * 40
COMMIT_3 = "3" * 40
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
GIT_URL = "https://example.com/custom-node.git"


@dataclass(slots=True)
class FakeComfyUIProvider:
    """In-memory ComfyUI provider that records calls."""

    releases: Sequence[ComfyUIReleaseCandidate] = field(
        default_factory=lambda: [
            ComfyUIReleaseCandidate(version="0.25.0", commit=COMMIT_1),
            ComfyUIReleaseCandidate(version="0.26.0", commit=COMMIT_2),
        ]
    )
    nightly_commit: str = COMMIT_3
    release_calls: int = 0
    nightly_calls: int = 0

    def list_releases(self) -> Sequence[ComfyUIReleaseCandidate]:
        self.release_calls += 1
        return self.releases

    def get_nightly_commit(self) -> str:
        self.nightly_calls += 1
        return self.nightly_commit


@dataclass(slots=True)
class FakeComfyCliProvider:
    """In-memory comfy-cli provider that records calls."""

    versions: Sequence[ComfyCliVersionCandidate] = field(
        default_factory=lambda: [
            ComfyCliVersionCandidate(version="1.5.0"),
            ComfyCliVersionCandidate(version="2.0.0"),
        ]
    )
    calls: int = 0

    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        self.calls += 1
        return self.versions


@dataclass(slots=True)
class FakeRegistryProvider:
    """In-memory registry provider that records calls."""

    latest_version: str = "1.5.0"
    exact_versions: dict[str, str] = field(default_factory=dict)
    deprecated: bool = False
    install_calls: list[tuple[str, str | None]] = field(default_factory=list)
    version_calls: list[str] = field(default_factory=list)

    def get_install_metadata(
        self,
        node_id: str,
        version: str | None = None,
    ) -> RegistryInstallMetadata:
        self.install_calls.append((node_id, version))
        resolved_version = (
            self.latest_version
            if version is None
            else self.exact_versions.get(version, version)
        )
        return RegistryInstallMetadata(
            node_id=node_id,
            version=resolved_version,
            deprecated=self.deprecated,
        )

    def list_versions(self, node_id: str) -> Sequence[RegistryVersionCandidate]:
        self.version_calls.append(node_id)
        return [
            RegistryVersionCandidate(node_id=node_id, version="1.4.0"),
            RegistryVersionCandidate(node_id=node_id, version="1.5.0"),
        ]


@dataclass(slots=True)
class FakeGitProvider:
    """In-memory Git provider that records calls."""

    default_commit: str = COMMIT_A
    refs: dict[str, str] = field(default_factory=lambda: {"main": COMMIT_A})
    default_calls: list[str] = field(default_factory=list)
    ref_calls: list[tuple[str, str]] = field(default_factory=list)

    def resolve_default_branch_head(self, url: str) -> str:
        self.default_calls.append(url)
        return self.default_commit

    def resolve_ref(self, url: str, ref: str) -> str:
        self.ref_calls.append((url, ref))
        return self.refs[ref]


@dataclass(slots=True)
class FakeProviders:
    """Provider aggregate plus individual fakes for assertions."""

    comfyui: FakeComfyUIProvider = field(default_factory=FakeComfyUIProvider)
    comfy_cli: FakeComfyCliProvider = field(default_factory=FakeComfyCliProvider)
    registry: FakeRegistryProvider = field(default_factory=FakeRegistryProvider)
    git: FakeGitProvider = field(default_factory=FakeGitProvider)

    def resolvers(self) -> SourceResolvers:
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


def make_config(
    *,
    comfyui_version: str = "latest",
    cli_version: str = "latest",
    custom_nodes: list[dict] | None = None,
) -> Config:
    """Return a fresh minimal config for lock service tests."""
    return Config.model_validate(
        {
            "compute_platform": {
                "type": "cuda",
                "cuda": {"version": "12.9.2"},
            },
            "pytorch": {"version": "2.10"},
            "comfyui": {
                "version": comfyui_version,
                "cli_version": cli_version,
                "custom_nodes": custom_nodes or [],
            },
        }
    )


def make_lockfile(config: Config, *, digest: str | None = None) -> Lockfile:
    """Return a compatible lockfile whose manifest digests match ``config``."""
    return Lockfile(
        schema_version=1,
        manifest=LockManifest(
            lock_input_digest=digest or compute_lock_input_digest(config),
            git_custom_nodes_input_digest=compute_git_custom_nodes_input_digest(config),
        ),
        comfyui=LockedComfyUI(
            repo=COMFYUI_REPO_URL,
            version="0.26.0",
            commit=COMMIT_2,
            cli_version="1.5.0",
        ),
        custom_nodes=[
            RegistryLockedCustomNode(
                type="registry",
                id="node",
                version="1.5.0",
            ),
            GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_A),
        ],
    )


def full_config() -> Config:
    """Return a config spanning ComfyUI, comfy-cli, registry, and Git locks."""
    return make_config(
        custom_nodes=[
            {"type": "registry", "id": "node"},
            {"type": "git", "url": GIT_URL, "ref": "main"},
        ]
    )


def test_default_resolves_full_lockfile_without_existing_lock() -> None:
    """Default mode resolves every required lock-domain entry when no lock exists."""
    config = full_config()
    providers = FakeProviders()

    result = resolve_lockfile(config, None, providers.resolvers())

    assert result.changed is True
    assert result.lockfile.manifest.lock_input_digest == compute_lock_input_digest(
        config
    )
    assert result.lockfile.comfyui == LockedComfyUI(
        repo=COMFYUI_REPO_URL,
        version="0.26.0",
        commit=COMMIT_2,
        cli_version="2.0.0",
    )
    assert result.lockfile.custom_nodes == [
        RegistryLockedCustomNode(type="registry", id="node", version="1.5.0"),
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_A),
    ]
    assert providers.comfyui.release_calls == 1
    assert providers.comfy_cli.calls == 1
    assert providers.registry.install_calls == [("node", None)]
    assert providers.git.ref_calls == [(GIT_URL, "main")]


def test_default_reuses_compatible_existing_entries_with_zero_provider_calls() -> None:
    """Default mode treats compatible concrete locks as reusable local state."""
    config = full_config()
    existing = make_lockfile(config)
    providers = FakeProviders()

    result = resolve_lockfile(config, existing, providers.resolvers())

    assert result.lockfile == existing
    assert result.changed is False
    providers.assert_zero_calls()


def test_default_rewrites_digest_when_new_selector_accepts_existing_lock() -> None:
    """Compatibility can reuse entries while still updating selector digest metadata."""
    config = make_config(comfyui_version=">=0.25,<0.27", cli_version=">=1.4,<2")
    existing = make_lockfile(config, digest="sha256:" + "0" * 64)
    existing.custom_nodes = []
    providers = FakeProviders()

    result = resolve_lockfile(config, existing, providers.resolvers())

    assert result.lockfile.comfyui == existing.comfyui
    assert result.lockfile.custom_nodes == existing.custom_nodes
    assert result.lockfile.manifest.lock_input_digest == compute_lock_input_digest(
        config
    )
    assert result.changed is True
    providers.assert_zero_calls()


def test_default_reuses_same_moving_git_ref_when_unrelated_selector_changed() -> None:
    """Only Git-selector digest drift refreshes moving Git refs in default mode."""
    old_config = make_config(
        custom_nodes=[
            {"type": "registry", "id": "node"},
            {"type": "git", "url": GIT_URL, "ref": "main"},
        ]
    )
    config = make_config(
        custom_nodes=[
            {"type": "registry", "id": "node", "version": ">=1,<2"},
            {"type": "git", "url": GIT_URL, "ref": "main"},
        ]
    )
    existing = make_lockfile(old_config)
    providers = FakeProviders()
    providers.git.refs["main"] = COMMIT_B

    assert compute_git_custom_nodes_input_digest(config) == (
        compute_git_custom_nodes_input_digest(old_config)
    )

    result = resolve_lockfile(config, existing, providers.resolvers())

    assert result.changed is True
    assert result.lockfile.manifest.lock_input_digest == compute_lock_input_digest(
        config
    )
    assert result.lockfile.custom_nodes == [
        RegistryLockedCustomNode(type="registry", id="node", version="1.5.0"),
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_A),
    ]
    providers.assert_zero_calls()


def test_default_resolves_moving_git_ref_when_git_selector_changed() -> None:
    """Git selector digest changes force moving refs through the provider boundary."""
    old_config = make_config(
        custom_nodes=[{"type": "git", "url": GIT_URL, "ref": "main"}]
    )
    config = make_config(
        custom_nodes=[{"type": "git", "url": GIT_URL, "ref": "release"}]
    )
    existing = make_lockfile(old_config)
    existing.custom_nodes = [
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_A)
    ]
    providers = FakeProviders()
    providers.git.refs["release"] = COMMIT_B

    assert compute_git_custom_nodes_input_digest(config) != (
        compute_git_custom_nodes_input_digest(old_config)
    )

    result = resolve_lockfile(config, existing, providers.resolvers())

    assert result.lockfile.manifest.git_custom_nodes_input_digest == (
        compute_git_custom_nodes_input_digest(config)
    )
    assert result.lockfile.custom_nodes == [
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_B)
    ]
    assert providers.git.ref_calls == [(GIT_URL, "release")]


def test_default_drops_extra_entries_and_keeps_config_order() -> None:
    """Produced non-locked lockfiles contain only config entries in config order."""
    config = make_config(
        custom_nodes=[
            {"type": "git", "url": GIT_URL, "ref": "main"},
            {"type": "registry", "id": "node", "version": "1.5.0"},
        ]
    )
    existing = make_lockfile(config)
    existing.custom_nodes = [
        RegistryLockedCustomNode(type="registry", id="extra", version="9.9.9"),
        GitLockedCustomNode(
            type="git", url="https://example.com/extra.git", commit=COMMIT_B
        ),
        RegistryLockedCustomNode(type="registry", id="node", version="1.5.0"),
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_A),
    ]
    providers = FakeProviders()

    result = resolve_lockfile(config, existing, providers.resolvers())

    assert result.lockfile.custom_nodes == [
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_A),
        RegistryLockedCustomNode(type="registry", id="node", version="1.5.0"),
    ]
    providers.assert_zero_calls()


def test_default_resolves_missing_or_incompatible_entries() -> None:
    """Default mode resolves entries absent from or incompatible with the old lock."""
    config = make_config(
        custom_nodes=[
            {"type": "registry", "id": "node", "version": "1.5.0"},
            {"type": "git", "url": GIT_URL, "ref": "main"},
        ]
    )
    existing = make_lockfile(config)
    existing.custom_nodes = [
        RegistryLockedCustomNode(type="registry", id="node", version="0.1.0")
    ]
    providers = FakeProviders()

    result = resolve_lockfile(config, existing, providers.resolvers())

    assert result.lockfile.custom_nodes == [
        RegistryLockedCustomNode(type="registry", id="node", version="1.5.0"),
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_A),
    ]
    assert providers.registry.install_calls == [("node", "1.5.0")]
    assert providers.git.ref_calls == [(GIT_URL, "main")]


def test_locked_fails_when_lockfile_is_missing() -> None:
    """Strict locked mode requires an existing lockfile."""
    config = make_config()
    providers = FakeProviders()

    with pytest.raises(LockServiceError) as raised:
        resolve_lockfile(
            config,
            None,
            providers.resolvers(),
            LockOptions(locked=True),
        )

    assert raised.value.diagnostics[0].code == "lockfile.required"
    providers.assert_zero_calls()


def test_locked_fails_on_digest_mismatch_with_zero_provider_calls() -> None:
    """Strict locked mode requires exact selector digest compatibility."""
    config = full_config()
    existing = make_lockfile(config, digest="sha256:" + "0" * 64)
    providers = FakeProviders()

    with pytest.raises(LockServiceError) as raised:
        resolve_lockfile(
            config,
            existing,
            providers.resolvers(),
            LockOptions(locked=True),
        )

    assert [item.code for item in raised.value.diagnostics] == [
        "lockfile.digest_mismatch"
    ]
    providers.assert_zero_calls()


def test_locked_fails_on_missing_or_incompatible_entries_without_provider_calls() -> (
    None
):
    """Strict locked mode reports local incompatibility without resolver fallback."""
    config = make_config(
        cli_version="1.5.0",
        custom_nodes=[
            {"type": "registry", "id": "node", "version": "1.5.0"},
            {"type": "git", "url": GIT_URL, "ref": "main"},
        ],
    )
    existing = make_lockfile(config)
    existing.comfyui.cli_version = "1.0.0"
    existing.custom_nodes = [
        RegistryLockedCustomNode(type="registry", id="node", version="0.1.0")
    ]
    providers = FakeProviders()

    with pytest.raises(LockServiceError) as raised:
        resolve_lockfile(
            config,
            existing,
            providers.resolvers(),
            LockOptions(locked=True),
        )

    codes = [item.code for item in raised.value.diagnostics]
    assert "lockfile.comfy_cli_incompatible" in codes
    assert "lockfile.registry_incompatible" in codes
    assert "lockfile.git_missing" in codes
    assert "missing git custom-node" in raised.value.diagnostics[-1].message
    providers.assert_zero_calls()


def test_locked_ignores_extra_entries_when_required_entries_are_compatible() -> None:
    """Strict locked mode allows extra lock entries outside the current config."""
    config = full_config()
    existing = make_lockfile(config)
    existing.custom_nodes.append(
        RegistryLockedCustomNode(type="registry", id="extra", version="9.9.9")
    )
    providers = FakeProviders()

    result = resolve_lockfile(
        config,
        existing,
        providers.resolvers(),
        LockOptions(locked=True),
    )

    assert result.lockfile == existing
    assert result.changed is False
    assert result.lockfile is not existing
    providers.assert_zero_calls()


def test_upgrade_lock_refreshes_moving_selectors_and_constraints() -> None:
    """Upgrade mode refreshes non-exact selectors through resolver providers."""
    config = make_config(
        comfyui_version="latest",
        cli_version=">=1,<3",
        custom_nodes=[
            {"type": "registry", "id": "node", "version": ">=1,<2"},
            {"type": "git", "url": GIT_URL, "ref": "main"},
        ],
    )
    existing = make_lockfile(config)
    providers = FakeProviders()
    providers.git.refs["main"] = COMMIT_B

    result = resolve_lockfile(
        config,
        existing,
        providers.resolvers(),
        LockOptions(upgrade_lock=True),
    )

    assert result.lockfile.custom_nodes == [
        RegistryLockedCustomNode(type="registry", id="node", version="1.5.0"),
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_B),
    ]
    assert providers.comfyui.release_calls == 1
    assert providers.comfy_cli.calls == 1
    assert providers.registry.version_calls == ["node"]
    assert providers.registry.install_calls == [("node", "1.5.0")]
    assert providers.git.ref_calls == [(GIT_URL, "main")]


def test_upgrade_lock_keeps_exact_full_git_commit_stable() -> None:
    """Upgrade mode keeps exact full Git commits as already locked selectors."""
    config = make_config(
        comfyui_version="0.26.0",
        cli_version="1.5.0",
        custom_nodes=[{"type": "git", "url": GIT_URL, "ref": COMMIT_A.upper()}],
    )
    existing = make_lockfile(config)
    existing.custom_nodes = [
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_A)
    ]
    providers = FakeProviders()

    result = resolve_lockfile(
        config,
        existing,
        providers.resolvers(),
        LockOptions(upgrade_lock=True),
    )

    assert result.lockfile.custom_nodes == existing.custom_nodes
    providers.assert_zero_calls()


def test_upgrade_lock_reuses_exact_concrete_non_git_selectors() -> None:
    """Upgrade mode reuses exact concrete ComfyUI, cli, and registry locks."""
    config = make_config(
        comfyui_version="0.26.0",
        cli_version="1.5.0",
        custom_nodes=[{"type": "registry", "id": "node", "version": "1.5.0"}],
    )
    existing = make_lockfile(config)
    existing.custom_nodes = [
        RegistryLockedCustomNode(type="registry", id="node", version="1.5.0")
    ]
    providers = FakeProviders()

    result = resolve_lockfile(
        config,
        existing,
        providers.resolvers(),
        LockOptions(upgrade_lock=True),
    )

    assert result.lockfile.comfyui == existing.comfyui
    assert result.lockfile.custom_nodes == existing.custom_nodes
    providers.assert_zero_calls()


@pytest.mark.parametrize(
    "options",
    [
        LockOptions(locked=True, upgrade_lock=True),
        LockOptions(check=True, upgrade_lock=True),
        LockOptions(check=True, dry_run=True),
    ],
)
def test_invalid_option_combinations_fail(options: LockOptions) -> None:
    """Incompatible policy flags fail before provider calls."""
    providers = FakeProviders()

    with pytest.raises(LockServiceError) as raised:
        resolve_lockfile(make_config(), None, providers.resolvers(), options)

    assert raised.value.diagnostics[0].code == "lock.options_incompatible"
    providers.assert_zero_calls()


@pytest.mark.parametrize(
    "options",
    [LockOptions(check=True), LockOptions(dry_run=True)],
)
def test_check_and_dry_run_report_changed_without_mutating_existing(
    options: LockOptions,
) -> None:
    """Non-writing modes return the would-be lockfile and leave the input untouched."""
    config = make_config(
        custom_nodes=[
            {"type": "registry", "id": "node", "version": "1.5.0"},
            {"type": "git", "url": GIT_URL, "ref": COMMIT_A},
        ],
    )
    existing = make_lockfile(config)
    existing.custom_nodes = [
        RegistryLockedCustomNode(type="registry", id="node", version="0.1.0"),
        GitLockedCustomNode(type="git", url=GIT_URL, commit=COMMIT_B),
    ]
    before = dump_lockfile_toml(existing)
    providers = FakeProviders()

    result = resolve_lockfile(config, existing, providers.resolvers(), options)

    assert result.changed is True
    assert result.lockfile != existing
    assert dump_lockfile_toml(existing) == before


def test_check_combined_with_locked_uses_strict_no_resolution_behavior() -> None:
    """Check plus locked still applies the strict no-provider policy."""
    config = full_config()
    existing = make_lockfile(config, digest="sha256:" + "0" * 64)
    providers = FakeProviders()

    with pytest.raises(LockServiceError):
        resolve_lockfile(
            config,
            existing,
            providers.resolvers(),
            LockOptions(locked=True, check=True),
        )

    providers.assert_zero_calls()


def test_check_combined_with_locked_succeeds_without_provider_calls() -> None:
    """Check plus locked succeeds as a strict local compatibility check."""
    config = full_config()
    existing = make_lockfile(config)
    providers = FakeProviders()

    result = resolve_lockfile(
        config,
        existing,
        providers.resolvers(),
        LockOptions(locked=True, check=True),
    )

    assert result.lockfile == existing
    assert result.lockfile is not existing
    assert result.changed is False
    providers.assert_zero_calls()


def test_registry_deprecated_warning_is_propagated() -> None:
    """Registry resolver warnings are surfaced on the lock service result."""
    config = make_config(
        custom_nodes=[{"type": "registry", "id": "node", "version": "1.5.0"}]
    )
    providers = FakeProviders()
    providers.registry.deprecated = True

    result = resolve_lockfile(config, None, providers.resolvers())

    assert [(item.path, item.code, item.severity) for item in result.warnings] == [
        (
            ("comfyui", "custom_nodes", 0, "version"),
            "custom_node.deprecated_registry_version",
            DiagnosticSeverity.WARNING,
        )
    ]
