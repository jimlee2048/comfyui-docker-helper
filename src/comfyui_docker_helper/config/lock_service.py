"""Service-level lockfile orchestration policies."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from comfyui_docker_helper.config.diagnostics import Diagnostic
from comfyui_docker_helper.config.lock import (
    LOCKFILE_SCHEMA_VERSION,
    GitLockedCustomNode,
    LockDomainError,
    LockedComfyUI,
    Lockfile,
    LockManifest,
    RegistryLockedCustomNode,
    compute_lock_input_digest,
)
from comfyui_docker_helper.config.models import (
    Config,
    GitCustomNodeConfig,
    RegistryCustomNodeConfig,
)
from comfyui_docker_helper.config.resolvers import (
    ComfyCliPackageProvider,
    ComfyUIReleaseProvider,
    GitCustomNodeProvider,
    RegistryCustomNodeProvider,
    ResolverError,
    locked_comfy_cli_satisfies_selector,
    locked_comfyui_satisfies_selector,
    locked_git_custom_node_satisfies_selector,
    locked_registry_custom_node_satisfies_selector,
    resolve_comfy_cli,
    resolve_comfyui,
    resolve_git_custom_node,
    resolve_registry_custom_node,
)
from comfyui_docker_helper.config.validation import (
    normalize_comfy_cli_version,
    normalize_comfyui_version,
    normalize_registry_version,
)

_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}\Z")


@dataclass(frozen=True, slots=True)
class LockOptions:
    """Policy flags for service-level lock orchestration."""

    locked: bool = False
    check: bool = False
    upgrade_lock: bool = False
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class SourceResolvers:
    """Provider boundaries used by lock-domain source resolvers."""

    comfyui: ComfyUIReleaseProvider
    comfy_cli: ComfyCliPackageProvider
    registry: RegistryCustomNodeProvider
    git: GitCustomNodeProvider


@dataclass(frozen=True, slots=True)
class LockServiceResult:
    """The expected lockfile and policy diagnostics for a lock operation."""

    lockfile: Lockfile
    changed: bool
    warnings: tuple[Diagnostic, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


class LockServiceError(ValueError):
    """A fatal lock orchestration failure represented by public diagnostics."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("lock service errors require diagnostics")
        self.diagnostics = diagnostics
        super().__init__("lock orchestration failed")


def resolve_lockfile(
    config: Config,
    existing_lockfile: Lockfile | None,
    resolvers: SourceResolvers,
    options: LockOptions | None = None,
) -> LockServiceResult:
    """Return the effective lockfile for ``config`` without reading or writing files."""
    options = options or LockOptions()
    _validate_options(options)

    try:
        lock_input_digest = compute_lock_input_digest(config)
    except LockDomainError as error:
        raise LockServiceError(
            (
                Diagnostic(
                    path=("comfyui", "custom_nodes"),
                    code="lock.domain_ambiguous",
                    message=str(error),
                ),
            )
        ) from error

    if options.locked:
        return _resolve_locked(config, existing_lockfile, lock_input_digest)

    warnings: list[Diagnostic] = []
    comfyui = _resolve_comfyui_lock(config, existing_lockfile, resolvers, options)
    custom_nodes = _resolve_custom_nodes(
        config,
        existing_lockfile,
        resolvers,
        options,
        warnings,
        lock_input_digest,
    )
    lockfile = Lockfile(
        schema_version=LOCKFILE_SCHEMA_VERSION,
        manifest=LockManifest(lock_input_digest=lock_input_digest),
        comfyui=comfyui,
        custom_nodes=custom_nodes,
    )
    return LockServiceResult(
        lockfile=lockfile,
        changed=lockfile != existing_lockfile,
        warnings=tuple(warnings),
    )


def _resolve_locked(
    config: Config,
    existing_lockfile: Lockfile | None,
    lock_input_digest: str,
) -> LockServiceResult:
    diagnostics: list[Diagnostic] = []
    if existing_lockfile is None:
        raise LockServiceError(
            (
                Diagnostic(
                    path=("config.lock.toml",),
                    code="lockfile.required",
                    message="--locked requires an existing config.lock.toml",
                ),
            )
        )

    if existing_lockfile.manifest.lock_input_digest != lock_input_digest:
        diagnostics.append(
            Diagnostic(
                path=("config.lock.toml", "manifest", "lock_input_digest"),
                code="lockfile.digest_mismatch",
                message=(
                    "config.lock.toml was created for different lock inputs; "
                    "refresh the lockfile without --locked"
                ),
            )
        )

    diagnostics.extend(_locked_compatibility_diagnostics(config, existing_lockfile))
    if diagnostics:
        raise LockServiceError(tuple(diagnostics))

    return LockServiceResult(
        lockfile=existing_lockfile.model_copy(deep=True),
        changed=False,
    )


def _resolve_comfyui_lock(
    config: Config,
    existing_lockfile: Lockfile | None,
    resolvers: SourceResolvers,
    options: LockOptions,
) -> LockedComfyUI:
    existing = existing_lockfile.comfyui if existing_lockfile is not None else None
    comfyui_selector = config.comfyui.version
    cli_selector = config.comfyui.cli_version

    reuse_comfyui = (
        existing is not None
        and not _upgrade_comfyui(comfyui_selector, options)
        and locked_comfyui_satisfies_selector(existing, comfyui_selector)
    )
    reuse_cli = (
        existing is not None
        and not _upgrade_comfy_cli(cli_selector, options)
        and locked_comfy_cli_satisfies_selector(existing.cli_version, cli_selector)
    )

    if reuse_comfyui:
        repo = existing.repo
        commit = existing.commit
        version = existing.version
    else:
        resolved_comfyui = _resolve_or_raise(
            path=("comfyui", "version"),
            mode=_mode_name(options),
            resolver=lambda: resolve_comfyui(comfyui_selector, resolvers.comfyui),
        )
        repo = resolved_comfyui.repo
        commit = resolved_comfyui.commit
        version = resolved_comfyui.version

    if reuse_cli:
        cli_version = existing.cli_version
    else:
        resolved_cli = _resolve_or_raise(
            path=("comfyui", "cli_version"),
            mode=_mode_name(options),
            resolver=lambda: resolve_comfy_cli(cli_selector, resolvers.comfy_cli),
        )
        cli_version = resolved_cli.version

    return LockedComfyUI(
        repo=repo,
        commit=commit,
        version=version,
        cli_version=cli_version,
    )


def _resolve_custom_nodes(
    config: Config,
    existing_lockfile: Lockfile | None,
    resolvers: SourceResolvers,
    options: LockOptions,
    warnings: list[Diagnostic],
    lock_input_digest: str,
) -> list[RegistryLockedCustomNode | GitLockedCustomNode]:
    registry_entries = _registry_entries(existing_lockfile)
    git_entries = _git_entries(existing_lockfile)
    locked_nodes: list[RegistryLockedCustomNode | GitLockedCustomNode] = []

    for index, node in enumerate(config.comfyui.custom_nodes):
        if isinstance(node, RegistryCustomNodeConfig):
            existing = registry_entries.get(node.id)
            if (
                existing is not None
                and not _upgrade_registry(node.version, options)
                and locked_registry_custom_node_satisfies_selector(
                    existing,
                    node.id,
                    node.version,
                )
            ):
                locked_nodes.append(existing.model_copy(deep=True))
                continue

            resolved = _resolve_or_raise(
                path=("comfyui", "custom_nodes", index, "version"),
                mode=_mode_name(options),
                resolver=lambda node=node: resolve_registry_custom_node(
                    node.id,
                    node.version,
                    resolvers.registry,
                ),
            )
            warnings.extend(
                _diagnostics_at_path(
                    resolved.warnings,
                    ("comfyui", "custom_nodes", index, "version"),
                )
            )
            locked_nodes.append(resolved.to_locked())
        elif isinstance(node, GitCustomNodeConfig):
            existing = git_entries.get(node.url)
            if (
                existing is not None
                and not _upgrade_git(node.ref, options)
                and not _moving_git_ref_needs_resolution(
                    node.ref,
                    existing_lockfile,
                    lock_input_digest,
                )
                and locked_git_custom_node_satisfies_selector(
                    existing,
                    node.url,
                    node.ref,
                )
            ):
                locked_nodes.append(existing.model_copy(deep=True))
                continue

            resolved = _resolve_or_raise(
                path=("comfyui", "custom_nodes", index, "ref"),
                mode=_mode_name(options),
                resolver=lambda node=node: resolve_git_custom_node(
                    node.url,
                    node.ref,
                    resolvers.git,
                ),
            )
            locked_nodes.append(resolved.to_locked())

    return locked_nodes


def _locked_compatibility_diagnostics(
    config: Config,
    lockfile: Lockfile,
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    if not locked_comfyui_satisfies_selector(lockfile.comfyui, config.comfyui.version):
        diagnostics.append(
            Diagnostic(
                path=("comfyui", "version"),
                code="lockfile.comfyui_incompatible",
                message=(
                    "locked ComfyUI source is missing or does not satisfy "
                    f"selector {config.comfyui.version!r}"
                ),
            )
        )
    if not locked_comfy_cli_satisfies_selector(
        lockfile.comfyui.cli_version,
        config.comfyui.cli_version,
    ):
        diagnostics.append(
            Diagnostic(
                path=("comfyui", "cli_version"),
                code="lockfile.comfy_cli_incompatible",
                message=(
                    "locked comfy-cli version does not satisfy selector "
                    f"{config.comfyui.cli_version!r}"
                ),
            )
        )

    registry_entries = _registry_entries(lockfile)
    git_entries = _git_entries(lockfile)
    for index, node in enumerate(config.comfyui.custom_nodes):
        if isinstance(node, RegistryCustomNodeConfig):
            existing = registry_entries.get(node.id)
            if existing is None:
                diagnostics.append(
                    Diagnostic(
                        path=("comfyui", "custom_nodes", index, "id"),
                        code="lockfile.registry_missing",
                        message=(
                            "config.lock.toml is missing registry custom-node "
                            f"entry {node.id!r}"
                        ),
                    )
                )
            elif not locked_registry_custom_node_satisfies_selector(
                existing,
                node.id,
                node.version,
            ):
                diagnostics.append(
                    Diagnostic(
                        path=("comfyui", "custom_nodes", index, "version"),
                        code="lockfile.registry_incompatible",
                        message=(
                            "locked registry custom-node version does not satisfy "
                            f"selector {_registry_selector_label(node.version)!r}"
                        ),
                    )
                )
        elif isinstance(node, GitCustomNodeConfig):
            existing = git_entries.get(node.url)
            if existing is None:
                diagnostics.append(
                    Diagnostic(
                        path=("comfyui", "custom_nodes", index, "url"),
                        code="lockfile.git_missing",
                        message=(
                            "config.lock.toml is missing git custom-node entry "
                            f"{node.url!r}"
                        ),
                    )
                )
            elif not locked_git_custom_node_satisfies_selector(
                existing,
                node.url,
                node.ref,
            ):
                diagnostics.append(
                    Diagnostic(
                        path=("comfyui", "custom_nodes", index, "ref"),
                        code="lockfile.git_incompatible",
                        message=(
                            "locked git custom-node commit does not satisfy ref "
                            f"{_git_selector_label(node.ref)!r}"
                        ),
                    )
                )
    return tuple(diagnostics)


def _validate_options(options: LockOptions) -> None:
    diagnostics: list[Diagnostic] = []
    if options.locked and options.upgrade_lock:
        diagnostics.append(_invalid_option("--locked", "--upgrade-lock"))
    if options.check and options.upgrade_lock:
        diagnostics.append(_invalid_option("--check", "--upgrade-lock"))
    if options.check and options.dry_run:
        diagnostics.append(_invalid_option("--check", "--dry-run"))
    if diagnostics:
        raise LockServiceError(tuple(diagnostics))


def _invalid_option(first: str, second: str) -> Diagnostic:
    return Diagnostic(
        path=(),
        code="lock.options_incompatible",
        message=f"{first} cannot be combined with {second}",
    )


def _registry_entries(
    lockfile: Lockfile | None,
) -> dict[str, RegistryLockedCustomNode]:
    if lockfile is None:
        return {}
    return {
        node.id: node
        for node in lockfile.custom_nodes
        if isinstance(node, RegistryLockedCustomNode)
    }


def _git_entries(lockfile: Lockfile | None) -> dict[str, GitLockedCustomNode]:
    if lockfile is None:
        return {}
    return {
        node.url: node
        for node in lockfile.custom_nodes
        if isinstance(node, GitLockedCustomNode)
    }


def _upgrade_comfyui(selector: str, options: LockOptions) -> bool:
    if not options.upgrade_lock:
        return False
    selector = normalize_comfyui_version(selector)
    return selector in {"latest", "nightly"} or _looks_like_constraint(selector)


def _upgrade_comfy_cli(selector: str, options: LockOptions) -> bool:
    if not options.upgrade_lock:
        return False
    selector = normalize_comfy_cli_version(selector)
    return selector == "latest" or _looks_like_constraint(selector)


def _upgrade_registry(selector: str | None, options: LockOptions) -> bool:
    if not options.upgrade_lock:
        return False
    if selector is None:
        return True
    selector = normalize_registry_version(selector)
    return selector == "latest" or _looks_like_constraint(selector)


def _upgrade_git(ref: str | None, options: LockOptions) -> bool:
    if not options.upgrade_lock:
        return False
    return ref is None or not _is_commit(ref)


def _moving_git_ref_needs_resolution(
    ref: str | None,
    existing_lockfile: Lockfile | None,
    lock_input_digest: str,
) -> bool:
    if ref is not None and _is_commit(ref):
        return False
    return (
        existing_lockfile is not None
        and existing_lockfile.manifest.lock_input_digest != lock_input_digest
    )


def _diagnostics_at_path(
    diagnostics: tuple[Diagnostic, ...],
    path: tuple[str | int, ...],
) -> tuple[Diagnostic, ...]:
    return tuple(replace(diagnostic, path=path) for diagnostic in diagnostics)


def _looks_like_constraint(selector: str) -> bool:
    return selector.startswith(("==", "!=", "<=", ">=", "<", ">"))


def _is_commit(value: str) -> bool:
    return _COMMIT_PATTERN.fullmatch(value) is not None


def _registry_selector_label(selector: str | None) -> str:
    return "latest" if selector is None else selector


def _git_selector_label(ref: str | None) -> str:
    return "<default branch>" if ref is None else ref


def _mode_name(options: LockOptions) -> str:
    if options.upgrade_lock:
        return "upgrade"
    if options.check:
        return "check"
    if options.dry_run:
        return "dry-run"
    return "default"


def _resolve_or_raise(path, mode: str, resolver):
    try:
        return resolver()
    except ResolverError as error:
        raise LockServiceError(
            (
                Diagnostic(
                    path=path,
                    code="lock.resolve_failed",
                    message=(
                        f"{mode} lock resolution failed for {error.source} "
                        f"selector {error.selector!r}: {error.reason}"
                    ),
                ),
            )
        ) from error
