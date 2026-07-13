"""Shared selector and direct-Git target lexical validation."""

import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

_SEMVER_PATTERN = re.compile(
    r"""
    (?:0|[1-9][0-9]*)\.
    (?:0|[1-9][0-9]*)\.
    (?:0|[1-9][0-9]*)
    (?:-
        (?:
            (?:0|[1-9][0-9]*)
            |[0-9]*[A-Za-z-][0-9A-Za-z-]*
        )
        (?:\.
            (?:
                (?:0|[1-9][0-9]*)
                |[0-9]*[A-Za-z-][0-9A-Za-z-]*
            )
        )*
    )?
    (?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?
    \Z
    """,
    re.VERBOSE,
)
_GIT_TARGET_DIR_PATTERN = re.compile(r"[A-Za-z0-9._-]+\Z")
_FULL_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SUPPORTED_VERSION_CONSTRAINT_OPERATORS = ("==", "!=", "<=", ">=", "<", ">")
_UNSUPPORTED_VERSION_CONSTRAINT_TOKENS = ("~=", "===", "^", "||")


def normalize_comfyui_version(version: str) -> str:
    if version in {"latest", "nightly"}:
        return version
    if _FULL_COMMIT_PATTERN.fullmatch(version):
        return version
    if _looks_like_version_constraint(version):
        return _normalize_version_constraint(version, allow_local_versions=False)
    normalized = version.removeprefix("v")
    if not _SEMVER_PATTERN.fullmatch(normalized):
        raise ValueError(
            "must be latest, nightly, a full lowercase commit, semver, "
            "v-prefixed semver, or a supported PEP 440 comparison constraint"
        )
    return normalized


def normalize_comfy_cli_version(version: str) -> str:
    if version == "latest":
        return version
    if _looks_like_version_constraint(version):
        return _normalize_version_constraint(version, allow_local_versions=False)
    try:
        parsed = Version(version)
    except InvalidVersion as error:
        raise ValueError(
            "must be latest, a PEP 440 public version, or a supported PEP 440 "
            "comparison constraint"
        ) from error
    if parsed.local is not None:
        raise ValueError("must not contain a PEP 440 local-version label")
    return parsed.public


def normalize_registry_version(version: str) -> str:
    if version == "latest":
        return version
    if _looks_like_version_constraint(version):
        return _normalize_version_constraint(version, allow_local_versions=False)
    normalized = version.removeprefix("v")
    if not _SEMVER_PATTERN.fullmatch(normalized):
        raise ValueError(
            "must be latest, semver, v-prefixed semver, or a supported PEP 440 "
            "comparison constraint"
        )
    return normalized


def infer_git_target_dir(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    repo_name = PurePosixPath((path or url).rstrip("/")).name
    if repo_name.endswith(".git"):
        repo_name = repo_name.removesuffix(".git")
    return repo_name if repo_name and repo_name not in {".", ".."} else None


def is_safe_git_target_dir(target_dir: str) -> bool:
    return (
        target_dir not in {"", ".", ".."}
        and _GIT_TARGET_DIR_PATTERN.fullmatch(target_dir) is not None
    )


def resolve_git_target_dir(url: str, target_dir: str | None) -> str:
    if target_dir is not None:
        if not is_safe_git_target_dir(target_dir):
            raise ValueError(
                "target_dir must match [A-Za-z0-9._-]+ and must not be . or .."
            )
        return target_dir
    inferred = infer_git_target_dir(url)
    if inferred is None:
        raise ValueError("cannot derive git target directory from URL")
    return inferred


def _looks_like_version_constraint(version: str) -> bool:
    stripped = version.strip()
    return stripped.startswith((*_SUPPORTED_VERSION_CONSTRAINT_OPERATORS, "~", "^"))


def _normalize_version_constraint(
    version: str,
    *,
    allow_local_versions: bool,
) -> str:
    if any(token in version for token in _UNSUPPORTED_VERSION_CONSTRAINT_TOKENS):
        raise ValueError("must use only ==, !=, <, <=, >, >= comparison constraints")
    if "*" in version:
        raise ValueError("must not use wildcard version constraints")
    parts = [part.strip() for part in version.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError("must be a comma-separated list of comparison constraints")
    if not all(
        part.startswith(_SUPPORTED_VERSION_CONSTRAINT_OPERATORS) for part in parts
    ):
        raise ValueError("must use only ==, !=, <, <=, >, >= comparison constraints")
    try:
        specifier_set = SpecifierSet(version)
    except InvalidSpecifier as error:
        raise ValueError("must be a valid PEP 440 comparison constraint") from error
    if not allow_local_versions:
        for specifier in specifier_set:
            try:
                parsed = Version(specifier.version)
            except InvalidVersion as error:
                raise ValueError(
                    "constraint operands must be public PEP 440 versions"
                ) from error
            if parsed.local is not None:
                raise ValueError("constraint operands must not contain local versions")
    return str(specifier_set)
