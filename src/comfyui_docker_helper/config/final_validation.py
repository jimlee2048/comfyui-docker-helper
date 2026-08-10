"""Layered validation for public configuration models."""

import posixpath
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticComparison,
    DiagnosticComparisonSite,
    DiagnosticError,
    DiagnosticPath,
    DiagnosticSeverity,
)
from comfyui_docker_helper.config.final_models import (
    FinalConfig,
    FinalGitCredentialConfig,
    FinalGitCustomNodeConfig,
    FinalRegistryCustomNodeConfig,
)
from comfyui_docker_helper.config.git_credentials import (
    GIT_CREDENTIAL_VALUE_MAX_BYTES,
    GitCredentialContextError,
    has_password_userinfo,
    parse_git_credential_context,
)
from comfyui_docker_helper.config.hook_validation import validate_hook_relative_path
from comfyui_docker_helper.config.merge import OriginNode
from comfyui_docker_helper.config.os_packages import (
    DEFAULT_OS_PACKAGES,
    validate_apt_package_identity,
)
from comfyui_docker_helper.config.publication_tags import (
    static_release_availability,
    validate_publication_tags,
)
from comfyui_docker_helper.config.registry_identity import (
    registry_distribution_identity,
    registry_resource_identity,
    validate_registry_id,
)
from comfyui_docker_helper.config.requirement_validation import (
    DirectRequirementError,
    is_stable_public_operand,
    parse_direct_requirement,
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
    validate_managed_python_support_range,
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
_SECRET_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_OCI_TAG_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_SCP_GIT_URL_PATTERN = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:[^\s:][^\s]*\Z")
_GIT_REF_FORBIDDEN_CHARACTERS = frozenset(" ~^:?*[\\")
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
    value: str
    name: str
    extras: tuple[str, ...]
    specifier: str

    @property
    def canonical_value(self) -> str:
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        return f"{self.name}{extras}{self.specifier}"


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
    git_credential_contexts: tuple[LocatedValue, ...]
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
    build_hooks_dir: str | Path | None = None,
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
    git_credential_contexts: list[LocatedValue] = []

    _validate_platform_domain(config, diagnostics)
    _validate_python_domain(config, package_requirements, diagnostics)
    _validate_pytorch_domain(config, package_requirements, diagnostics)
    workspace, comfyui_path = _validate_system_domains(
        config, apt_packages, diagnostics
    )
    _validate_comfyui_domains(
        config,
        build_hooks_dir,
        registry_ids,
        registry_nodes,
        git_urls,
        git_targets,
        controlled_extra_args,
        diagnostics,
    )
    _validate_file_domains(config, file_targets, diagnostics)
    _validate_build_domains(config, diagnostics)
    _validate_secret_domains(config, diagnostics)
    _validate_git_credential_domains(config, git_credential_contexts, diagnostics)

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
        git_credential_contexts=tuple(git_credential_contexts),
        workspace=workspace,
        comfyui_path=comfyui_path,
    )


def validate_final_config_semantics(
    config: FinalConfig,
    domains: FinalConfigDomainResult,
    *,
    origins: OriginNode | None = None,
) -> tuple[Diagnostic, ...]:
    """Enforce normalized duplicates and cross-field relationships once."""
    diagnostics: list[Diagnostic] = []
    _duplicate_diagnostics(
        domains.platforms,
        "build.duplicate_platform",
        "platforms must not contain duplicates",
        diagnostics,
        origins=origins,
        display_values=True,
    )
    _package_owner_diagnostics(
        domains.package_requirements,
        diagnostics,
        origins=origins,
    )
    _duplicate_diagnostics(
        domains.apt_packages,
        "system.duplicate_apt_package",
        "package names must be unique",
        diagnostics,
        origins=origins,
        display_values=True,
        initial_seen=frozenset(DEFAULT_OS_PACKAGES),
    )
    _duplicate_diagnostics(
        domains.registry_ids,
        "custom_node.duplicate_registry_id",
        "Registry resource IDs must be unique ignoring case",
        diagnostics,
        normalize=registry_resource_identity,
        origins=origins,
        display_values=True,
    )
    _registry_distribution_identity_diagnostics(
        domains.registry_ids,
        diagnostics,
        origins=origins,
    )
    _duplicate_diagnostics(
        domains.git_urls,
        "custom_node.duplicate_git_url",
        "Git URLs must be unique",
        diagnostics,
        origins=origins,
    )
    _duplicate_diagnostics(
        domains.git_credential_contexts,
        "git_credential.duplicate_match",
        "credential match contexts must be unique after normalization",
        diagnostics,
        origins=origins,
    )
    _secret_reference_diagnostics(config, diagnostics)
    _duplicate_diagnostics(
        domains.git_targets,
        "custom_node.duplicate_git_target_dir",
        "Git target directories must be unique",
        diagnostics,
        origins=origins,
    )
    _duplicate_diagnostics(
        domains.file_targets,
        "file.duplicate_target",
        "file targets must be unique",
        diagnostics,
        origins=origins,
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
    else:
        try:
            validate_managed_python_support_range(value)
        except ValueError:
            diagnostics.append(
                Diagnostic(
                    ("python", "version"),
                    "python.unsupported_version",
                    "must satisfy the supported range >=3.12,<3.15",
                )
            )
    if config.python.uv_version != "latest" and not _is_exact_stable_release(
        config.python.uv_version
    ):
        diagnostics.append(
            Diagnostic(
                ("python", "uv_version"),
                "python.invalid_uv_version",
                "must be latest or an exact stable major.minor.patch release",
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
    try:
        identity = parse_direct_requirement(value)
    except DirectRequirementError as error:
        diagnostics.append(Diagnostic(path, error.code, str(error)))
        return None
    return NormalizedRequirement(
        path,
        value,
        identity.name,
        identity.extras,
        identity.specifier,
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
    build_hooks_dir: str | Path | None,
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
    root = _validate_build_hooks_root(hooks, build_hooks_dir, diagnostics)
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
    if any(not is_stable_public_operand(item) for item in specifiers):
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
        invalid = any(not is_stable_public_operand(item) for item in operands)
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
    try:
        validate_registry_id(node.id)
    except ValueError:
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
    password_userinfo = has_password_userinfo(node.url)
    url_valid = is_git_source_url(node.url) and not password_userinfo
    if password_userinfo:
        diagnostics.append(
            Diagnostic(
                (*path, "url"),
                "custom_node.password_userinfo_forbidden",
                "HTTP(S) Git URLs must not contain a password",
            )
        )
    elif not url_valid:
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
    for field in ("pre_install_hooks", "post_install_hooks"):
        for hook_index, hook in enumerate(getattr(node, field)):
            yield hook, (*base, field, hook_index)


def _validate_build_hooks_root(
    hooks: tuple[tuple[str, DiagnosticPath], ...],
    build_hooks_dir: str | Path | None,
    diagnostics: list[Diagnostic],
) -> Path | None:
    if not hooks:
        return None
    if build_hooks_dir is None:
        diagnostics.append(
            Diagnostic(
                ("build_hooks_dir",),
                "hook.build_hooks_dir_required",
                "--build-hooks-dir is required when build hooks are configured",
            )
        )
        return None
    root = Path(build_hooks_dir)
    try:
        mode = root.lstat().st_mode
    except OSError:
        diagnostics.append(
            Diagnostic(
                ("build_hooks_dir",),
                "hook.build_hooks_dir_not_directory",
                "must be an existing regular directory",
            )
        )
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        diagnostics.append(
            Diagnostic(
                ("build_hooks_dir",),
                "hook.build_hooks_dir_not_directory",
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
    try:
        hook = PurePosixPath(validate_hook_relative_path(value))
    except ValueError as error:
        if "must end" in str(error):
            diagnostics.append(
                Diagnostic(path, "hook.unsupported_extension", "must end in .sh or .py")
            )
            return
        diagnostics.append(
            Diagnostic(path, "hook.invalid_path", "must be a normalized relative path")
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
    release_available: bool | None = None
    with suppress(ValueError):
        release_available = static_release_availability(config.comfyui.version)

    diagnostics.extend(
        Diagnostic(("build", "tags", issue.index), issue.code, issue.message)
        for issue in validate_publication_tags(
            config.build.tags,
            release_available=release_available,
        )
    )


def _validate_secret_domains(
    config: FinalConfig,
    diagnostics: list[Diagnostic],
) -> None:
    for name, source in config.secrets.items():
        path: DiagnosticPath = ("secrets", name)
        if _SECRET_NAME_PATTERN.fullmatch(name) is None:
            diagnostics.append(
                Diagnostic(
                    path,
                    "secret.invalid_name",
                    "names must match [a-z][a-z0-9_-]{0,63}",
                )
            )
        if (source.env is None) == (source.file is None):
            diagnostics.append(
                Diagnostic(
                    path,
                    "secret.invalid_source",
                    "must define exactly one of env or file",
                )
            )
        if (
            source.env is not None
            and _ENVIRONMENT_NAME_PATTERN.fullmatch(source.env) is None
        ):
            diagnostics.append(
                Diagnostic(
                    (*path, "env"),
                    "secret.invalid_env",
                    "environment variable names must match [A-Za-z_][A-Za-z0-9_]*",
                )
            )
        if source.file is not None and not _is_valid_secret_file_locator(source.file):
            diagnostics.append(
                Diagnostic(
                    (*path, "file"),
                    "secret.invalid_file",
                    "file locators must name a POSIX file path without control "
                    "characters, backslashes, or a trailing separator",
                )
            )


def _is_valid_secret_file_locator(value: str) -> bool:
    """Return whether one locator can lexically name a POSIX file."""
    return (
        bool(value)
        and not has_control_characters(value)
        and "\\" not in value
        and not value.endswith("/")
        and value.rsplit("/", 1)[-1] not in {".", ".."}
        and not posixpath.normpath(value).startswith("//")
    )


def _validate_git_credential_domains(
    config: FinalConfig,
    contexts: list[LocatedValue],
    diagnostics: list[Diagnostic],
) -> None:
    for index, route in enumerate(config.cdh.git.credentials):
        path: DiagnosticPath = ("cdh", "git", "credentials", index)
        _validate_git_credential_route(route, path, contexts, diagnostics)


def _validate_git_credential_route(
    route: FinalGitCredentialConfig,
    path: DiagnosticPath,
    contexts: list[LocatedValue],
    diagnostics: list[Diagnostic],
) -> None:
    try:
        context = parse_git_credential_context(route.match)
    except GitCredentialContextError as error:
        code = (
            "git_credential.password_userinfo_forbidden"
            if error.code == "password_userinfo"
            else "git_credential.invalid_match"
        )
        diagnostics.append(Diagnostic((*path, "match"), code, str(error)))
    else:
        contexts.append(LocatedValue((*path, "match"), context.canonical_url))
        if context.scheme == "http":
            diagnostics.append(
                Diagnostic(
                    (*path, "match"),
                    "git_credential.insecure_http",
                    "credentials sent over HTTP lack TLS transport confidentiality",
                    DiagnosticSeverity.WARNING,
                )
            )

    if (
        not route.username
        or any(character in route.username for character in "\x00\r\n")
        or len(route.username.encode("utf-8")) > GIT_CREDENTIAL_VALUE_MAX_BYTES
    ):
        diagnostics.append(
            Diagnostic(
                (*path, "username"),
                "git_credential.invalid_username",
                "must be non-empty, contain no NUL, CR, or LF, and fit the Git "
                "credential protocol",
            )
        )
    if _SECRET_NAME_PATTERN.fullmatch(route.password.secret) is None:
        diagnostics.append(
            Diagnostic(
                (*path, "password", "secret"),
                "secret.invalid_reference",
                "Secret references must match [a-z][a-z0-9_-]{0,63}",
            )
        )


def _secret_reference_diagnostics(
    config: FinalConfig,
    diagnostics: list[Diagnostic],
) -> None:
    for index, route in enumerate(config.cdh.git.credentials):
        name = route.password.secret
        if (
            _SECRET_NAME_PATTERN.fullmatch(name) is not None
            and name not in config.secrets
        ):
            diagnostics.append(
                Diagnostic(
                    ("cdh", "git", "credentials", index, "password", "secret"),
                    "secret.unknown_reference",
                    "must reference a defined Secret",
                )
            )


def _duplicate_diagnostics(
    values: tuple[LocatedValue, ...],
    code: str,
    message: str,
    diagnostics: list[Diagnostic],
    *,
    normalize: Callable[[str], str] | None = None,
    origins: OriginNode | None = None,
    display_values: bool = False,
    initial_seen: frozenset[str] = frozenset(),
) -> None:
    seen: dict[str, LocatedValue | None] = {value: None for value in initial_seen}
    for item in values:
        value = item.value if normalize is None else normalize(item.value)
        if value in seen:
            established = seen[value]
            diagnostics.append(
                Diagnostic(
                    item.path,
                    code,
                    message,
                    source_context=(
                        _comparison(
                            established.path,
                            item.path,
                            origins,
                            earlier_value=established.value if display_values else None,
                            later_value=item.value if display_values else None,
                        )
                        if established is not None
                        else None
                    ),
                )
            )
        else:
            seen[value] = item


def _registry_distribution_identity_diagnostics(
    values: tuple[LocatedValue, ...],
    diagnostics: list[Diagnostic],
    *,
    origins: OriginNode | None,
) -> None:
    established: dict[str, LocatedValue] = {}
    for item in values:
        distribution = registry_distribution_identity(item.value)
        earlier = established.get(distribution)
        if earlier is None:
            established[distribution] = item
            continue
        if registry_resource_identity(earlier.value) == registry_resource_identity(
            item.value
        ):
            continue
        diagnostics.append(
            Diagnostic(
                item.path,
                "custom_node.registry_distribution_identity_collision",
                "distinct Registry IDs map to the same installed Python "
                "distribution identity",
                source_context=_comparison(
                    earlier.path,
                    item.path,
                    origins,
                    earlier_value=earlier.value,
                    later_value=item.value,
                ),
                hint="Keep only one of these Registry nodes.",
            )
        )


def _package_owner_diagnostics(
    requirements: tuple[NormalizedRequirement, ...],
    diagnostics: list[Diagnostic],
    *,
    origins: OriginNode | None,
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
    authored_application_owners: dict[str, NormalizedRequirement] = {}
    authored_tool_owners: dict[str, NormalizedRequirement] = {}
    for requirement in requirements:
        owners = (
            tool_owners
            if requirement.path[:2] == ("python", "uv_tools")
            else application_owners
        )
        authored_owners = (
            authored_tool_owners
            if requirement.path[:2] == ("python", "uv_tools")
            else authored_application_owners
        )
        reserved = (
            python_extra_reserved.get(requirement.name)
            if requirement.path[:2] == ("python", "extra_packages")
            else None
        ) or owners.get(requirement.name)
        existing = authored_owners.get(requirement.name)
        if reserved is not None:
            owner_text = (
                "reserved by"
                if reserved in reserved_owner_paths
                else "already owned at"
            )
            diagnostics.append(
                Diagnostic(
                    requirement.path,
                    "python.duplicate_package_owner",
                    f"package {requirement.name} is {owner_text} "
                    f"{_format_path(reserved)}",
                )
            )
        elif existing is not None:
            diagnostics.append(
                Diagnostic(
                    requirement.path,
                    "python.conflicting_package_requirement",
                    f"package {requirement.name} has conflicting requirements",
                    source_context=_comparison(
                        existing.path,
                        requirement.path,
                        origins,
                        earlier_value=existing.value,
                        later_value=requirement.value,
                    ),
                    hint="Use one requirement for this package.",
                )
            )
        else:
            authored_owners[requirement.name] = requirement


def _comparison(
    earlier_path: DiagnosticPath,
    later_path: DiagnosticPath,
    origins: OriginNode | None,
    *,
    earlier_value: str | None = None,
    later_value: str | None = None,
) -> DiagnosticComparison | None:
    if origins is None:
        return None
    earlier = origins.exact_location(earlier_path)
    later = origins.exact_location(later_path)
    if earlier is None or later is None:
        return None
    return DiagnosticComparison(
        DiagnosticComparisonSite(earlier, earlier_value),
        DiagnosticComparisonSite(later, later_value),
    )


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
