"""Strict ComfyUI requirements projection and PyTorch-group merge rules."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.canonical_lock import DirectPythonRequestMember
from comfyui_docker_helper.exact_ledger import (
    CUDA_PROTECTED_REQUIREMENTS as CUDA_PROTECTED_REQUIREMENTS,
)

COMFYUI_REQUIREMENTS_PATH = "requirements.txt"
COMFYUI_REQUIREMENTS_POLICY_VERSION = 1
_SOURCE_OPTION = re.compile(
    r"(?:-e(?:ditable)?|-i|--index-url|--extra-index-url|--find-links|"
    r"--trusted-host|--no-index|--pre|--prefer-binary|--only-binary|"
    r"--no-binary|--require-hashes|--constraint|-c)"
    r"(?:\s|=|$)"
)


class ComfyUIRequirementsError(ValueError):
    """One contextual strict-requirements or merge failure."""


@dataclass(frozen=True, slots=True)
class ParsedComfyUIRequirements:
    """Canonical target projection plus ordinary install remainder."""

    digest: str
    protected: tuple[DirectPythonRequestMember, ...]
    ordinary: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeclaredManagerRequirement:
    """One target-active checkout-declared Manager requirement."""

    package: str
    requirement: str
    specifier: str


@dataclass(frozen=True, slots=True)
class ParsedManagerRequirements:
    """Strict install rows plus the exact checkout-owned Manager identity."""

    digest: str
    rows: tuple[str, ...]
    active: tuple[DeclaredManagerRequirement, ...]
    manager_version: str


def protected_policy_digest(protected_names: tuple[str, ...]) -> str:
    """Bind the parser policy and adapter-owned protected-name set."""
    names = _normalized_protected_names(protected_names)
    payload = json.dumps(
        {"policy_version": COMFYUI_REQUIREMENTS_POLICY_VERSION, "names": names},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def parse_comfyui_requirements(
    content: bytes,
    *,
    python_version: str,
    platform: str,
    protected_names: tuple[str, ...],
) -> ParsedComfyUIRequirements:
    """Parse PEP 508 rows and optionally project target-active protected members."""
    names = (
        set(_normalized_protected_names(protected_names)) if protected_names else set()
    )
    environment = target_marker_environment(python_version, platform)
    protected: list[DirectPythonRequestMember] = []
    ordinary: list[str] = []
    try:
        document = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ComfyUIRequirementsError("ComfyUI requirements must be UTF-8") from error
    for line_number, raw in enumerate(document.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or _SOURCE_OPTION.match(line):
            raise ComfyUIRequirementsError(
                f"ComfyUI requirements line {line_number} changes package sources"
            )
        try:
            requirement = Requirement(line)
        except InvalidRequirement as error:
            raise ComfyUIRequirementsError(
                f"ComfyUI requirements line {line_number} is not valid PEP 508"
            ) from error
        if requirement.url is not None:
            raise ComfyUIRequirementsError(
                f"ComfyUI requirements line {line_number} uses a direct source"
            )
        name = canonicalize_name(requirement.name)
        if name in names:
            if requirement.marker is None or requirement.marker.evaluate(environment):
                protected.append(_member(requirement))
            continue
        ordinary.append(str(requirement))
    merged = merge_requirement_members(tuple(protected))
    return ParsedComfyUIRequirements(
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        protected=merged,
        ordinary=tuple(ordinary),
    )


def parse_ordinary_requirements(
    content: bytes,
    *,
    python_version: str,
    platform: str,
) -> tuple[str, ...]:
    """Return strict ordinary rows for a cdh-owned requirements operation."""
    return parse_comfyui_requirements(
        content,
        python_version=python_version,
        platform=platform,
        protected_names=(),
    ).ordinary


def parse_manager_requirements(
    content: bytes,
    *,
    python_version: str,
    platform: str,
) -> ParsedManagerRequirements:
    """Parse checkout-owned Manager requirements without accepting source control."""
    environment = target_marker_environment(python_version, platform)
    rows: list[str] = []
    active: list[DeclaredManagerRequirement] = []
    seen_active: set[str] = set()
    try:
        document = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ComfyUIRequirementsError("Manager requirements must be UTF-8") from error
    for line_number, raw in enumerate(document.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or _SOURCE_OPTION.match(line):
            raise ComfyUIRequirementsError(
                f"Manager requirements line {line_number} changes package sources"
            )
        try:
            requirement = Requirement(line)
        except InvalidRequirement as error:
            raise ComfyUIRequirementsError(
                f"Manager requirements line {line_number} is not valid PEP 508"
            ) from error
        if requirement.url is not None:
            raise ComfyUIRequirementsError(
                f"Manager requirements line {line_number} uses a direct source"
            )
        canonical = str(requirement)
        rows.append(canonical)
        if requirement.marker is not None and not requirement.marker.evaluate(
            environment
        ):
            continue
        package = canonicalize_name(requirement.name)
        if package in seen_active:
            raise ComfyUIRequirementsError(
                f"Manager requirements duplicate target package {package}"
            )
        seen_active.add(package)
        active.append(
            DeclaredManagerRequirement(
                package=package,
                requirement=canonical,
                specifier=str(requirement.specifier),
            )
        )
    manager = [item for item in active if item.package == "comfyui-manager"]
    if len(manager) != 1:
        raise ComfyUIRequirementsError(
            "Manager requirements must declare exactly one active comfyui-manager"
        )
    manager_version = _exact_version(
        manager[0].specifier,
        subject="comfyui-manager",
    )
    return ParsedManagerRequirements(
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        rows=tuple(rows),
        active=tuple(active),
        manager_version=manager_version,
    )


def merge_pytorch_requirements(
    mandatory_torch: DirectPythonRequestMember,
    upstream: tuple[DirectPythonRequestMember, ...],
    configured_extras: tuple[DirectPythonRequestMember, ...],
) -> tuple[DirectPythonRequestMember, ...]:
    """Merge all direct PyTorch ownership into one deterministic request."""
    if mandatory_torch.package != "torch":
        raise ComfyUIRequirementsError("mandatory PyTorch member must be torch")
    return merge_requirement_members((mandatory_torch, *upstream, *configured_extras))


def merge_requirement_members(
    members: tuple[DirectPythonRequestMember, ...],
) -> tuple[DirectPythonRequestMember, ...]:
    """Merge normalized PEP 503 identities, extras, and compatible selectors."""
    grouped: dict[str, list[DirectPythonRequestMember]] = {}
    for member in members:
        canonical = DirectPythonRequestMember.model_validate(
            member.model_dump(mode="python")
        )
        grouped.setdefault(canonical.package, []).append(canonical)
    result: list[DirectPythonRequestMember] = []
    for package in sorted(grouped):
        values = grouped[package]
        extras = sorted({extra for item in values for extra in item.extras})
        selector = _merge_selectors(package, tuple(item.selector for item in values))
        result.append(
            DirectPythonRequestMember(
                package=package,
                extras=extras,
                selector=selector,
            )
        )
    return tuple(result)


def _member(requirement: Requirement) -> DirectPythonRequestMember:
    try:
        selector = str(SpecifierSet(str(requirement.specifier)))
    except InvalidSpecifier as error:  # pragma: no cover - Requirement owns syntax.
        raise ComfyUIRequirementsError("requirement specifier is invalid") from error
    return DirectPythonRequestMember(
        package=canonicalize_name(requirement.name),
        extras=sorted(canonicalize_name(extra) for extra in requirement.extras),
        selector=selector,
    )


def _exact_version(specifier: str, *, subject: str) -> str:
    clauses = tuple(SpecifierSet(specifier))
    if len(clauses) != 1 or clauses[0].operator != "==" or "*" in clauses[0].version:
        raise ComfyUIRequirementsError(
            f"{subject} must have one exact checkout-owned version"
        )
    try:
        version = Version(clauses[0].version)
    except InvalidVersion as error:
        raise ComfyUIRequirementsError(
            f"{subject} has an invalid exact version"
        ) from error
    value = str(version)
    if value != clauses[0].version:
        raise ComfyUIRequirementsError(
            f"{subject} must have one canonical exact checkout-owned version"
        )
    return value


def _merge_selectors(package: str, selectors: tuple[str, ...]) -> str:
    clauses = sorted(
        {
            clause
            for selector in selectors
            if selector
            for clause in selector.split(",")
            if clause
        }
    )
    combined = str(SpecifierSet(",".join(clauses))) if clauses else ""
    exact: set[Version] = set()
    for clause in clauses:
        if (
            clause.startswith("==")
            and not clause.startswith("===")
            and "*" not in clause
        ):
            try:
                exact.add(Version(clause[2:]))
            except InvalidVersion as error:
                raise ComfyUIRequirementsError(
                    f"protected requirement {package} has an invalid exact selector"
                ) from error
    if len(exact) > 1:
        raise ComfyUIRequirementsError(
            f"protected requirement {package} has conflicting exact selectors"
        )
    if exact and not SpecifierSet(combined).contains(
        next(iter(exact)), prereleases=True
    ):
        raise ComfyUIRequirementsError(
            f"protected requirement {package} has incompatible selectors"
        )
    return combined


def _normalized_protected_names(names: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted(canonicalize_name(name) for name in names))
    if not normalized or len(normalized) != len(set(normalized)):
        raise ComfyUIRequirementsError(
            "protected requirement names must be non-empty and unique"
        )
    return normalized


def target_marker_environment(python_version: str, platform: str) -> dict[str, str]:
    """Return the exact target environment used for PEP 508 marker evaluation."""
    try:
        parsed = Version(python_version)
    except InvalidVersion as error:
        raise ComfyUIRequirementsError("target Python version is invalid") from error
    if platform != "linux/amd64" or len(parsed.release) < 2:
        raise ComfyUIRequirementsError("requirements target is unsupported")
    environment = default_environment()
    environment.update(
        {
            "implementation_name": "cpython",
            "implementation_version": python_version,
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_python_implementation": "CPython",
            "platform_system": "Linux",
            "python_full_version": python_version,
            "python_version": ".".join(str(value) for value in parsed.release[:2]),
            "sys_platform": "linux",
            "extra": "",
        }
    )
    return environment
