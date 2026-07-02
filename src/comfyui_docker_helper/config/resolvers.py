"""Source-selection resolvers for lock-domain ComfyUI inputs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticSeverity
from comfyui_docker_helper.config.lock import (
    GitLockedCustomNode,
    LockedComfyUI,
    RegistryLockedCustomNode,
)
from comfyui_docker_helper.config.validation import (
    normalize_comfy_cli_version,
    normalize_comfyui_version,
    normalize_registry_version,
)

COMFYUI_REPO_URL = "https://github.com/comfyanonymous/ComfyUI.git"
COMFY_CLI_PACKAGE_NAME = "comfy-cli"
_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}\Z")


class ResolverError(ValueError):
    """Base class for user-readable resolver failures."""

    def __init__(self, *, source: str, selector: str, reason: str) -> None:
        self.source = source
        self.selector = selector
        self.reason = reason
        super().__init__(f"could not resolve {source} selector {selector!r}: {reason}")


class NoMatchingVersionError(ResolverError):
    """Raised when a selector has no matching upstream candidate."""


class UpstreamResponseError(ResolverError):
    """Raised when upstream data does not satisfy the expected response shape."""


class RegistryVersionListingUnavailableError(ResolverError):
    """Raised when registry version listing is required but unavailable."""


@dataclass(frozen=True, slots=True)
class ComfyUIReleaseCandidate:
    """One ComfyUI release candidate returned by the upstream boundary."""

    version: str
    commit: str


@dataclass(frozen=True, slots=True)
class ResolvedComfyUI:
    """Resolved ComfyUI source selection."""

    repo: str
    commit: str
    version: str | None = None

    def to_locked(self, *, cli_version: str) -> LockedComfyUI:
        """Combine with a resolved comfy-cli version for lockfile storage."""
        return LockedComfyUI(
            repo=self.repo,
            commit=self.commit,
            version=self.version,
            cli_version=cli_version,
        )


@dataclass(frozen=True, slots=True)
class ComfyCliVersionCandidate:
    """One comfy-cli package version candidate returned by the upstream boundary."""

    version: str


@dataclass(frozen=True, slots=True)
class ResolvedComfyCli:
    """Resolved comfy-cli package selection."""

    version: str


@dataclass(frozen=True, slots=True)
class RegistryInstallMetadata:
    """One registry install endpoint response returned by the upstream boundary."""

    node_id: str
    version: str
    active: bool = True
    installable: bool = True
    deprecated: bool = False


@dataclass(frozen=True, slots=True)
class RegistryVersionCandidate:
    """One registry custom-node version candidate returned by the upstream boundary."""

    node_id: str
    version: str
    active: bool = True
    installable: bool = True
    deprecated: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedRegistryCustomNode:
    """Resolved registry custom-node source selection."""

    id: str
    version: str
    warnings: tuple[Diagnostic, ...] = ()

    def to_locked(self) -> RegistryLockedCustomNode:
        """Return the minimal lockfile entry for this registry custom node."""
        return RegistryLockedCustomNode(
            type="registry",
            id=self.id,
            version=self.version,
        )


@dataclass(frozen=True, slots=True)
class ResolvedGitCustomNode:
    """Resolved Git custom-node source selection."""

    url: str
    commit: str

    def to_locked(self) -> GitLockedCustomNode:
        """Return the minimal lockfile entry for this Git custom node."""
        return GitLockedCustomNode(
            type="git",
            url=self.url,
            commit=self.commit,
        )


class ComfyUIReleaseProvider(Protocol):
    """Mockable boundary for ComfyUI release and nightly metadata."""

    def list_releases(self) -> Sequence[ComfyUIReleaseCandidate]:
        """Return ComfyUI release candidates with their source commits."""

    def get_nightly_commit(self) -> str:
        """Return the concrete commit currently selected by nightly."""


class ComfyCliPackageProvider(Protocol):
    """Mockable boundary for comfy-cli package metadata."""

    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        """Return available comfy-cli package versions."""


class RegistryCustomNodeProvider(Protocol):
    """Mockable boundary for Comfy Registry custom-node metadata."""

    def get_install_metadata(
        self,
        node_id: str,
        version: str | None = None,
    ) -> RegistryInstallMetadata:
        """Return install endpoint metadata for a node and optional exact version."""

    def list_versions(self, node_id: str) -> Sequence[RegistryVersionCandidate]:
        """Return available registry versions for one custom-node ID."""


class GitCustomNodeProvider(Protocol):
    """Mockable boundary for Git custom-node ref resolution."""

    def resolve_default_branch_head(self, url: str) -> str:
        """Resolve a repository default branch HEAD to a full commit SHA."""

    def resolve_ref(self, url: str, ref: str) -> str:
        """Resolve an explicit branch, tag, symbolic ref, or commit to a SHA."""


def resolve_comfyui(
    selector: str,
    provider: ComfyUIReleaseProvider,
) -> ResolvedComfyUI:
    """Resolve an already-validated ComfyUI selector to lockfile source data."""
    selector = normalize_comfyui_version(selector)
    if selector == "nightly":
        commit = provider.get_nightly_commit()
        _validate_commit(
            commit,
            source="ComfyUI nightly",
            selector=selector,
        )
        return ResolvedComfyUI(repo=COMFYUI_REPO_URL, commit=commit)

    candidates = _validated_comfyui_candidates(provider.list_releases(), selector)
    if selector == "latest":
        candidate = _highest_stable_comfyui_candidate(candidates, selector)
    elif _looks_like_constraint(selector):
        candidate = _highest_stable_comfyui_candidate(
            [
                candidate
                for candidate in candidates
                if SpecifierSet(selector).contains(candidate.parsed, prereleases=False)
            ],
            selector,
        )
    else:
        requested = _parse_version(selector, source="ComfyUI", selector=selector)
        candidate = _find_exact_stable_comfyui_candidate(
            candidates,
            requested=requested,
            selector=selector,
        )

    return ResolvedComfyUI(
        repo=COMFYUI_REPO_URL,
        commit=candidate.commit.lower(),
        version=candidate.version,
    )


def resolve_comfy_cli(
    selector: str,
    provider: ComfyCliPackageProvider,
) -> ResolvedComfyCli:
    """Resolve an already-validated comfy-cli selector to a concrete version."""
    selector = normalize_comfy_cli_version(selector)
    if selector != "latest" and not _looks_like_constraint(selector):
        return ResolvedComfyCli(version=selector)

    candidates = _validated_comfy_cli_candidates(provider.list_versions(), selector)
    if selector == "latest":
        candidate = _highest_stable_comfy_cli_candidate(candidates, selector)
    else:
        candidate = _highest_stable_comfy_cli_candidate(
            [
                candidate
                for candidate in candidates
                if SpecifierSet(selector).contains(candidate.parsed, prereleases=False)
            ],
            selector,
        )

    return ResolvedComfyCli(version=candidate.version)


def resolve_registry_custom_node(
    node_id: str,
    selector: str | None,
    provider: RegistryCustomNodeProvider,
) -> ResolvedRegistryCustomNode:
    """Resolve an already-validated registry custom-node selector."""
    normalized_selector = _normalize_registry_selector(selector)
    if normalized_selector == "latest":
        install_metadata = provider.get_install_metadata(node_id)
        expected_version = None
    elif _looks_like_constraint(normalized_selector):
        registry_candidates = [
            candidate
            for candidate in _list_registry_versions(
                provider,
                node_id,
                normalized_selector,
            )
            if candidate.node_id == node_id
            and candidate.active
            and candidate.installable
        ]
        candidate = _highest_stable_registry_candidate(
            [
                candidate
                for candidate in _validated_registry_candidates(
                    registry_candidates,
                    selector=normalized_selector,
                )
                if SpecifierSet(normalized_selector).contains(
                    candidate.parsed,
                    prereleases=False,
                )
            ],
            selector=normalized_selector,
        )
        expected_version = candidate.version
        install_metadata = provider.get_install_metadata(node_id, expected_version)
    else:
        expected_version = normalized_selector
        install_metadata = provider.get_install_metadata(node_id, expected_version)

    return _resolved_registry_install_metadata(
        install_metadata,
        node_id=node_id,
        expected_version=expected_version,
        selector=normalized_selector,
    )


def resolve_git_custom_node(
    url: str,
    ref: str | None,
    provider: GitCustomNodeProvider,
) -> ResolvedGitCustomNode:
    """Resolve an already-validated Git custom-node selector to a full commit."""
    selector = _git_selector(ref)
    if ref is not None and _is_commit(ref):
        return ResolvedGitCustomNode(url=url, commit=ref.lower())

    try:
        if ref is None:
            commit = provider.resolve_default_branch_head(url)
        else:
            commit = provider.resolve_ref(url, ref)
    except NoMatchingVersionError:
        raise
    except ResolverError:
        raise
    except LookupError as error:
        raise NoMatchingVersionError(
            source="git custom-node",
            selector=selector,
            reason=f"no commit matches url {url!r}",
        ) from error

    _validate_commit(
        commit,
        source="git custom-node ref",
        selector=selector,
    )
    return ResolvedGitCustomNode(url=url, commit=commit.lower())


def locked_comfyui_satisfies_selector(
    locked: LockedComfyUI,
    selector: str,
) -> bool:
    """Return whether a locked ComfyUI entry can be reused without provider calls."""
    if locked.repo != COMFYUI_REPO_URL or not _is_commit(locked.commit):
        return False

    selector = normalize_comfyui_version(selector)
    if selector == "nightly":
        return locked.version is None
    if locked.version is None:
        return False

    try:
        locked_version = _parse_version(
            locked.version,
            source="locked ComfyUI",
            selector=selector,
        )
    except UpstreamResponseError:
        return False
    if selector == "latest":
        return _is_stable_version(locked_version)
    if _looks_like_constraint(selector):
        return _is_stable_version(locked_version) and SpecifierSet(selector).contains(
            locked_version,
            prereleases=False,
        )
    return locked_version == _parse_version(
        selector,
        source="ComfyUI",
        selector=selector,
    ) and _is_stable_version(locked_version)


def locked_comfy_cli_satisfies_selector(
    locked_version: str,
    selector: str,
) -> bool:
    """Return whether a locked comfy-cli version can be reused without calls."""
    selector = normalize_comfy_cli_version(selector)
    try:
        parsed = _parse_version(
            locked_version,
            source=f"locked {COMFY_CLI_PACKAGE_NAME}",
            selector=selector,
        )
    except UpstreamResponseError:
        return False
    if selector == "latest":
        return _is_stable_version(parsed)
    if _looks_like_constraint(selector):
        return _is_stable_version(parsed) and SpecifierSet(selector).contains(
            parsed,
            prereleases=False,
        )
    return parsed == _parse_version(
        selector,
        source=COMFY_CLI_PACKAGE_NAME,
        selector=selector,
    )


def locked_registry_custom_node_satisfies_selector(
    locked: RegistryLockedCustomNode,
    node_id: str,
    selector: str | None,
) -> bool:
    """Return whether a locked registry entry can be reused without provider calls."""
    if locked.type != "registry" or locked.id != node_id:
        return False

    normalized_selector = _normalize_registry_selector(selector)
    try:
        normalized_locked = _normalize_registry_exact_version(
            locked.version,
            source="locked registry custom-node",
            selector=normalized_selector,
        )
    except UpstreamResponseError:
        return False

    if normalized_selector == "latest":
        return True

    if _looks_like_constraint(normalized_selector):
        try:
            locked_version = _parse_version(
                normalized_locked,
                source="locked registry custom-node",
                selector=normalized_selector,
            )
        except UpstreamResponseError:
            return False
        return _is_stable_version(locked_version) and SpecifierSet(
            normalized_selector
        ).contains(locked_version, prereleases=False)
    return normalized_locked == normalized_selector


def locked_git_custom_node_satisfies_selector(
    locked: GitLockedCustomNode,
    url: str,
    ref: str | None,
) -> bool:
    """Return whether a locked Git entry can be reused without provider calls."""
    if locked.type != "git" or locked.url != url or not _is_commit(locked.commit):
        return False
    if ref is None or not _is_commit(ref):
        return True
    return locked.commit.lower() == ref.lower()


@dataclass(frozen=True, slots=True)
class _ParsedComfyUIReleaseCandidate:
    version: str
    parsed: Version
    commit: str
    source: str


@dataclass(frozen=True, slots=True)
class _ParsedComfyCliVersionCandidate:
    version: str
    parsed: Version


@dataclass(frozen=True, slots=True)
class _ParsedRegistryVersionCandidate:
    node_id: str
    version: str
    parsed: Version
    active: bool
    installable: bool
    deprecated: bool


def _validated_comfyui_candidates(
    candidates: Sequence[ComfyUIReleaseCandidate],
    selector: str,
) -> list[_ParsedComfyUIReleaseCandidate]:
    parsed_candidates: list[_ParsedComfyUIReleaseCandidate] = []
    for index, candidate in enumerate(candidates):
        version = candidate.version.removeprefix("v")
        parsed = _parse_version(
            version,
            source=f"ComfyUI release candidate {index}",
            selector=selector,
        )
        parsed_candidates.append(
            _ParsedComfyUIReleaseCandidate(
                version=parsed.public,
                parsed=parsed,
                commit=candidate.commit,
                source=f"ComfyUI release candidate {index}",
            )
        )
    return parsed_candidates


def _validated_comfy_cli_candidates(
    candidates: Sequence[ComfyCliVersionCandidate],
    selector: str,
) -> list[_ParsedComfyCliVersionCandidate]:
    parsed_candidates: list[_ParsedComfyCliVersionCandidate] = []
    for index, candidate in enumerate(candidates):
        parsed = _parse_version(
            candidate.version,
            source=f"{COMFY_CLI_PACKAGE_NAME} candidate {index}",
            selector=selector,
        )
        if parsed.local is not None:
            raise UpstreamResponseError(
                source=f"{COMFY_CLI_PACKAGE_NAME} candidate {index}",
                selector=selector,
                reason="candidate version must be a public PEP 440 version",
            )
        parsed_candidates.append(
            _ParsedComfyCliVersionCandidate(version=parsed.public, parsed=parsed)
        )
    return parsed_candidates


def _validated_registry_candidates(
    candidates: Sequence[RegistryVersionCandidate],
    selector: str,
) -> list[_ParsedRegistryVersionCandidate]:
    parsed_candidates: list[_ParsedRegistryVersionCandidate] = []
    for index, candidate in enumerate(candidates):
        source = f"registry custom-node version candidate {index}"
        version = _normalize_registry_exact_version(
            candidate.version,
            source=source,
            selector=selector,
        )
        parsed = _try_parse_registry_constraint_candidate(
            version,
            source=source,
            selector=selector,
        )
        if parsed is None:
            continue
        parsed_candidates.append(
            _ParsedRegistryVersionCandidate(
                node_id=candidate.node_id,
                version=version,
                parsed=parsed,
                active=candidate.active,
                installable=candidate.installable,
                deprecated=candidate.deprecated,
            )
        )
    return parsed_candidates


def _highest_stable_comfyui_candidate(
    candidates: Sequence[_ParsedComfyUIReleaseCandidate],
    selector: str,
) -> _ParsedComfyUIReleaseCandidate:
    stable_candidates = [
        candidate for candidate in candidates if _is_stable_version(candidate.parsed)
    ]
    if not stable_candidates:
        raise NoMatchingVersionError(
            source="ComfyUI releases",
            selector=selector,
            reason="no stable release matches the selector",
        )
    candidate = max(stable_candidates, key=lambda candidate: candidate.parsed)
    _validate_comfyui_candidate_commit(candidate, selector)
    return candidate


def _highest_stable_comfy_cli_candidate(
    candidates: Sequence[_ParsedComfyCliVersionCandidate],
    selector: str,
) -> _ParsedComfyCliVersionCandidate:
    stable_candidates = [
        candidate for candidate in candidates if _is_stable_version(candidate.parsed)
    ]
    if not stable_candidates:
        raise NoMatchingVersionError(
            source=COMFY_CLI_PACKAGE_NAME,
            selector=selector,
            reason="no stable public package version matches the selector",
        )
    return max(stable_candidates, key=lambda candidate: candidate.parsed)


def _highest_stable_registry_candidate(
    candidates: Sequence[_ParsedRegistryVersionCandidate],
    selector: str,
) -> _ParsedRegistryVersionCandidate:
    stable_candidates = [
        candidate for candidate in candidates if _is_stable_version(candidate.parsed)
    ]
    if not stable_candidates:
        raise NoMatchingVersionError(
            source="registry custom-node versions",
            selector=selector,
            reason="no stable active installable version matches the selector",
        )
    return max(stable_candidates, key=lambda candidate: candidate.parsed)


def _find_exact_stable_comfyui_candidate(
    candidates: Sequence[_ParsedComfyUIReleaseCandidate],
    *,
    requested: Version,
    selector: str,
) -> _ParsedComfyUIReleaseCandidate:
    for candidate in candidates:
        if candidate.parsed == requested and _is_stable_version(candidate.parsed):
            _validate_comfyui_candidate_commit(candidate, selector)
            return candidate
    raise NoMatchingVersionError(
        source="ComfyUI releases",
        selector=selector,
        reason="no stable release exactly matches the selector",
    )


def _looks_like_constraint(selector: str) -> bool:
    return selector.startswith(("==", "!=", "<=", ">=", "<", ">"))


def _normalize_registry_selector(selector: str | None) -> str:
    if selector is None:
        return "latest"
    return normalize_registry_version(selector)


def _git_selector(ref: str | None) -> str:
    if ref is None:
        return "<default branch>"
    return ref


def _list_registry_versions(
    provider: RegistryCustomNodeProvider,
    node_id: str,
    selector: str,
) -> Sequence[RegistryVersionCandidate]:
    try:
        return provider.list_versions(node_id)
    except RegistryVersionListingUnavailableError:
        raise
    except NotImplementedError as error:
        raise RegistryVersionListingUnavailableError(
            source="registry custom-node versions",
            selector=selector,
            reason="registry version listing is unavailable for constrained selectors",
        ) from error


def _resolved_registry_install_metadata(
    metadata: RegistryInstallMetadata,
    *,
    node_id: str,
    expected_version: str | None,
    selector: str,
) -> ResolvedRegistryCustomNode:
    if metadata.node_id != node_id:
        raise UpstreamResponseError(
            source="registry custom-node install response",
            selector=selector,
            reason=(
                f"response node id {metadata.node_id!r} does not match "
                f"requested node id {node_id!r}"
            ),
        )

    version = _normalize_registry_exact_version(
        metadata.version,
        source="registry custom-node install response",
        selector=selector,
    )
    if expected_version is not None and version != expected_version:
        raise UpstreamResponseError(
            source="registry custom-node install response",
            selector=selector,
            reason=(
                f"response version {version!r} does not match requested "
                f"version {expected_version!r}"
            ),
        )
    if not metadata.active:
        raise UpstreamResponseError(
            source="registry custom-node install response",
            selector=selector,
            reason="selected registry custom-node version is not active",
        )
    if not metadata.installable:
        raise UpstreamResponseError(
            source="registry custom-node install response",
            selector=selector,
            reason="selected registry custom-node version is not installable",
        )

    warnings = ()
    if metadata.deprecated:
        warnings = (
            Diagnostic(
                path=("comfyui", "custom_nodes", node_id, "version"),
                code="custom_node.deprecated_registry_version",
                message=(
                    f"registry custom-node {node_id!r} version {version!r} "
                    "is deprecated"
                ),
                severity=DiagnosticSeverity.WARNING,
            ),
        )
    return ResolvedRegistryCustomNode(id=node_id, version=version, warnings=warnings)


def _validate_comfyui_candidate_commit(
    candidate: _ParsedComfyUIReleaseCandidate,
    selector: str,
) -> None:
    _validate_commit(candidate.commit, source=candidate.source, selector=selector)


def _parse_version(version: str, *, source: str, selector: str) -> Version:
    try:
        parsed = Version(version)
    except InvalidVersion as error:
        raise UpstreamResponseError(
            source=source,
            selector=selector,
            reason=f"candidate version {version!r} is not PEP 440-compatible",
        ) from error
    return parsed


def _parse_registry_version(version: str, *, source: str, selector: str) -> Version:
    return _parse_version(version.removeprefix("v"), source=source, selector=selector)


def _try_parse_registry_constraint_candidate(
    version: str,
    *,
    source: str,
    selector: str,
) -> Version | None:
    try:
        return _parse_registry_version(version, source=source, selector=selector)
    except UpstreamResponseError:
        return None


def _normalize_registry_exact_version(
    version: str,
    *,
    source: str,
    selector: str,
) -> str:
    try:
        normalized = normalize_registry_version(version)
    except ValueError as error:
        raise UpstreamResponseError(
            source=source,
            selector=selector,
            reason=f"version {version!r} is not a supported registry semver",
        ) from error
    if normalized == "latest" or _looks_like_constraint(normalized):
        raise UpstreamResponseError(
            source=source,
            selector=selector,
            reason=f"version {version!r} is not an exact registry semver",
        )
    return normalized


def _is_stable_version(version: Version) -> bool:
    return not (
        version.is_prerelease
        or version.is_devrelease
        or version.is_postrelease
        or version.local is not None
    )


def _validate_commit(commit: str, *, source: str, selector: str) -> None:
    if not _is_commit(commit):
        raise UpstreamResponseError(
            source=source,
            selector=selector,
            reason="commit must be a 40-character hexadecimal SHA",
        )


def _is_commit(commit: str) -> bool:
    return _COMMIT_PATTERN.fullmatch(commit) is not None
