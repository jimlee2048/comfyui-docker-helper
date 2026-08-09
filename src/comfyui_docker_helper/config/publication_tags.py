"""Closed syntax and Docker-reference authority for publication tags."""

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass

from comfyui_docker_helper.config.selector_validation import normalize_comfyui_version

__all__ = [
    "PublicationTagError",
    "PublicationTagIssue",
    "resolve_publication_tags",
    "static_release_availability",
    "validate_publication_tags",
]

_EXPRESSION_PATTERN = re.compile(
    r"\$\{\{ (comfyui\.(?:release|commit|commit\.prefix\(([0-9]+)\))) \}\}"
)
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_IDENTIFIER_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PATH_COMPONENT_PATTERN = re.compile(r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*\Z")
_DOMAIN_LABEL_PATTERN = re.compile(
    r"(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9])\Z"
)
_TAG_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class PublicationTagIssue:
    """One stable indexed publication-tag validation failure."""

    index: int
    code: str
    message: str


class PublicationTagError(ValueError):
    """Expected publication-tag resolution failure."""

    def __init__(self, issues: tuple[PublicationTagIssue, ...]) -> None:
        if not issues:
            raise ValueError("publication tag errors require at least one issue")
        self.issues = issues
        super().__init__("publication tag resolution failed")


def static_release_availability(selector: str) -> bool | None:
    """Return false only when a selector cannot produce a formal release."""
    normalized = normalize_comfyui_version(selector)
    if normalized == "nightly" or _COMMIT_PATTERN.fullmatch(normalized):
        return False
    return None


@dataclass(frozen=True, slots=True)
class _TagExpression:
    start: int
    end: int
    name: str
    prefix_length: int | None


@dataclass(frozen=True, slots=True)
class _ParsedTag:
    value: str
    normalized_identity: str
    expressions: tuple[_TagExpression, ...]


def validate_publication_tags(
    values: Sequence[str],
    *,
    release_available: bool | None = None,
) -> tuple[PublicationTagIssue, ...]:
    """Validate an ordered config or CLI publication-tag list without I/O."""
    issues: list[PublicationTagIssue] = []
    normalized_seen: set[str] = set()
    for index, value in enumerate(values):
        try:
            parsed = _parse_publication_tag(value)
        except ValueError as error:
            code, message = error.args
            issues.append(PublicationTagIssue(index, code, message))
            continue
        if release_available is False and any(
            expression.name == "comfyui.release" for expression in parsed.expressions
        ):
            issues.append(
                PublicationTagIssue(
                    index,
                    "build.release_unavailable",
                    "comfyui.release is unavailable for this ComfyUI selector",
                )
            )
            continue
        if parsed.normalized_identity in normalized_seen:
            issues.append(
                PublicationTagIssue(
                    index,
                    "build.duplicate_tag",
                    "must not duplicate another normalized publication target",
                )
            )
        normalized_seen.add(parsed.normalized_identity)
    return tuple(issues)


def resolve_publication_tags(
    values: Sequence[str],
    commit: str,
    formal_release: str | None,
) -> tuple[str, ...]:
    """Expand an ordered tag list from one accepted exact ComfyUI identity."""
    issues = validate_publication_tags(
        values, release_available=formal_release is not None
    )
    if issues:
        raise PublicationTagError(issues)

    parsed_values = tuple(_parse_publication_tag(value) for value in values)
    commit_indexes = tuple(
        index
        for index, parsed in enumerate(parsed_values)
        if any(
            expression.name in {"comfyui.commit", "comfyui.commit.prefix"}
            for expression in parsed.expressions
        )
    )
    if commit_indexes and _COMMIT_PATTERN.fullmatch(commit) is None:
        raise PublicationTagError(
            tuple(
                PublicationTagIssue(
                    index,
                    "build.invalid_comfyui_commit",
                    "ComfyUI commit must be a full lowercase 40-character SHA",
                )
                for index in commit_indexes
            )
        )

    resolved: list[str] = []
    normalized_seen: set[str] = set()
    resolved_issues: list[PublicationTagIssue] = []
    for index, parsed in enumerate(parsed_values):
        value = _expand_tag(parsed, commit=commit, formal_release=formal_release)
        try:
            final = _parse_publication_tag(value)
        except ValueError as error:
            code, message = error.args
            resolved_issues.append(PublicationTagIssue(index, code, message))
            continue
        if final.normalized_identity in normalized_seen:
            resolved_issues.append(
                PublicationTagIssue(
                    index,
                    "build.duplicate_tag",
                    "must not duplicate another normalized publication target",
                )
            )
        normalized_seen.add(final.normalized_identity)
        resolved.append(value)
    if resolved_issues:
        raise PublicationTagError(tuple(resolved_issues))
    return tuple(resolved)


def _parse_publication_tag(value: str) -> _ParsedTag:
    if not value:
        raise ValueError(
            "build.invalid_image_reference", "must be a non-empty image reference"
        )
    if "@" in value:
        raise ValueError(
            "build.digest_target_forbidden",
            "digest publication targets are not supported",
        )
    if _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            "build.invalid_image_reference",
            "an untagged 64-character image identifier is not a repository name",
        )

    expressions = _parse_expressions(value)
    tag_separator = _tag_separator(value)
    if expressions and tag_separator is None:
        raise ValueError(
            "build.invalid_tag_expression",
            "expressions require an explicit image tag component",
        )
    if tag_separator is not None and any(
        expression.start <= tag_separator for expression in expressions
    ):
        raise ValueError(
            "build.invalid_tag_expression",
            "expressions are allowed only in the image tag component",
        )

    name = value if tag_separator is None else value[:tag_separator]
    tag = None if tag_separator is None else value[tag_separator + 1 :]
    normalized_name = _normalize_repository_name(name)
    if tag is None:
        normalized_tag = "latest"
    else:
        normalized_tag = _validate_tag_component(tag, expressions, tag_separator + 1)
    return _ParsedTag(
        value=value,
        normalized_identity=f"{normalized_name}:{normalized_tag}",
        expressions=expressions,
    )


def _parse_expressions(value: str) -> tuple[_TagExpression, ...]:
    expressions: list[_TagExpression] = []
    cursor = 0
    while True:
        start = value.find("${{", cursor)
        if start < 0:
            break
        match = _EXPRESSION_PATTERN.match(value, start)
        if match is None:
            raise ValueError(
                "build.invalid_tag_expression",
                "must use a supported expression with canonical spacing",
            )
        spelling = match.group(1)
        prefix_text = match.group(2)
        if prefix_text is None:
            name = spelling
            prefix_length = None
        else:
            prefix_length = int(prefix_text)
            if (
                str(prefix_length) != prefix_text
                or prefix_length < 12
                or prefix_length > 40
            ):
                raise ValueError(
                    "build.invalid_tag_expression",
                    "comfyui.commit.prefix length must be an integer from 12 to 40",
                )
            name = "comfyui.commit.prefix"
        expressions.append(_TagExpression(start, match.end(), name, prefix_length))
        cursor = match.end()
    return tuple(expressions)


def _tag_separator(value: str) -> int | None:
    colon = value.rfind(":")
    return colon if colon > value.rfind("/") else None


def _validate_tag_component(
    tag: str,
    expressions: tuple[_TagExpression, ...],
    tag_offset: int,
) -> str:
    fragments: list[str] = []
    cursor = tag_offset
    has_release = False
    for expression in expressions:
        fragments.append(tag[cursor - tag_offset : expression.start - tag_offset])
        if expression.name == "comfyui.release":
            fragments.append("0.0.0")
            has_release = True
        elif expression.name == "comfyui.commit":
            fragments.append("a" * 40)
        else:
            fragments.append("a" * (expression.prefix_length or 0))
        cursor = expression.end
    fragments.append(tag[cursor - tag_offset :])
    representative = "".join(fragments)
    if _TAG_PATTERN.fullmatch(representative) is None:
        message = "tag must match [A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
        if has_release and len(representative) <= 128:
            message = "tag literals and expression placement must follow tag grammar"
        raise ValueError("build.invalid_image_reference", message)
    return tag


def _normalize_repository_name(name: str) -> str:
    components = name.split("/")
    if not components or any(not component for component in components):
        raise ValueError(
            "build.invalid_image_reference",
            "repository name must contain non-empty path components",
        )

    if len(components) > 1 and _looks_like_domain(components[0]):
        domain = _validate_domain(components[0])
        path = components[1:]
    else:
        domain = "docker.io"
        path = components
    if any(_PATH_COMPONENT_PATTERN.fullmatch(component) is None for component in path):
        raise ValueError(
            "build.invalid_image_reference",
            "repository path components must use lowercase distribution syntax",
        )
    domain = domain.lower()
    if domain == "index.docker.io":
        domain = "docker.io"
    if domain == "docker.io" and len(path) == 1:
        path = ["library", *path]
    # distribution/reference limits the normalized remote path, excluding the
    # registry domain but including an inserted Docker Hub "library/" prefix.
    if len("/".join(path)) > 255:
        raise ValueError(
            "build.invalid_image_reference",
            "repository path must not exceed 255 characters",
        )
    return "/".join((domain, *path))


def _looks_like_domain(value: str) -> bool:
    return (
        "." in value
        or ":" in value
        or value.lower() == "localhost"
        or value.startswith("[")
        or value.lower() != value
    )


def _validate_domain(value: str) -> str:
    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            raise ValueError(
                "build.invalid_image_reference", "registry IPv6 address is malformed"
            )
        address = value[1:close]
        suffix = value[close + 1 :]
        if re.fullmatch(r"[A-Fa-f0-9:]+", address) is None or (
            suffix and not _valid_port_suffix(suffix)
        ):
            raise ValueError(
                "build.invalid_image_reference", "registry IPv6 address is malformed"
            )
        try:
            ipaddress.IPv6Address(address)
        except ValueError as error:
            raise ValueError(
                "build.invalid_image_reference", "registry IPv6 address is malformed"
            ) from error
        return value

    if value.count(":") > 1:
        raise ValueError(
            "build.invalid_image_reference",
            "registry IPv6 addresses must use brackets",
        )
    host, separator, port = value.partition(":")
    if separator and (not port or not port.isascii() or not port.isdecimal()):
        raise ValueError(
            "build.invalid_image_reference", "registry port must contain digits"
        )
    if not host or any(
        _DOMAIN_LABEL_PATTERN.fullmatch(label) is None for label in host.split(".")
    ):
        raise ValueError("build.invalid_image_reference", "registry host is malformed")
    return value


def _valid_port_suffix(value: str) -> bool:
    return (
        value.startswith(":")
        and len(value) > 1
        and value[1:].isascii()
        and value[1:].isdecimal()
    )


def _expand_tag(
    parsed: _ParsedTag,
    *,
    commit: str,
    formal_release: str | None,
) -> str:
    fragments: list[str] = []
    cursor = 0
    for expression in parsed.expressions:
        fragments.append(parsed.value[cursor : expression.start])
        if expression.name == "comfyui.release":
            if formal_release is None:
                raise AssertionError("release availability was validated")
            fragments.append(formal_release)
        elif expression.name == "comfyui.commit":
            fragments.append(commit)
        else:
            fragments.append(commit[: expression.prefix_length])
        cursor = expression.end
    fragments.append(parsed.value[cursor:])
    return "".join(fragments)
