"""Layered validation for public configuration models."""

import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import Specifier, SpecifierSet
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticError,
    DiagnosticPath,
)
from comfyui_docker_helper.config.final_models import (
    FinalConfig,
    FinalGitCustomNodeConfig,
    FinalRegistryCustomNodeConfig,
)
from comfyui_docker_helper.config.selector_validation import (
    normalize_comfyui_version,
    normalize_registry_version,
    resolve_git_target_dir,
)
from comfyui_docker_helper.config.ssh_keys import normalize_ssh_public_keys
from comfyui_docker_helper.config.url_validation import (
    is_http_url,
    validate_file_name,
    validate_relative_file_directory,
)
from comfyui_docker_helper.config.value_validation import (
    has_control_characters,
    is_argv_value,
)
from comfyui_docker_helper.exact_ledger import (
    COMFYUI_MINIMUM_VERSION,
    CUDA_PROTECTED_REQUIREMENTS,
)

_EXACT_RELEASE_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
_CUDA_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_APT_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.-]*\Z")
_OCI_TAG_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_SCP_GIT_URL_PATTERN = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:[^\s:][^\s]*\Z")
_GIT_REF_FORBIDDEN_CHARACTERS = frozenset(" ~^:?*[\\")
_HOOK_SUFFIXES = frozenset({".py", ".sh"})
_CONTROLLED_LAUNCH_FLAGS = frozenset(
    {"--listen", "--port", "--auto-launch", "--disable-auto-launch"}
)
_MANAGED_ENV_KEYS = frozenset(
    {
        "VIRTUAL_ENV",
        "PATH",
        "WORKSPACE",
        "COMFYUI_PATH",
        "DEBIAN_FRONTEND",
    }
)
_MANAGED_PACKAGE_ENV_PREFIXES = ("UV_", "PIP_")
_COMFYUI_FLOOR = Version(COMFYUI_MINIMUM_VERSION)


class FinalConfigError(DiagnosticError):
    """Expected final-config validation failure."""


@dataclass(frozen=True, slots=True)
class LocatedValue:
    """One domain-normalized value and its public diagnostic path."""

    path: DiagnosticPath
    value: str


@dataclass(frozen=True, slots=True)
class NormalizedRequirement:
    """Resolution-relevant identity of one validated direct requirement."""

    path: DiagnosticPath
    name: str
    extras: tuple[str, ...]
    specifier: str


@dataclass(frozen=True, slots=True)
class FinalConfigDomainResult:
    """Focused domain-pass output consumed by the single semantic pass."""

    diagnostics: tuple[Diagnostic, ...]
    platforms: tuple[LocatedValue, ...]
    package_requirements: tuple[NormalizedRequirement, ...]
    apt_packages: tuple[LocatedValue, ...]
    registry_ids: tuple[LocatedValue, ...]
    registry_nodes: tuple[DiagnosticPath, ...]
    git_urls: tuple[LocatedValue, ...]
    git_targets: tuple[LocatedValue, ...]
    file_targets: tuple[LocatedValue, ...]
    controlled_extra_args: tuple[LocatedValue, ...]
    workspace: PurePosixPath | None
    comfyui_path: PurePosixPath | None


def validate_final_config_structure(document: Mapping[str, Any]) -> FinalConfig:
    """Apply only strict Pydantic ingress validation to a final config document."""
    try:
        return FinalConfig.model_validate(document)
    except ValidationError as error:
        diagnostics = tuple(
            Diagnostic(
                path=tuple(item["loc"]),
                code=f"schema.{item['type']}",
                message=item["msg"],
            )
            for item in error.errors(include_url=False, include_context=False)
        )
        raise FinalConfigError(diagnostics) from error


def validate_final_config_domains(
    config: FinalConfig,
    *,
    scripts_dir: str | Path | None = None,
) -> FinalConfigDomainResult:
    """Validate individual consumer domains without enforcing relationships."""
    diagnostics: list[Diagnostic] = []
    package_requirements: list[NormalizedRequirement] = []
    apt_packages: list[LocatedValue] = []
    registry_ids: list[LocatedValue] = []
    registry_nodes: list[DiagnosticPath] = []
    git_urls: list[LocatedValue] = []
    git_targets: list[LocatedValue] = []
    file_targets: list[LocatedValue] = []
    controlled_extra_args: list[LocatedValue] = []

    _validate_platform_domain(config, diagnostics)
    _validate_python_domain(config, package_requirements, diagnostics)
    _validate_pytorch_domain(config, package_requirements, diagnostics)
    workspace, comfyui_path = _validate_system_domains(
        config, apt_packages, diagnostics
    )
    _validate_comfyui_domains(
        config,
        scripts_dir,
        registry_ids,
        registry_nodes,
        git_urls,
        git_targets,
        controlled_extra_args,
        diagnostics,
    )
    _validate_file_domains(config, file_targets, diagnostics)
    _validate_build_domains(config, diagnostics)

    return FinalConfigDomainResult(
        diagnostics=tuple(diagnostics),
        platforms=tuple(
            LocatedValue(("build", "platforms", index), platform)
            for index, platform in enumerate(config.build.platforms)
        ),
        package_requirements=tuple(package_requirements),
        apt_packages=tuple(apt_packages),
        registry_ids=tuple(registry_ids),
        registry_nodes=tuple(registry_nodes),
        git_urls=tuple(git_urls),
        git_targets=tuple(git_targets),
        file_targets=tuple(file_targets),
        controlled_extra_args=tuple(controlled_extra_args),
        workspace=workspace,
        comfyui_path=comfyui_path,
    )


def validate_final_config_semantics(
    config: FinalConfig,
    domains: FinalConfigDomainResult,
) -> tuple[Diagnostic, ...]:
    """Enforce normalized duplicates and cross-field relationships once."""
    diagnostics: list[Diagnostic] = []
    _duplicate_diagnostics(
        domains.platforms,
        "build.duplicate_platform",
        "platforms must not contain duplicates",
        diagnostics,
    )
    _package_owner_diagnostics(domains.package_requirements, diagnostics)
    _duplicate_diagnostics(
        domains.apt_packages,
        "system.duplicate_apt_package",
        "package names must be unique",
        diagnostics,
    )
    _duplicate_diagnostics(
        domains.registry_ids,
        "custom_node.duplicate_registry_id",
        "registry IDs must be unique",
        diagnostics,
        normalize=lambda value: canonicalize_name(value, validate=True),
    )
    _duplicate_diagnostics(
        domains.git_urls,
        "custom_node.duplicate_git_url",
        "Git URLs must be unique",
        diagnostics,
    )
    _duplicate_diagnostics(
        domains.git_targets,
        "custom_node.duplicate_git_target_dir",
        "Git target directories must be unique",
        diagnostics,
    )
    _duplicate_diagnostics(
        domains.file_targets,
        "file.duplicate_target",
        "file targets must be unique",
        diagnostics,
    )
    if domains.workspace is not None and domains.comfyui_path == domains.workspace:
        diagnostics.append(
            Diagnostic(
                ("system", "comfyui_path"),
                "system.comfyui_path_equals_workspace",
                "must not equal system.workspace",
            )
        )
    if not config.comfyui.install_manager:
        diagnostics.extend(
            Diagnostic(
                path,
                "custom_node.manager_required",
                "registry nodes require install_manager = true",
            )
            for path in domains.registry_nodes
        )
    diagnostics.extend(
        Diagnostic(
            item.path,
            "comfyui.controlled_extra_arg",
            "must not override cdh-owned launch flags",
        )
        for item in domains.controlled_extra_args
    )
    return tuple(diagnostics)


def validate_final_config(
    config: FinalConfig,
    *,
    scripts_dir: str | Path | None = None,
) -> tuple[Diagnostic, ...]:
    """Run the focused domain pass, then the one semantic pass."""
    domains = validate_final_config_domains(config, scripts_dir=scripts_dir)
    return (*domains.diagnostics, *validate_final_config_semantics(config, domains))


def _validate_platform_domain(
    config: FinalConfig,
    diagnostics: list[Diagnostic],
) -> None:
    if not _CUDA_VERSION_PATTERN.fullmatch(config.compute_platform.cuda.version):
        diagnostics.append(
            Diagnostic(
                ("compute_platform", "cuda", "version"),
                "compute_platform.invalid_cuda_version",
                "must use major.minor or major.minor.patch numeric format",
            )
        )


def _validate_python_domain(
    config: FinalConfig,
    requirements: list[NormalizedRequirement],
    diagnostics: list[Diagnostic],
) -> None:
    value = config.python.version
    if not _is_exact_stable_release(value):
        diagnostics.append(
            Diagnostic(
                ("python", "version"),
                "python.exact_patch_required",
                "must be an exact stable CPython major.minor.patch version",
            )
        )
    if not is_oci_tag(config.python.uv_version):
        diagnostics.append(
            Diagnostic(
                ("python", "uv_version"),
                "python.invalid_uv_version",
                "must be one non-empty control-free OCI tag",
            )
        )
    _validate_http_index(
        config.python.index_url,
        ("python", "index_url"),
        "python.invalid_index_url",
        diagnostics,
    )
    _collect_requirements(
        "python", config.python.extra_packages, requirements, diagnostics
    )
    _collect_requirements(
        "python",
        config.python.uv_tools,
        requirements,
        diagnostics,
        field="uv_tools",
    )


def _validate_pytorch_domain(
    config: FinalConfig,
    requirements: list[NormalizedRequirement],
    diagnostics: list[Diagnostic],
) -> None:
    if not _is_exact_stable_release(config.pytorch.version):
        diagnostics.append(
            Diagnostic(
                ("pytorch", "version"),
                "pytorch.exact_stable_version_required",
                "must be an exact stable public PyTorch version",
            )
        )
    _validate_http_index(
        config.pytorch.index_base_url,
        ("pytorch", "index_base_url"),
        "pytorch.invalid_index_base_url",
        diagnostics,
    )
    _collect_requirements(
        "pytorch", config.pytorch.extra_packages, requirements, diagnostics
    )


def _is_exact_stable_release(value: str) -> bool:
    if not _EXACT_RELEASE_PATTERN.fullmatch(value):
        return False
    try:
        version = Version(value)
    except InvalidVersion:
        return False
    return not (
        version.is_prerelease
        or version.is_devrelease
        or version.local is not None
        or version.public != value
    )


def _validate_http_index(
    value: str,
    path: DiagnosticPath,
    code: str,
    diagnostics: list[Diagnostic],
) -> None:
    if not is_http_url(value):
        diagnostics.append(Diagnostic(path, code, "must be an HTTP(S) URL with a host"))


def _collect_requirements(
    group: str,
    values: list[str],
    requirements: list[NormalizedRequirement],
    diagnostics: list[Diagnostic],
    *,
    field: str = "extra_packages",
) -> None:
    for index, value in enumerate(values):
        path: DiagnosticPath = (group, field, index)
        normalized = validate_direct_requirement(value, path, diagnostics)
        if normalized is not None:
            requirements.append(normalized)


def validate_direct_requirement(
    value: str,
    path: DiagnosticPath,
    diagnostics: list[Diagnostic],
) -> NormalizedRequirement | None:
    if not value.strip() or value != value.strip() or has_control_characters(value):
        diagnostics.append(
            Diagnostic(
                path,
                "python.invalid_requirement",
                "must be a non-empty unambiguous PEP 508 requirement",
            )
        )
        return None
    try:
        requirement = Requirement(value)
    except InvalidRequirement:
        diagnostics.append(
            Diagnostic(
                path,
                "python.invalid_requirement",
                "must be a supported PEP 508 package requirement",
            )
        )
        return None
    if requirement.url is not None:
        diagnostics.append(
            Diagnostic(
                path,
                "python.direct_requirement_forbidden",
                "must not use a URL, VCS, local, or editable requirement",
            )
        )
        return None
    if requirement.marker is not None:
        diagnostics.append(
            Diagnostic(
                path,
                "python.environment_marker_forbidden",
                "must not use an environment marker",
            )
        )
        return None
    if any(
        spec.operator not in {"==", "!=", "<", "<=", ">", ">="} or "*" in spec.version
        for spec in requirement.specifier
    ):
        diagnostics.append(
            Diagnostic(
                path,
                "python.unsupported_requirement_selector",
                "must use an exact version or bounded comparison selector",
            )
        )
        return None
    for specifier in requirement.specifier:
        if not _is_stable_public_operand(specifier):
            diagnostics.append(
                Diagnostic(
                    path,
                    "python.prerelease_selector_forbidden",
                    "selector operands must be stable public versions",
                )
            )
            return None
    operators = {specifier.operator for specifier in requirement.specifier}
    if operators == {"=="} and len(requirement.specifier) != 1:
        diagnostics.append(
            Diagnostic(
                path,
                "python.ambiguous_exact_requirement",
                "must contain exactly one exact version selector",
            )
        )
        return None
    if (
        requirement.specifier
        and operators != {"=="}
        and (not operators & {">", ">="} or not operators & {"<", "<="})
    ):
        diagnostics.append(
            Diagnostic(
                path,
                "python.unbounded_requirement_selector",
                "comparison selectors must include lower and upper bounds",
            )
        )
        return None
    return NormalizedRequirement(
        path,
        canonicalize_name(requirement.name),
        tuple(sorted({canonicalize_name(extra) for extra in requirement.extras})),
        str(requirement.specifier),
    )


def _is_stable_public_operand(specifier: Specifier) -> bool:
    try:
        operand = Version(specifier.version)
    except InvalidVersion:
        return False
    return not (
        operand.is_prerelease or operand.is_devrelease or operand.local is not None
    )


def _validate_system_domains(
    config: FinalConfig,
    apt_packages: list[LocatedValue],
    diagnostics: list[Diagnostic],
) -> tuple[PurePosixPath | None, PurePosixPath | None]:
    workspace = _absolute_container_path(
        config.system.workspace, ("system", "workspace"), diagnostics
    )
    comfyui_path = None
    if config.system.comfyui_path is not None:
        comfyui_path = _absolute_container_path(
            config.system.comfyui_path,
            ("system", "comfyui_path"),
            diagnostics,
        )
    for index, package in enumerate(config.system.extra_packages):
        path: DiagnosticPath = ("system", "extra_packages", index)
        try:
            validate_apt_package_identity(package)
        except ValueError:
            diagnostics.append(
                Diagnostic(
                    path, "system.invalid_apt_package", "must be one apt package name"
                )
            )
        else:
            apt_packages.append(LocatedValue(path, package))
    for name, value in config.system.env.items():
        path: DiagnosticPath = ("system", "env", name)
        if not _ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            diagnostics.append(
                Diagnostic(
                    path,
                    "system.invalid_env_name",
                    "name must be a valid environment variable",
                )
            )
        if is_managed_environment_name(name):
            diagnostics.append(
                Diagnostic(
                    path, "system.managed_env_override", f"must not override {name}"
                )
            )
        if has_control_characters(value):
            diagnostics.append(
                Diagnostic(
                    path,
                    "system.invalid_env_value",
                    "must not contain control characters",
                )
            )
    _, key_diagnostics = normalize_ssh_public_keys(
        config.system.ssh.pub_keys,
        path=("system", "ssh", "pub_keys"),
        code="ssh.invalid_public_key",
    )
    diagnostics.extend(key_diagnostics)
    min_split_size = config.cdh.downloader.aria2.min_split_size
    if not is_aria2_argument_value(min_split_size):
        diagnostics.append(
            Diagnostic(
                ("cdh", "downloader", "aria2", "min_split_size"),
                "cdh.downloader.invalid_aria2_min_split_size",
                "must be a non-empty control-free aria2 argument value",
            )
        )
    return workspace, comfyui_path


def _absolute_container_path(
    value: str,
    path: DiagnosticPath,
    diagnostics: list[Diagnostic],
) -> PurePosixPath | None:
    parsed = PurePosixPath(value)
    if (
        not value
        or has_control_characters(value)
        or not parsed.is_absolute()
        or str(parsed) != value
        or "." in parsed.parts
        or ".." in parsed.parts
    ):
        diagnostics.append(
            Diagnostic(
                path,
                "path.invalid_absolute_container_path",
                "must be an absolute normalized POSIX path",
            )
        )
        return None
    return parsed


def _validate_comfyui_domains(
    config: FinalConfig,
    scripts_dir: str | Path | None,
    registry_ids: list[LocatedValue],
    registry_nodes: list[DiagnosticPath],
    git_urls: list[LocatedValue],
    git_targets: list[LocatedValue],
    controlled_extra_args: list[LocatedValue],
    diagnostics: list[Diagnostic],
) -> None:
    comfyui = config.comfyui
    _validate_comfyui_selector(comfyui.version, diagnostics)
    if not is_argv_value(comfyui.listen):
        diagnostics.append(
            Diagnostic(
                ("comfyui", "listen"),
                "comfyui.invalid_listen",
                "must be non-empty and control-free",
            )
        )
    for index, argument in enumerate(comfyui.extra_args):
        path: DiagnosticPath = ("comfyui", "extra_args", index)
        if not is_argv_value(argument):
            diagnostics.append(
                Diagnostic(
                    path,
                    "comfyui.invalid_extra_arg",
                    "must be non-empty and control-free",
                )
            )
        elif argument.split("=", maxsplit=1)[0] in _CONTROLLED_LAUNCH_FLAGS:
            controlled_extra_args.append(LocatedValue(path, argument))

    hooks = tuple(_iter_hooks(config))
    root = _validate_scripts_root(hooks, scripts_dir, diagnostics)
    for index, node in enumerate(comfyui.custom_nodes):
        path: DiagnosticPath = ("comfyui", "custom_nodes", index)
        if isinstance(node, FinalRegistryCustomNodeConfig):
            _validate_registry_node(
                node, path, registry_ids, registry_nodes, diagnostics
            )
        else:
            _validate_git_node(node, path, git_urls, git_targets, diagnostics)
        for hook, hook_path in _iter_node_hooks(index, node):
            _validate_hook(hook, hook_path, root, diagnostics)


def _validate_comfyui_selector(value: str, diagnostics: list[Diagnostic]) -> None:
    path: DiagnosticPath = ("comfyui", "version")
    try:
        normalized = normalize_comfyui_version(value)
    except ValueError as error:
        diagnostics.append(Diagnostic(path, "comfyui.invalid_version", str(error)))
        return
    if normalized in {"latest", "nightly"}:
        return
    if len(normalized) == 40 and all(
        character in "0123456789abcdef" for character in normalized
    ):
        return
    if normalized[0].isdigit():
        version = Version(normalized)
        if version.is_prerelease or version.is_devrelease or version.local is not None:
            diagnostics.append(
                Diagnostic(
                    path,
                    "comfyui.formal_stable_release_required",
                    "exact selectors must name a stable formal release",
                )
            )
        elif version < _COMFYUI_FLOOR:
            diagnostics.append(
                Diagnostic(
                    path,
                    "comfyui.version_below_floor",
                    f"must resolve to ComfyUI {COMFYUI_MINIMUM_VERSION} or newer",
                )
            )
        return
    specifiers = SpecifierSet(normalized)
    if any(not _is_stable_public_operand(item) for item in specifiers):
        diagnostics.append(
            Diagnostic(
                path,
                "comfyui.prerelease_selector_forbidden",
                "selector operands must be stable public releases",
            )
        )
        return
    if not _stable_selector_satisfiable(specifiers, floor=_COMFYUI_FLOOR):
        below_floor = _selector_definitely_below_floor(specifiers, floor=_COMFYUI_FLOOR)
        diagnostics.append(
            Diagnostic(
                path,
                (
                    "comfyui.version_below_floor"
                    if below_floor
                    else "comfyui.unsatisfiable_selector"
                ),
                (
                    f"must allow ComfyUI {COMFYUI_MINIMUM_VERSION} or newer"
                    if below_floor
                    else (
                        "must allow at least one stable ComfyUI "
                        f"{COMFYUI_MINIMUM_VERSION} or newer release"
                    )
                ),
            )
        )


def _stable_selector_satisfiable(
    specifiers: SpecifierSet,
    *,
    floor: Version,
) -> bool:
    exact = [Version(item.version) for item in specifiers if item.operator == "=="]
    if exact:
        candidate = _formal_release_at_or_after(exact[0], inclusive=True)
        return (
            len(set(exact)) == 1
            and candidate == exact[0]
            and candidate >= floor
            and specifiers.contains(candidate, prereleases=False)
        )

    lower = floor
    lower_inclusive = True
    for item in specifiers:
        operand = Version(item.version)
        if item.operator in {">", ">="}:
            inclusive = item.operator == ">="
            if operand > lower:
                lower, lower_inclusive = operand, inclusive
            elif operand == lower:
                lower_inclusive = lower_inclusive and inclusive
    candidate = _formal_release_at_or_after(lower, inclusive=lower_inclusive)
    excluded_points = sum(1 for item in specifiers if item.operator == "!=")
    for _ in range(excluded_points + 1):
        if specifiers.contains(candidate, prereleases=False):
            return True
        candidate = _next_formal_patch(candidate)
    return False


def _formal_release_at_or_after(value: Version, *, inclusive: bool) -> Version:
    release = (*value.release[:3], 0, 0, 0)[:3]
    candidate = Version(".".join(str(part) for part in release))
    if not inclusive or candidate < value:
        candidate = _next_formal_patch(candidate)
    return candidate


def _next_formal_patch(value: Version) -> Version:
    major, minor, patch = (*value.release[:3], 0, 0, 0)[:3]
    return Version(f"{major}.{minor}.{patch + 1}")


def _selector_definitely_below_floor(
    specifiers: SpecifierSet,
    *,
    floor: Version,
) -> bool:
    exact = [Version(item.version) for item in specifiers if item.operator == "=="]
    if exact:
        return all(item < floor for item in exact)
    return any(
        (item.operator == "<" and Version(item.version) <= floor)
        or (item.operator == "<=" and Version(item.version) < floor)
        for item in specifiers
    )


def _validate_published_selector(
    value: str,
    path: DiagnosticPath,
    code: str,
    normalizer: Callable[[str], str],
    diagnostics: list[Diagnostic],
) -> None:
    try:
        normalized = normalizer(value)
    except ValueError as error:
        diagnostics.append(Diagnostic(path, code, str(error)))
        return
    if normalized == "latest":
        return
    if any(character in normalized for character in "<>=!"):
        operands = SpecifierSet(normalized)
        invalid = any(not _is_stable_public_operand(item) for item in operands)
    else:
        # Exact published Registry versions may be prereleases; its domain
        # normalizer already rejects malformed/local identities.
        invalid = False
    if invalid:
        diagnostics.append(
            Diagnostic(path, code, "must select only stable public releases")
        )


def _validate_registry_node(
    node: FinalRegistryCustomNodeConfig,
    path: DiagnosticPath,
    registry_ids: list[LocatedValue],
    registry_nodes: list[DiagnosticPath],
    diagnostics: list[Diagnostic],
) -> None:
    registry_nodes.append((*path, "type"))
    id_valid = is_argv_value(node.id) and not node.id.startswith("-")
    if id_valid:
        try:
            canonicalize_name(node.id, validate=True)
        except InvalidName:
            id_valid = False
    if not id_valid:
        diagnostics.append(
            Diagnostic(
                (*path, "id"),
                "custom_node.invalid_registry_id",
                "must be a non-empty control-free valid Registry project name",
            )
        )
    else:
        registry_ids.append(LocatedValue((*path, "id"), node.id))
    if node.version is not None:
        _validate_published_selector(
            node.version,
            (*path, "version"),
            "custom_node.invalid_registry_version",
            normalize_registry_version,
            diagnostics,
        )


def _validate_git_node(
    node: FinalGitCustomNodeConfig,
    path: DiagnosticPath,
    git_urls: list[LocatedValue],
    git_targets: list[LocatedValue],
    diagnostics: list[Diagnostic],
) -> None:
    url_valid = is_git_source_url(node.url)
    if not url_valid:
        diagnostics.append(
            Diagnostic(
                (*path, "url"),
                "custom_node.invalid_git_url",
                "must be an HTTP(S), SSH, Git, or SCP-style repository URL",
            )
        )
    else:
        git_urls.append(LocatedValue((*path, "url"), node.url))
    if node.ref is not None and not is_git_ref(node.ref):
        diagnostics.append(
            Diagnostic(
                (*path, "ref"),
                "custom_node.invalid_git_ref",
                "must be a valid unambiguous Git ref or full commit",
            )
        )
    if node.target_dir is None and not url_valid:
        return
    target_path = (
        (*path, "target_dir") if node.target_dir is not None else (*path, "url")
    )
    try:
        target = resolve_git_target_dir(node.url, node.target_dir)
    except ValueError as error:
        diagnostics.append(
            Diagnostic(target_path, "custom_node.invalid_git_target_dir", str(error))
        )
    else:
        git_targets.append(LocatedValue(target_path, target))


def is_git_source_url(value: str) -> bool:
    if (
        not is_argv_value(value)
        or value.startswith("-")
        or any(character.isspace() for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme:
        return (
            parsed.scheme.casefold() in {"http", "https", "ssh", "git"}
            and bool(hostname)
            and bool(parsed.path.strip("/"))
        )
    return _SCP_GIT_URL_PATTERN.fullmatch(value) is not None


def is_git_ref(value: str) -> bool:
    if (
        not is_argv_value(value)
        or value.startswith("-")
        or value == "@"
        or value.startswith("/")
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(character in _GIT_REF_FORBIDDEN_CHARACTERS for character in value)
    ):
        return False
    return all(
        part not in {"", ".", ".."}
        and not part.startswith(".")
        and not part.endswith(".lock")
        for part in value.split("/")
    )


def _iter_hooks(config: FinalConfig) -> Iterable[tuple[str, DiagnosticPath]]:
    for index, node in enumerate(config.comfyui.custom_nodes):
        yield from _iter_node_hooks(index, node)


def _iter_node_hooks(
    index: int,
    node: FinalRegistryCustomNodeConfig | FinalGitCustomNodeConfig,
) -> Iterable[tuple[str, DiagnosticPath]]:
    base: DiagnosticPath = ("comfyui", "custom_nodes", index)
    for field in ("pre_install_scripts", "post_install_scripts"):
        for hook_index, hook in enumerate(getattr(node, field)):
            yield hook, (*base, field, hook_index)


def _validate_scripts_root(
    hooks: tuple[tuple[str, DiagnosticPath], ...],
    scripts_dir: str | Path | None,
    diagnostics: list[Diagnostic],
) -> Path | None:
    if not hooks:
        return None
    if scripts_dir is None:
        diagnostics.append(
            Diagnostic(
                ("scripts_dir",),
                "hook.scripts_dir_required",
                "is required when hooks are configured",
            )
        )
        return None
    root = Path(scripts_dir)
    try:
        mode = root.lstat().st_mode
    except OSError:
        diagnostics.append(
            Diagnostic(
                ("scripts_dir",),
                "hook.scripts_dir_not_directory",
                "must be an existing regular directory",
            )
        )
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        diagnostics.append(
            Diagnostic(
                ("scripts_dir",),
                "hook.scripts_dir_not_directory",
                "must be a non-symlink directory",
            )
        )
        return None
    return root


def _validate_hook(
    value: str,
    path: DiagnosticPath,
    root: Path | None,
    diagnostics: list[Diagnostic],
) -> None:
    hook = PurePosixPath(value)
    if (
        not value
        or has_control_characters(value)
        or hook.is_absolute()
        or hook.as_posix() != value
        or "." in hook.parts
        or ".." in hook.parts
    ):
        diagnostics.append(
            Diagnostic(path, "hook.invalid_path", "must be a normalized relative path")
        )
        return
    if hook.suffix not in _HOOK_SUFFIXES:
        diagnostics.append(
            Diagnostic(path, "hook.unsupported_extension", "must end in .sh or .py")
        )
        return
    if root is None:
        return
    candidate = root
    try:
        for part in hook.parts:
            candidate = candidate / part
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError("symlink")
        if not stat.S_ISREG(mode):
            raise ValueError("not regular")
    except (OSError, ValueError):
        diagnostics.append(
            Diagnostic(
                path,
                "hook.source_not_regular",
                "must reference an existing non-symlink regular file",
            )
        )


def _validate_file_domains(
    config: FinalConfig,
    file_targets: list[LocatedValue],
    diagnostics: list[Diagnostic],
) -> None:
    for index, item in enumerate(config.files):
        base: DiagnosticPath = ("files", index)
        if not is_http_url(item.url):
            diagnostics.append(
                Diagnostic(
                    (*base, "url"),
                    "file.invalid_url",
                    "must be an HTTP(S) URL with a host",
                )
            )
        directory = validate_relative_file_directory(item.dir)
        if directory.path is None:
            diagnostics.append(
                Diagnostic(
                    (*base, "dir"),
                    f"file.{directory.code}",
                    directory.message or "invalid directory",
                )
            )
        filename = validate_file_name(item.filename)
        if filename.filename is None:
            diagnostics.append(
                Diagnostic(
                    (*base, "filename"),
                    "file.invalid_filename",
                    filename.message or "invalid filename",
                )
            )
        if directory.path is not None and filename.filename is not None:
            target = f"{directory.path.as_posix()}/{filename.filename}"
            file_targets.append(LocatedValue((*base, "filename"), target))


def _validate_build_domains(
    config: FinalConfig,
    diagnostics: list[Diagnostic],
) -> None:
    for index, tag in enumerate(config.build.tags):
        if (
            not tag
            or any(character.isspace() for character in tag)
            or has_control_characters(tag)
        ):
            diagnostics.append(
                Diagnostic(
                    ("build", "tags", index),
                    "build.invalid_tag",
                    "must be non-empty and contain no whitespace or controls",
                )
            )


def _duplicate_diagnostics(
    values: tuple[LocatedValue, ...],
    code: str,
    message: str,
    diagnostics: list[Diagnostic],
    *,
    normalize: Callable[[str], str] | None = None,
) -> None:
    seen: set[str] = set()
    for item in values:
        value = item.value if normalize is None else normalize(item.value)
        if value in seen:
            diagnostics.append(Diagnostic(item.path, code, message))
        seen.add(value)


def _package_owner_diagnostics(
    requirements: tuple[NormalizedRequirement, ...],
    diagnostics: list[Diagnostic],
) -> None:
    application_owners: dict[str, DiagnosticPath] = {
        "torch": ("pytorch", "version"),
        "pip": ("python", "managed_pip"),
        "setuptools": ("pytorch", "setuptools_policy"),
        "comfyui-docker-helper": ("cdh",),
        "comfy-cli": ("comfyui", "install_cli"),
    }
    python_extra_reserved = {
        name: ("pytorch", "protected_requirements")
        for name in CUDA_PROTECTED_REQUIREMENTS
    }
    python_extra_reserved["torch"] = ("pytorch", "version")
    tool_owners: dict[str, DiagnosticPath] = {
        "comfyui-docker-helper": ("cdh",),
        "comfy-cli": ("comfyui", "install_cli"),
    }
    reserved_owner_paths = frozenset(
        {
            ("pytorch", "version"),
            ("pytorch", "protected_requirements"),
            ("pytorch", "setuptools_policy"),
            ("python", "managed_pip"),
            ("cdh",),
            ("comfyui", "install_cli"),
        }
    )
    for requirement in requirements:
        owners = (
            tool_owners
            if requirement.path[:2] == ("python", "uv_tools")
            else application_owners
        )
        existing = (
            python_extra_reserved.get(requirement.name)
            if requirement.path[:2] == ("python", "extra_packages")
            else None
        ) or owners.get(requirement.name)
        if existing is not None:
            owner_text = (
                "reserved by"
                if existing in reserved_owner_paths
                else "already owned at"
            )
            diagnostics.append(
                Diagnostic(
                    requirement.path,
                    "python.duplicate_package_owner",
                    f"package {requirement.name} is {owner_text} "
                    f"{_format_path(existing)}",
                )
            )
        else:
            owners[requirement.name] = requirement.path


def _format_path(path: DiagnosticPath) -> str:
    return ".".join(str(part) for part in path)


def is_managed_environment_name(name: str) -> bool:
    """Return whether image/package authority reserves an environment name."""
    return name in _MANAGED_ENV_KEYS or name.startswith(_MANAGED_PACKAGE_ENV_PREFIXES)


def is_oci_tag(value: str) -> bool:
    """Return whether a configured OCI tag is safe for provider requests."""
    return _OCI_TAG_PATTERN.fullmatch(value) is not None


def is_aria2_argument_value(value: str) -> bool:
    """Return whether a configured aria2 value is an unambiguous argv token."""
    return is_argv_value(value) and not value.startswith("-")


def validate_apt_package_identity(value: str) -> str:
    """Return one argv-safe apt package identity or fail closed."""
    if _APT_PACKAGE_PATTERN.fullmatch(value) is None:
        raise ValueError("apt package must be one canonical package identity")
    return value
