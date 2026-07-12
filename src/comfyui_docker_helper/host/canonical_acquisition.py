"""Provider adapters and exact uv group resolution for canonical lock v1."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLockEntry,
    ComfyCliLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    DirectPythonLockEntry,
    DirectPythonRequestIdentity,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciLockEntry,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    RegistryNodeLockEntry,
    RegistryRequestIdentity,
    ResolverRequestIdentity,
)
from comfyui_docker_helper.config.canonical_resolver import (
    AcquiredCanonicalEntries,
    CanonicalAcquisitionError,
    ManagedPythonReleaseInputs,
    entries_satisfy_request,
    rebuild_canonical_entries,
)
from comfyui_docker_helper.host.identity_providers import (
    ComfyCliIdentity,
    ComfyCliIdentityProvider,
    DirectGitIdentityProvider,
    DirectGitIdentityRequest,
    IdentityProviderError,
    LocalExecutableIdentityProvider,
    LocalExecutableIdentityRequest,
    ManagedPythonIdentityProvider,
    ManagedPythonIdentityRequest,
    OciIdentityProvider,
    OciIdentityRequest,
    OfficialComfyUIIdentity,
    OfficialComfyUIIdentityProvider,
    OfficialComfyUIIdentityRequest,
    RegistryNodeIdentity,
    RegistryNodeIdentityProvider,
    RegistryNodeIdentityRequest,
)
from comfyui_docker_helper.host.uv_runner import HostUvRunner

_COMFYUI_FLOOR = Version("0.4.0")
_LINUX_AMD64_UV_PLATFORM = "x86_64-unknown-linux-gnu"


@dataclass(frozen=True, slots=True)
class ResolvedPythonMember:
    package: str
    version: str


class PythonGroupResolver(Protocol):
    def resolve(
        self, request: DirectPythonRequestIdentity
    ) -> tuple[ResolvedPythonMember, ...]: ...


type ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class UvPythonGroupResolver:
    """Resolve one complete group through the exact absolute cdh-owned uv."""

    uv: HostUvRunner
    runner: ProcessRunner = subprocess.run

    def resolve(
        self, request: DirectPythonRequestIdentity
    ) -> tuple[ResolvedPythonMember, ...]:
        requirements = "\n".join(
            _requirement_text(member.package, member.extras, member.selector)
            for member in request.members
        )
        argv = self.uv.argv(
            (
                "pip",
                "compile",
                "-",
                "--no-header",
                "--no-annotate",
                "--no-strip-extras",
                "--python-version",
                request.python_version,
                "--python-platform",
                _uv_platform(request.platform),
                "--default-index",
                request.index_url,
                "--resolution",
                "highest",
                "--prerelease",
                "disallow",
                "--no-sources",
                "--no-python-downloads",
                "--color",
                "never",
            )
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("UV_", "PIP_"))
        }
        environment.update({"UV_NO_CONFIG": "1", "UV_NO_PROGRESS": "1"})
        try:
            completed = self.runner(
                argv,
                input=f"{requirements}\n",
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd="/",
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise CanonicalAcquisitionError("Python group resolution failed") from error
        if completed.returncode != 0:
            raise CanonicalAcquisitionError("Python group resolution failed")
        return _parse_direct_members(completed.stdout, request)


@dataclass(frozen=True, slots=True)
class ProviderIdentityAcquirer:
    """Adapt typed T4 providers into minimal canonical lock entries."""

    oci: OciIdentityProvider
    managed_python: ManagedPythonIdentityProvider
    comfyui: OfficialComfyUIIdentityProvider
    comfy_cli: ComfyCliIdentityProvider
    registry: RegistryNodeIdentityProvider
    git: DirectGitIdentityProvider
    python_group: PythonGroupResolver
    release: ManagedPythonReleaseInputs

    def acquire(
        self, request: ResolverRequestIdentity, request_digest: str
    ) -> AcquiredCanonicalEntries:
        try:
            entries = rebuild_canonical_entries(self._acquire(request, request_digest))
        except IdentityProviderError as error:
            raise CanonicalAcquisitionError(str(error)) from error
        if not entries_satisfy_request(
            request,
            entries,
            request_digest,
            self.release if isinstance(request, ManagedPythonRequestIdentity) else None,
        ):
            raise CanonicalAcquisitionError(
                f"{request.type} provider returned incompatible data"
            )
        return AcquiredCanonicalEntries(entries, _uses_external_provider(request))

    def _acquire(
        self, request: ResolverRequestIdentity, request_digest: str
    ) -> tuple[CanonicalLockEntry, ...]:
        if isinstance(request, OciRequestIdentity):
            identity = self.oci.resolve(
                OciIdentityRequest(
                    request.role, request.repository, request.tag, request.platform
                )
            )
            _require_provider_match(
                (
                    identity.role == request.role
                    and identity.repository == request.repository
                    and identity.tag == request.tag
                    and identity.platform == request.platform
                ),
                "OCI",
            )
            return (
                OciLockEntry(
                    type="oci",
                    request_digest=request_digest,
                    role=identity.role,
                    repository=identity.repository,
                    tag=identity.tag,
                    descriptor_digest=identity.descriptor_digest,
                    descriptor_kind=identity.descriptor_kind,
                    platform=identity.platform,
                ),
            )
        if isinstance(request, ManagedPythonRequestIdentity):
            identity = self.managed_python.resolve(
                ManagedPythonIdentityRequest(
                    version=request.version,
                    catalog_descriptor_digest=request.catalog_descriptor_digest,
                    implementation=request.implementation,
                    platform=request.platform,
                    libc=request.libc,
                )
            )
            _require_provider_match(
                (
                    identity.version == request.version
                    and identity.implementation == request.implementation
                    and identity.platform == request.platform
                    and identity.libc == request.libc
                    and identity.catalog_descriptor_digest
                    == request.catalog_descriptor_digest
                ),
                "managed Python",
            )
            return (
                ManagedPythonLockEntry(
                    type="managed-python",
                    request_digest=request_digest,
                    version=identity.version,
                    implementation=identity.implementation,
                    platform=identity.platform,
                    libc=identity.libc,
                    provider=identity.provider,
                    catalog_descriptor_digest=identity.catalog_descriptor_digest,
                    catalog_key=identity.catalog_key,
                    catalog_url=identity.catalog_url,
                    pip_version=self.release.pip_version,
                    setuptools_version=self.release.setuptools_version,
                    wheel_version=self.release.wheel_version,
                    cdh_version=self.release.cdh_version,
                    cdh_source_digest=self.release.cdh_source_digest,
                    uv_build_version=self.release.uv_build_version,
                ),
            )
        if isinstance(request, ComfyUIRequestIdentity):
            identity = self._resolve_comfyui(request)
            _require_provider_match(
                identity.repository == request.repository, "official ComfyUI"
            )
            return (
                OfficialComfyUILockEntry(
                    type="comfyui",
                    request_digest=request_digest,
                    repository=identity.repository,
                    commit=identity.commit,
                    formal_release=identity.formal_release,
                ),
            )
        if isinstance(request, ComfyCliRequestIdentity):
            version = self._resolve_comfy_cli(request.selector)
            return (
                ComfyCliLockEntry(
                    type="comfy-cli",
                    request_digest=request_digest,
                    package="comfy-cli",
                    version=version,
                    environment="application",
                ),
            )
        if isinstance(request, RegistryRequestIdentity):
            identity = self._resolve_registry(request)
            _require_provider_match(identity.node_id == request.id, "Registry")
            return (
                RegistryNodeLockEntry(
                    type="registry",
                    request_digest=request_digest,
                    id=identity.node_id,
                    version=identity.version,
                ),
            )
        if isinstance(request, DirectGitRequestIdentity):
            commit = request.ref
            if not _is_commit(commit):
                identity = self.git.resolve(
                    DirectGitIdentityRequest(request.url, request.ref)
                )
                _require_provider_match(identity.url == request.url, "direct Git")
                commit = identity.commit
            return (
                DirectGitLockEntry(
                    type="git",
                    request_digest=request_digest,
                    url=request.url,
                    commit=commit,
                ),
            )
        resolved = self.python_group.resolve(request)
        by_package = {item.package: item.version for item in resolved}
        if set(by_package) != {member.package for member in request.members} or len(
            by_package
        ) != len(resolved):
            raise CanonicalAcquisitionError(
                "Python resolver returned an incompatible direct group"
            )
        return tuple(
            DirectPythonLockEntry(
                type="python-package",
                request_digest=request_digest,
                package=member.package,
                extras=member.extras,
                version=by_package[member.package],
                environment=request.environment,
            )
            for member in request.members
        )

    def _resolve_comfyui(
        self, request: ComfyUIRequestIdentity
    ) -> OfficialComfyUIIdentity:
        selector = request.selector
        if _is_commit(selector):
            return OfficialComfyUIIdentity(request.repository, selector, None)
        if selector == "nightly":
            return self.comfyui.resolve(
                OfficialComfyUIIdentityRequest(request.repository, "HEAD")
            )
        if selector[0].isdigit():
            return self.comfyui.resolve(
                OfficialComfyUIIdentityRequest(
                    request.repository, f"refs/tags/v{selector}"
                )
            )
        candidates = self.comfyui.list_releases(request.repository)
        selected = _select_comfyui_candidate(candidates, selector)
        return selected

    def _resolve_comfy_cli(self, selector: str) -> str:
        if _is_exact_selector(selector):
            return selector
        candidates = self.comfy_cli.list_versions()
        return _select_package_candidate(candidates, selector, "comfy-cli").version

    def _resolve_registry(
        self, request: RegistryRequestIdentity
    ) -> RegistryNodeIdentity:
        if _is_exact_selector(request.selector):
            return self.registry.resolve(
                RegistryNodeIdentityRequest(request.id, request.selector)
            )
        candidates = self.registry.list_versions(request.id)
        return _select_registry_candidate(candidates, request.selector, request.id)


@dataclass(frozen=True, slots=True)
class LocalExecutableEntryAcquirer:
    """Re-hash one current local executable without counting an external call."""

    provider: LocalExecutableIdentityProvider

    def acquire(
        self, request: LocalExecutableIdentityRequest
    ) -> LocalExecutableLockEntry:
        try:
            identity = self.provider.resolve(request)
        except IdentityProviderError as error:
            raise CanonicalAcquisitionError(str(error)) from error
        return LocalExecutableLockEntry(
            type="local-executable",
            relative_path=identity.relative_path.as_posix(),
            digest=identity.digest,
        )


def _select_comfyui_candidate(
    candidates: tuple[OfficialComfyUIIdentity, ...], selector: str
) -> OfficialComfyUIIdentity:
    compatible = [
        item
        for item in candidates
        if item.formal_release is not None
        and _stable_version(item.formal_release) is not None
        and Version(item.formal_release) >= _COMFYUI_FLOOR
        and _selector_matches(selector, Version(item.formal_release))
    ]
    if not compatible:
        raise CanonicalAcquisitionError("official ComfyUI identity was not found")
    return max(compatible, key=lambda item: Version(item.formal_release or "0"))


def _select_package_candidate(
    candidates: tuple[ComfyCliIdentity, ...], selector: str, source: str
) -> ComfyCliIdentity:
    compatible = [
        item
        for item in candidates
        if (version := _stable_version(item.version)) is not None
        and _selector_matches(selector, version)
    ]
    if not compatible:
        raise CanonicalAcquisitionError(f"{source} identity was not found")
    return max(compatible, key=lambda item: Version(item.version))


def _select_registry_candidate(
    candidates: tuple[RegistryNodeIdentity, ...], selector: str, node_id: str
) -> RegistryNodeIdentity:
    compatible = [
        item
        for item in candidates
        if item.node_id == node_id
        and (version := _stable_version(item.version)) is not None
        and _selector_matches(selector, version)
    ]
    if not compatible:
        raise CanonicalAcquisitionError("Registry identity was not found")
    return max(compatible, key=lambda item: Version(item.version))


def _parse_direct_members(
    output: str, request: DirectPythonRequestIdentity
) -> tuple[ResolvedPythonMember, ...]:
    requested = {member.package for member in request.members}
    matches: dict[str, str] = {}
    for line in output.splitlines():
        value = line.strip()
        if not value or value.startswith(("#", "--")):
            continue
        try:
            requirement = Requirement(value)
        except InvalidRequirement as error:
            raise CanonicalAcquisitionError(
                "Python resolver returned invalid data"
            ) from error
        package = canonicalize_name(requirement.name)
        if package not in requested:
            continue
        exact = [item for item in requirement.specifier if item.operator == "=="]
        if len(exact) != 1 or len(tuple(requirement.specifier)) != 1:
            raise CanonicalAcquisitionError("Python resolver returned invalid data")
        version = _stable_version(exact[0].version)
        if version is None or package in matches:
            raise CanonicalAcquisitionError("Python resolver returned invalid data")
        matches[package] = str(version)
    if set(matches) != requested:
        raise CanonicalAcquisitionError("Python resolver omitted a direct package")
    return tuple(
        ResolvedPythonMember(member.package, matches[member.package])
        for member in request.members
    )


def _requirement_text(package: str, extras: list[str], selector: str) -> str:
    rendered_extras = f"[{','.join(extras)}]" if extras else ""
    return f"{package}{rendered_extras}{selector}"


def _uv_platform(platform: str) -> str:
    if platform != "linux/amd64":
        raise ValueError("unsupported Python resolver platform")
    return _LINUX_AMD64_UV_PLATFORM


def _selector_matches(selector: str, version: Version) -> bool:
    return selector == "latest" or SpecifierSet(selector).contains(
        version, prereleases=False
    )


def _stable_version(value: str) -> Version | None:
    try:
        version = Version(value)
    except InvalidVersion:
        return None
    if version.is_prerelease or version.is_devrelease or version.local is not None:
        return None
    return version


def _is_exact_selector(selector: str) -> bool:
    return selector != "latest" and not any(
        character in selector for character in "<>=!,"
    )


def _is_commit(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )


def _require_provider_match(condition: bool, source: str) -> None:
    if not condition:
        raise CanonicalAcquisitionError(f"{source} provider returned incompatible data")


def _uses_external_provider(request: ResolverRequestIdentity) -> bool:
    if isinstance(request, (OciRequestIdentity, ManagedPythonRequestIdentity)):
        return True
    if isinstance(request, ComfyUIRequestIdentity):
        return not _is_commit(request.selector)
    if isinstance(request, ComfyCliRequestIdentity):
        return not _is_exact_selector(request.selector)
    if isinstance(request, RegistryRequestIdentity):
        return True
    if isinstance(request, DirectGitRequestIdentity):
        return not _is_commit(request.ref)
    return True
