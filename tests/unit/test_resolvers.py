"""Tests for ComfyUI and comfy-cli source resolvers."""

from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from comfyui_docker_helper.config import (
    COMFYUI_REPO_URL,
    ComfyCliVersionCandidate,
    ComfyUIReleaseCandidate,
    LockedComfyUI,
    NoMatchingVersionError,
    RegistryInstallMetadata,
    RegistryLockedCustomNode,
    RegistryVersionCandidate,
    RegistryVersionListingUnavailableError,
    UpstreamResponseError,
    locked_comfy_cli_satisfies_selector,
    locked_comfyui_satisfies_selector,
    locked_registry_custom_node_satisfies_selector,
    resolve_comfy_cli,
    resolve_comfyui,
    resolve_registry_custom_node,
)

COMMIT_1 = "1" * 40
COMMIT_2 = "2" * 40
COMMIT_3 = "3" * 40
COMMIT_4 = "4" * 40
COMMIT_5 = "5" * 40


@dataclass(slots=True)
class FakeComfyUIProvider:
    """In-memory provider that records resolver boundary calls."""

    releases: Sequence[ComfyUIReleaseCandidate]
    nightly_commit: str = COMMIT_5
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
    """In-memory package provider that records resolver boundary calls."""

    versions: Sequence[ComfyCliVersionCandidate]
    calls: int = 0

    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        self.calls += 1
        return self.versions


@dataclass(slots=True)
class FakeRegistryProvider:
    """In-memory registry provider that records resolver boundary calls."""

    install_metadata: RegistryInstallMetadata
    versions: Sequence[RegistryVersionCandidate] = ()
    listing_unavailable: bool = False
    install_calls: list[tuple[str, str | None]] | None = None
    version_calls: list[str] | None = None

    def __post_init__(self) -> None:
        self.install_calls = []
        self.version_calls = []

    def get_install_metadata(
        self,
        node_id: str,
        version: str | None = None,
    ) -> RegistryInstallMetadata:
        assert self.install_calls is not None
        self.install_calls.append((node_id, version))
        return self.install_metadata

    def list_versions(self, node_id: str) -> Sequence[RegistryVersionCandidate]:
        assert self.version_calls is not None
        self.version_calls.append(node_id)
        if self.listing_unavailable:
            raise RegistryVersionListingUnavailableError(
                source="registry custom-node versions",
                selector=">=1",
                reason="registry version listing is unavailable",
            )
        return self.versions


def comfyui_candidates() -> list[ComfyUIReleaseCandidate]:
    """Return mixed ComfyUI candidates for stable-only selection tests."""
    return [
        ComfyUIReleaseCandidate(version="0.25.0", commit=COMMIT_1),
        ComfyUIReleaseCandidate(version="v0.26.0", commit=COMMIT_2),
        ComfyUIReleaseCandidate(version="0.27.0rc1", commit=COMMIT_3),
        ComfyUIReleaseCandidate(version="0.27.0.post1", commit=COMMIT_4),
    ]


def comfy_cli_candidates() -> list[ComfyCliVersionCandidate]:
    """Return mixed comfy-cli candidates for stable-only selection tests."""
    return [
        ComfyCliVersionCandidate(version="1.4.0"),
        ComfyCliVersionCandidate(version="1.5.0"),
        ComfyCliVersionCandidate(version="1.6.0rc1"),
        ComfyCliVersionCandidate(version="1.6.0.post1"),
        ComfyCliVersionCandidate(version="2.0.0"),
    ]


def registry_install(
    version: str,
    *,
    node_id: str = "node",
    active: bool = True,
    installable: bool = True,
    deprecated: bool = False,
) -> RegistryInstallMetadata:
    """Return install endpoint metadata for registry resolver tests."""
    return RegistryInstallMetadata(
        node_id=node_id,
        version=version,
        active=active,
        installable=installable,
        deprecated=deprecated,
    )


def test_comfyui_exact_version_resolves_matching_release_commit() -> None:
    """Resolve an exact stable ComfyUI version to its release commit."""
    provider = FakeComfyUIProvider(comfyui_candidates())

    resolved = resolve_comfyui("0.25.0", provider)

    assert resolved.repo == COMFYUI_REPO_URL
    assert resolved.version == "0.25.0"
    assert resolved.commit == COMMIT_1
    assert provider.release_calls == 1
    assert provider.nightly_calls == 0


def test_comfyui_exact_selector_normalizes_leading_v() -> None:
    """Allow users to request v-prefixed exact ComfyUI versions."""
    provider = FakeComfyUIProvider(comfyui_candidates())

    resolved = resolve_comfyui("v0.26.0", provider)

    assert resolved.version == "0.26.0"
    assert resolved.commit == COMMIT_2


def test_comfyui_latest_selects_highest_stable_and_normalizes_leading_v() -> None:
    """Select the highest stable release and store it without a leading v."""
    provider = FakeComfyUIProvider(comfyui_candidates())

    resolved = resolve_comfyui("latest", provider)

    assert resolved.version == "0.26.0"
    assert resolved.commit == COMMIT_2


def test_comfyui_nightly_resolves_commit_without_stable_version() -> None:
    """Resolve nightly through its separate concrete commit boundary."""
    provider = FakeComfyUIProvider(comfyui_candidates(), nightly_commit=COMMIT_5)

    resolved = resolve_comfyui("nightly", provider)

    assert resolved.version is None
    assert resolved.commit == COMMIT_5
    assert provider.release_calls == 0
    assert provider.nightly_calls == 1


def test_comfyui_nightly_rejects_invalid_commit() -> None:
    """Reject malformed nightly metadata."""
    provider = FakeComfyUIProvider(comfyui_candidates(), nightly_commit="not-a-sha")

    with pytest.raises(UpstreamResponseError) as error:
        resolve_comfyui("nightly", provider)

    assert error.value.source == "ComfyUI nightly"
    assert error.value.selector == "nightly"


def test_comfyui_constraint_selects_highest_matching_stable_release() -> None:
    """Resolve constraints with stable-only PEP 440-compatible semantics."""
    provider = FakeComfyUIProvider(comfyui_candidates())

    resolved = resolve_comfyui(">=0.24,<0.27", provider)

    assert resolved.version == "0.26.0"
    assert resolved.commit == COMMIT_2


def test_comfyui_constraint_excludes_prerelease_post_and_local_candidates() -> None:
    """Exclude prerelease, post, and local ComfyUI releases for constraints."""
    provider = FakeComfyUIProvider(
        [
            ComfyUIReleaseCandidate(version="0.27.0rc1", commit=COMMIT_1),
            ComfyUIReleaseCandidate(version="0.27.0.post1", commit=COMMIT_2),
            ComfyUIReleaseCandidate(version="0.27.0+local", commit=COMMIT_3),
            ComfyUIReleaseCandidate(version="0.26.0", commit=COMMIT_4),
        ]
    )

    resolved = resolve_comfyui(">=0.26", provider)

    assert resolved.version == "0.26.0"
    assert resolved.commit == COMMIT_4


def test_comfyui_unselected_invalid_commit_does_not_poison_latest() -> None:
    """Only the selected ComfyUI candidate must have a valid commit."""
    provider = FakeComfyUIProvider(
        [
            ComfyUIReleaseCandidate(version="0.27.0rc1", commit="not-a-sha"),
            ComfyUIReleaseCandidate(version="0.26.0", commit=COMMIT_1),
        ]
    )

    resolved = resolve_comfyui("latest", provider)

    assert resolved.version == "0.26.0"
    assert resolved.commit == COMMIT_1


def test_comfyui_unselected_invalid_commit_does_not_poison_constraint() -> None:
    """Ignore malformed commits on candidates excluded by the selector."""
    provider = FakeComfyUIProvider(
        [
            ComfyUIReleaseCandidate(version="0.27.0", commit="not-a-sha"),
            ComfyUIReleaseCandidate(version="0.26.0", commit=COMMIT_1),
        ]
    )

    resolved = resolve_comfyui("<0.27", provider)

    assert resolved.version == "0.26.0"
    assert resolved.commit == COMMIT_1


def test_comfyui_no_match_reports_selector_and_source() -> None:
    """Raise a user-readable no-match diagnostic."""
    provider = FakeComfyUIProvider(comfyui_candidates())

    with pytest.raises(NoMatchingVersionError) as error:
        resolve_comfyui(">=9", provider)

    assert error.value.selector == ">=9"
    assert error.value.source == "ComfyUI releases"
    assert "no stable release matches" in str(error.value)


def test_comfyui_upstream_response_mismatch_reports_source() -> None:
    """Reject malformed upstream release data before selecting it."""
    provider = FakeComfyUIProvider(
        [ComfyUIReleaseCandidate(version="0.26.0", commit="not-a-sha")]
    )

    with pytest.raises(UpstreamResponseError) as error:
        resolve_comfyui("latest", provider)

    assert error.value.selector == "latest"
    assert error.value.source == "ComfyUI release candidate 0"
    assert "commit must be" in str(error.value)


def test_comfy_cli_exact_version_resolves_public_package_version() -> None:
    """Resolve exact public PEP 440 package versions."""
    provider = FakeComfyCliProvider(comfy_cli_candidates())

    resolved = resolve_comfy_cli("1.5", provider)

    assert resolved.version == "1.5"
    assert provider.calls == 0


def test_comfy_cli_latest_selects_highest_stable_public_package_version() -> None:
    """Select highest stable public comfy-cli version for latest."""
    provider = FakeComfyCliProvider(comfy_cli_candidates())

    resolved = resolve_comfy_cli("latest", provider)

    assert resolved.version == "2.0.0"


def test_comfy_cli_constraint_selects_highest_matching_stable_package() -> None:
    """Resolve comfy-cli constraints by highest stable matching public version."""
    provider = FakeComfyCliProvider(comfy_cli_candidates())

    resolved = resolve_comfy_cli(">=1.4,<2", provider)

    assert resolved.version == "1.5.0"


def test_comfy_cli_constraint_excludes_prerelease_dev_post_and_local_versions() -> None:
    """Exclude prerelease, dev, and post package versions from constraints."""
    provider = FakeComfyCliProvider(
        [
            ComfyCliVersionCandidate(version="1.6.0rc1"),
            ComfyCliVersionCandidate(version="1.6.0.dev1"),
            ComfyCliVersionCandidate(version="1.6.0.post1"),
            ComfyCliVersionCandidate(version="1.5.0"),
        ]
    )

    resolved = resolve_comfy_cli(">=1.0,<2", provider)

    assert resolved.version == "1.5.0"


def test_comfy_cli_upstream_response_mismatch_rejects_local_versions() -> None:
    """Reject non-public package versions from the upstream boundary."""
    provider = FakeComfyCliProvider([ComfyCliVersionCandidate(version="1.6.0+local")])

    with pytest.raises(UpstreamResponseError, match="public PEP 440"):
        resolve_comfy_cli(">=1.0,<2", provider)


def test_comfy_cli_no_match_reports_selector_and_source() -> None:
    """Raise a user-readable no-match diagnostic for package selectors."""
    provider = FakeComfyCliProvider(comfy_cli_candidates())

    with pytest.raises(NoMatchingVersionError) as error:
        resolve_comfy_cli("<1", provider)

    assert error.value.selector == "<1"
    assert error.value.source == "comfy-cli"
    assert "no stable public package version matches" in str(error.value)


def test_registry_latest_uses_install_endpoint_without_listing_versions() -> None:
    """Resolve omitted/latest registry selectors through the install endpoint only."""
    provider = FakeRegistryProvider(registry_install("1.2.3"))

    omitted = resolve_registry_custom_node("node", None, provider)
    latest = resolve_registry_custom_node("node", "latest", provider)

    assert omitted.to_locked() == RegistryLockedCustomNode(
        type="registry",
        id="node",
        version="1.2.3",
    )
    assert latest.version == "1.2.3"
    assert provider.install_calls == [("node", None), ("node", None)]
    assert provider.version_calls == []


def test_registry_exact_semver_uses_install_endpoint_with_normalized_version() -> None:
    """Resolve v-prefixed exact registry selectors through exact install metadata."""
    provider = FakeRegistryProvider(registry_install("v1.2.3"))

    resolved = resolve_registry_custom_node("node", "v1.2.3", provider)

    assert resolved.version == "1.2.3"
    assert provider.install_calls == [("node", "1.2.3")]
    assert provider.version_calls == []


def test_registry_exact_prerelease_preserves_registry_identity() -> None:
    """Do not rewrite exact registry semver prerelease labels to PEP 440 form."""
    provider = FakeRegistryProvider(registry_install("v1.2.3-beta.1"))

    resolved = resolve_registry_custom_node("node", "1.2.3-beta.1", provider)

    assert resolved.version == "1.2.3-beta.1"
    assert provider.install_calls == [("node", "1.2.3-beta.1")]


def test_registry_constraint_selects_highest_stable_active_installable_match() -> None:
    """Enumerate registry versions, then resolve the chosen exact candidate."""
    provider = FakeRegistryProvider(
        registry_install("1.5.0"),
        versions=[
            RegistryVersionCandidate(node_id="node", version="1.4.0"),
            RegistryVersionCandidate(node_id="node", version="1.5.0"),
            RegistryVersionCandidate(node_id="node", version="1.6.0-rc.1"),
            RegistryVersionCandidate(node_id="node", version="1.7.0", active=False),
            RegistryVersionCandidate(
                node_id="node",
                version="1.8.0",
                installable=False,
            ),
            RegistryVersionCandidate(node_id="other", version="1.9.0"),
            RegistryVersionCandidate(node_id="node", version="1.5.1+build"),
        ],
    )

    resolved = resolve_registry_custom_node("node", ">=1.0,<2", provider)

    assert resolved.version == "1.5.0"
    assert provider.version_calls == ["node"]
    assert provider.install_calls == [("node", "1.5.0")]


def test_registry_constraint_ignores_unselected_malformed_candidates() -> None:
    """Filter node/activity/installability before parsing registry versions."""
    provider = FakeRegistryProvider(
        registry_install("1.5.0"),
        versions=[
            RegistryVersionCandidate(node_id="other", version="not-a-version"),
            RegistryVersionCandidate(
                node_id="node",
                version="also-bad",
                active=False,
            ),
            RegistryVersionCandidate(
                node_id="node",
                version="still-bad",
                installable=False,
            ),
            RegistryVersionCandidate(node_id="node", version="1.5.0"),
        ],
    )

    resolved = resolve_registry_custom_node("node", ">=1.0,<2", provider)

    assert resolved.version == "1.5.0"
    assert provider.install_calls == [("node", "1.5.0")]


def test_registry_constraint_skips_semver_prerelease_not_supported_by_pep440() -> None:
    """Skip legal non-stable registry SemVer that cannot be compared by PEP 440."""
    provider = FakeRegistryProvider(
        registry_install("1.5.0"),
        versions=[
            RegistryVersionCandidate(node_id="node", version="1.6.0-foo.1"),
            RegistryVersionCandidate(node_id="node", version="1.5.0"),
        ],
    )

    resolved = resolve_registry_custom_node("node", ">=1,<2", provider)

    assert resolved.version == "1.5.0"
    assert provider.install_calls == [("node", "1.5.0")]


def test_registry_constraint_no_match_reports_user_readable_error() -> None:
    """Fail constrained registry selectors when no candidate matches."""
    provider = FakeRegistryProvider(
        registry_install("1.2.3"),
        versions=[
            RegistryVersionCandidate(node_id="node", version="1.0.0", active=False),
            RegistryVersionCandidate(node_id="node", version="1.1.0-rc.1"),
        ],
    )

    with pytest.raises(NoMatchingVersionError) as error:
        resolve_registry_custom_node("node", ">=1,<2", provider)

    assert error.value.source == "registry custom-node versions"
    assert error.value.selector == "<2,>=1"
    assert provider.install_calls == []


def test_registry_constraint_unavailable_listing_fails_without_latest_fallback() -> (
    None
):
    """Do not treat constrained registry selectors as latest when listing is missing."""
    provider = FakeRegistryProvider(
        registry_install("1.2.3"),
        listing_unavailable=True,
    )

    with pytest.raises(RegistryVersionListingUnavailableError):
        resolve_registry_custom_node("node", ">=1,<2", provider)

    assert provider.version_calls == ["node"]
    assert provider.install_calls == []


def test_registry_install_response_node_id_mismatch_fails() -> None:
    """Verify registry install responses match the requested node ID."""
    provider = FakeRegistryProvider(registry_install("1.2.3", node_id="other"))

    with pytest.raises(UpstreamResponseError, match="node id"):
        resolve_registry_custom_node("node", "latest", provider)


def test_registry_install_response_version_mismatch_fails() -> None:
    """Verify exact registry install responses match the requested version."""
    provider = FakeRegistryProvider(registry_install("1.2.4"))

    with pytest.raises(UpstreamResponseError, match="version"):
        resolve_registry_custom_node("node", "1.2.3", provider)


def test_registry_install_response_build_metadata_mismatch_fails() -> None:
    """Do not compare exact registry responses through lossy PEP 440 public form."""
    provider = FakeRegistryProvider(registry_install("1.2.3+build"))

    with pytest.raises(UpstreamResponseError, match="version"):
        resolve_registry_custom_node("node", "1.2.3", provider)


@pytest.mark.parametrize(
    ("active", "installable", "message"),
    [(False, True, "not active"), (True, False, "not installable")],
)
def test_registry_non_active_or_non_installable_install_response_fails(
    active: bool,
    installable: bool,
    message: str,
) -> None:
    """Fail selected registry install metadata that cannot be installed."""
    provider = FakeRegistryProvider(
        registry_install("1.2.3", active=active, installable=installable)
    )

    with pytest.raises(UpstreamResponseError, match=message):
        resolve_registry_custom_node("node", "latest", provider)


def test_registry_deprecated_response_warns_but_resolves() -> None:
    """Deprecated registry metadata is diagnostic-only for v0.2 resolution."""
    provider = FakeRegistryProvider(registry_install("1.2.3", deprecated=True))

    resolved = resolve_registry_custom_node("node", "latest", provider)

    assert resolved.version == "1.2.3"
    assert len(resolved.warnings) == 1
    assert resolved.warnings[0].code == "custom_node.deprecated_registry_version"
    assert resolved.warnings[0].severity == "warning"


def test_locked_registry_reuse_checks_selector_without_provider_calls() -> None:
    """Strict locked mode can check registry selectors without provider calls."""
    provider = FakeRegistryProvider(registry_install("9.9.9"))
    locked = RegistryLockedCustomNode(type="registry", id="node", version="1.5.0")

    assert locked_registry_custom_node_satisfies_selector(locked, "node", None) is True
    assert (
        locked_registry_custom_node_satisfies_selector(locked, "node", "latest") is True
    )
    assert (
        locked_registry_custom_node_satisfies_selector(locked, "node", ">=1.4,<2")
        is True
    )
    assert (
        locked_registry_custom_node_satisfies_selector(locked, "node", "1.4.0") is False
    )
    assert (
        locked_registry_custom_node_satisfies_selector(locked, "other", "latest")
        is False
    )
    assert provider.install_calls == []
    assert provider.version_calls == []


def test_locked_registry_exact_preserves_prerelease_and_build_identity() -> None:
    """Strict locked checks compare exact registry semver identity, not public form."""
    prerelease = RegistryLockedCustomNode(
        type="registry",
        id="node",
        version="1.2.3-beta.1",
    )
    build = RegistryLockedCustomNode(type="registry", id="node", version="1.2.3+build")

    assert (
        locked_registry_custom_node_satisfies_selector(
            prerelease,
            "node",
            "1.2.3-beta.1",
        )
        is True
    )
    assert (
        locked_registry_custom_node_satisfies_selector(build, "node", "latest") is True
    )
    assert (
        locked_registry_custom_node_satisfies_selector(build, "node", "1.2.3") is False
    )


def test_locked_registry_exact_semver_not_supported_by_pep440_does_not_raise() -> None:
    """Exact registry lock reuse should not parse before identity comparison."""
    locked = RegistryLockedCustomNode(type="registry", id="node", version="1.2.3-foo.1")

    assert (
        locked_registry_custom_node_satisfies_selector(
            locked,
            "node",
            "1.2.3-foo.1",
        )
        is True
    )
    assert (
        locked_registry_custom_node_satisfies_selector(locked, "node", ">=1,<2")
        is False
    )


def test_locked_registry_malformed_version_is_incompatible() -> None:
    """Treat malformed local registry lock data as incompatible."""
    locked = RegistryLockedCustomNode(
        type="registry",
        id="node",
        version="not-a-version",
    )

    assert (
        locked_registry_custom_node_satisfies_selector(locked, "node", ">=1.4,<2")
        is False
    )


def test_locked_comfyui_reuse_checks_selector_without_provider_calls() -> None:
    """Strict locked mode can decide compatibility without network boundaries."""
    provider = FakeComfyUIProvider(comfyui_candidates())
    locked = LockedComfyUI(
        repo=COMFYUI_REPO_URL,
        version="0.26.0",
        commit=COMMIT_2,
        cli_version="1.5.0",
    )

    assert locked_comfyui_satisfies_selector(locked, ">=0.25,<0.27") is True
    assert locked_comfyui_satisfies_selector(locked, "0.25.0") is False
    assert (
        locked_comfyui_satisfies_selector(
            locked.model_copy(update={"repo": "https://example.com/ComfyUI.git"}),
            "0.26.0",
        )
        is False
    )
    assert provider.release_calls == 0
    assert provider.nightly_calls == 0


def test_locked_comfyui_malformed_version_is_incompatible() -> None:
    """Treat malformed local lock data as incompatible, not upstream failure."""
    locked = LockedComfyUI(
        repo=COMFYUI_REPO_URL,
        version="not-a-version",
        commit=COMMIT_2,
        cli_version="1.5.0",
    )

    assert locked_comfyui_satisfies_selector(locked, ">=0.25,<0.27") is False


def test_locked_comfy_cli_reuse_checks_selector_without_provider_calls() -> None:
    """Strict locked mode can check comfy-cli selectors without listing packages."""
    provider = FakeComfyCliProvider(comfy_cli_candidates())

    assert locked_comfy_cli_satisfies_selector("1.5.0", ">=1.4,<2") is True
    assert locked_comfy_cli_satisfies_selector("1.5.0", "latest") is True
    assert locked_comfy_cli_satisfies_selector("1.5.0", "1.4.0") is False
    assert provider.calls == 0


def test_locked_comfy_cli_malformed_version_is_incompatible() -> None:
    """Treat malformed locked comfy-cli values as incompatible."""
    assert locked_comfy_cli_satisfies_selector("not-a-version", ">=1.4,<2") is False
