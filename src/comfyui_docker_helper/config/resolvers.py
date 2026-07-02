"""Source-selection resolvers for lock-domain ComfyUI inputs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.lock import LockedComfyUI
from comfyui_docker_helper.config.validation import (
    normalize_comfy_cli_version,
    normalize_comfyui_version,
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
