"""Pure admission and canonical identity for direct Python requirements."""

from dataclasses import dataclass

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import Specifier
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.value_validation import has_control_characters


@dataclass(frozen=True, slots=True)
class DirectRequirementIdentity:
    """Resolution-relevant canonical identity of one admitted requirement."""

    name: str
    extras: tuple[str, ...]
    specifier: str

    @property
    def canonical_value(self) -> str:
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        return f"{self.name}{extras}{self.specifier}"


class DirectRequirementError(ValueError):
    """One stable admission failure without a diagnostic path."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


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
        raise DirectRequirementError(
            "python.direct_requirement_forbidden",
            "must not use a URL, VCS, local, or editable requirement",
        )
    if requirement.marker is not None:
        raise DirectRequirementError(
            "python.environment_marker_forbidden",
            "must not use an environment marker",
        )
    if any(
        spec.operator not in {"==", "!=", "<", "<=", ">", ">="} or "*" in spec.version
        for spec in requirement.specifier
    ):
        raise DirectRequirementError(
            "python.unsupported_requirement_selector",
            "must use an exact version or bounded comparison selector",
        )
    if any(not is_stable_public_operand(item) for item in requirement.specifier):
        raise DirectRequirementError(
            "python.prerelease_selector_forbidden",
            "selector operands must be stable public versions",
        )
    operators = {specifier.operator for specifier in requirement.specifier}
    if operators == {"=="} and len(requirement.specifier) != 1:
        raise DirectRequirementError(
            "python.ambiguous_exact_requirement",
            "must contain exactly one exact version selector",
        )
    if (
        requirement.specifier
        and operators != {"=="}
        and (not operators & {">", ">="} or not operators & {"<", "<="})
    ):
        raise DirectRequirementError(
            "python.unbounded_requirement_selector",
            "comparison selectors must include lower and upper bounds",
        )
    return DirectRequirementIdentity(
        canonicalize_name(requirement.name),
        tuple(sorted({canonicalize_name(extra) for extra in requirement.extras})),
        str(requirement.specifier),
    )


def is_stable_public_operand(specifier: Specifier) -> bool:
    """Return whether a selector operand is one stable public version."""
    try:
        operand = Version(specifier.version)
    except InvalidVersion:
        return False
    return not (
        operand.is_prerelease or operand.is_devrelease or operand.local is not None
    )
