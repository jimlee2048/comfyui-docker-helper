"""Pure admission and canonical identity for direct Python requirements."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from packaging.markers import Marker, UndefinedComparison, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.value_validation import has_control_characters

_SUPPORTED_DIRECT_REFERENCE_SCHEMES = frozenset(
    {"http", "https", "git+http", "git+https"}
)
_UNSUPPORTED_REQUIREMENT_MARKER_CONTEXTS = frozenset(
    {"extra", "extras", "dependency_groups"}
)


@dataclass(frozen=True, slots=True)
class DirectRequirementIdentity:
    """Canonical identity of one admitted authored requirement."""

    name: str
    extras: tuple[str, ...]
    specifier: str
    direct_reference: str | None
    marker: str | None

    @property
    def canonical_value(self) -> str:
        value = self.resolver_requirement
        if self.marker is None:
            return value
        separator = " ; " if self.direct_reference is not None else "; "
        return f"{value}{separator}{self.marker}"

    @property
    def resolver_requirement(self) -> str:
        """Render the dependency without its already-authored target marker."""
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        dependency = f"{self.name}{extras}"
        if self.direct_reference is not None:
            return f"{dependency} @ {self.direct_reference}"
        return f"{dependency}{self.specifier}"


class DirectRequirementError(ValueError):
    """One stable admission failure without a diagnostic path."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def target_marker_environment(
    python_version: str, platform: str, machine: str
) -> dict[str, str]:
    """Return the exact target environment used for PEP 508 marker evaluation."""
    try:
        parsed = Version(python_version)
    except InvalidVersion as error:
        raise ValueError("target Python version is invalid") from error
    if platform != "linux/amd64" or machine != "x86_64" or len(parsed.release) < 2:
        raise ValueError("requirements target is unsupported")
    return {
        "implementation_name": "cpython",
        "implementation_version": python_version,
        "os_name": "posix",
        "platform_machine": machine,
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": "Linux",
        "platform_version": "",
        "python_full_version": python_version,
        "python_version": ".".join(str(value) for value in parsed.release[:2]),
        "sys_platform": "linux",
        "extra": "",
    }


def parse_direct_requirement(value: str) -> DirectRequirementIdentity:
    """Validate and return the canonical identity of one direct requirement."""
    if not value.strip() or value != value.strip() or has_control_characters(value):
        raise DirectRequirementError(
            "python.invalid_requirement",
            "must be a non-empty unambiguous PEP 508 requirement",
        )
    try:
        requirement = Requirement(value)
    except InvalidRequirement as error:
        raise DirectRequirementError(
            "python.invalid_requirement",
            "must be a supported PEP 508 package requirement",
        ) from error
    if requirement.url is not None:
        validate_direct_reference(requirement.url)
    return DirectRequirementIdentity(
        canonicalize_name(requirement.name),
        tuple(sorted({canonicalize_name(extra) for extra in requirement.extras})),
        str(requirement.specifier),
        requirement.url,
        str(requirement.marker) if requirement.marker is not None else None,
    )


def validate_direct_reference(value: str) -> None:
    """Admit only public remote transports supported by the execution path."""
    if not value or value != value.strip() or has_control_characters(value):
        raise DirectRequirementError(
            "python.unsupported_direct_reference",
            "must use a supported public remote direct reference",
        )
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise DirectRequirementError(
            "python.unsupported_direct_reference",
            "must use a supported public remote direct reference",
        ) from error
    if (
        parsed.scheme not in _SUPPORTED_DIRECT_REFERENCE_SCHEMES
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DirectRequirementError(
            "python.unsupported_direct_reference",
            "must use a supported public remote direct reference",
        )


def direct_selector_is_exact(value: str) -> bool:
    """Return whether a standard selector contains one explicit exact identity."""
    try:
        specifiers = SpecifierSet(value)
    except InvalidSpecifier:
        return False
    exact = tuple(
        item
        for item in specifiers
        if item.operator in {"==", "==="} and "*" not in item.version
    )
    return len(exact) == 1


def direct_requirement_is_active(
    identity: DirectRequirementIdentity,
    environment: dict[str, str],
) -> bool:
    """Evaluate one authored marker in the standard requirement context."""
    if identity.marker is None:
        return True
    try:
        return Marker(identity.marker).evaluate(
            environment=environment,
            context="requirement",
        )
    except (KeyError, UndefinedEnvironmentName) as error:
        missing = error.args[0] if error.args else None
        if missing in _UNSUPPORTED_REQUIREMENT_MARKER_CONTEXTS:
            raise DirectRequirementError(
                "python.unsupported_marker_context",
                "marker requires an unsupported extra or dependency-group context",
            ) from error
        raise
    except UndefinedComparison as error:
        raise DirectRequirementError(
            "python.invalid_environment_marker",
            "marker comparison is undefined for the target values",
        ) from error
