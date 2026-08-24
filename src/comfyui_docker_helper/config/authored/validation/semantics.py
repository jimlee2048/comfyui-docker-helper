"""Semantic validation for authored configuration."""

from collections.abc import Callable

from comfyui_docker_helper.config.authored.models import (
    FinalConfig,
    FinalHttpFileConfig,
)
from comfyui_docker_helper.config.authored.validation.domains import (
    _SECRET_NAME_PATTERN,
)
from comfyui_docker_helper.config.authored.validation.result import (
    FinalConfigDomainResult,
    LocatedValue,
    NormalizedRequirement,
)
from comfyui_docker_helper.config.credentials.downloader import (
    DownloaderCredentialContextError,
    parse_downloader_credential_context,
    parse_downloader_request_url,
    select_downloader_credential_context,
)
from comfyui_docker_helper.config.diagnostics import (
    Diagnostic,
    DiagnosticComparison,
    DiagnosticComparisonSite,
    DiagnosticPath,
    DiagnosticSeverity,
)
from comfyui_docker_helper.config.merge import OriginNode
from comfyui_docker_helper.config.registry_identity import (
    registry_distribution_identity,
    registry_resource_identity,
)
from comfyui_docker_helper.config.validation.os_packages import DEFAULT_OS_PACKAGES
from comfyui_docker_helper.exact_ledger import CUDA_PROTECTED_REQUIREMENTS


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
    _apt_package_diagnostics(
        domains.authored_apt_packages,
        diagnostics,
        origins=origins,
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
    _duplicate_diagnostics(
        domains.downloader_credential_contexts,
        "downloader_credential.duplicate_match",
        "credential match routes must be unique after normalization",
        diagnostics,
        origins=origins,
    )
    _secret_reference_diagnostics(config, diagnostics)
    _authenticated_downloader_diagnostics(config, diagnostics)
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
    for index, route in enumerate(config.cdh.downloader.credentials):
        name = route.token.secret
        if (
            _SECRET_NAME_PATTERN.fullmatch(name) is not None
            and name not in config.secrets
        ):
            diagnostics.append(
                Diagnostic(
                    (
                        "cdh",
                        "downloader",
                        "credentials",
                        index,
                        "token",
                        "secret",
                    ),
                    "secret.unknown_reference",
                    "must reference a defined Secret",
                )
            )


def _authenticated_downloader_diagnostics(
    config: FinalConfig,
    diagnostics: list[Diagnostic],
) -> None:
    routes = []
    for route in config.cdh.downloader.credentials:
        try:
            routes.append(parse_downloader_credential_context(route.match))
        except DownloaderCredentialContextError:
            continue
    if not routes:
        return
    for index, item in enumerate(config.files):
        if not isinstance(item, FinalHttpFileConfig):
            continue
        try:
            request = parse_downloader_request_url(item.url)
        except DownloaderCredentialContextError:
            continue
        if (
            select_downloader_credential_context(routes, request) is not None
            and (item.downloader or config.cdh.default_downloader) != "httpx"
        ):
            diagnostics.append(
                Diagnostic(
                    ("files", index, "downloader"),
                    "file.authenticated_downloader_requires_httpx",
                    "For credential safety, authenticated downloads require "
                    'downloader = "httpx"; aria2 cannot constrain credential '
                    "scope across redirects",
                    hint=(
                        'Set downloader = "httpx" for this file or change the '
                        "default downloader"
                    ),
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
) -> None:
    seen: dict[str, LocatedValue] = {}
    for item in values:
        value = item.value if normalize is None else normalize(item.value)
        if value in seen:
            established = seen[value]
            diagnostics.append(
                Diagnostic(
                    item.path,
                    code,
                    message,
                    source_context=_comparison(
                        established.path,
                        item.path,
                        origins,
                        earlier_value=established.value if display_values else None,
                        later_value=item.value if display_values else None,
                    ),
                )
            )
        else:
            seen[value] = item


def _apt_package_diagnostics(
    values: tuple[LocatedValue, ...],
    diagnostics: list[Diagnostic],
    *,
    origins: OriginNode | None,
) -> None:
    counts: dict[str, int] = {}
    for item in values:
        counts[item.value] = counts.get(item.value, 0) + 1

    established: dict[str, LocatedValue] = {}
    for item in values:
        earlier = established.get(item.value)
        if earlier is None:
            established[item.value] = item
            if counts[item.value] == 1 and item.value in DEFAULT_OS_PACKAGES:
                diagnostics.append(
                    Diagnostic(
                        item.path,
                        "system.redundant_default_apt_package",
                        f"{item.value} is already installed by cdh and is ignored",
                        DiagnosticSeverity.WARNING,
                    )
                )
            continue
        diagnostics.append(
            Diagnostic(
                item.path,
                "system.duplicate_apt_package",
                "package names must be unique",
                source_context=_comparison(
                    earlier.path,
                    item.path,
                    origins,
                    earlier_value=earlier.value,
                    later_value=item.value,
                ),
            )
        )


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
        if (
            requirement.path[:2] == ("pytorch", "extra_packages")
            and requirement.name in CUDA_PROTECTED_REQUIREMENTS
            and requirement.identity.direct_reference is not None
        ):
            diagnostics.append(
                Diagnostic(
                    requirement.path,
                    "pytorch.protected_requirement_conflict",
                    "protected PyTorch requirements must use the managed index source",
                )
            )
            continue
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
