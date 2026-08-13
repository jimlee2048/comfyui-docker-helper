"""Provider adapters and exact uv group resolution for canonical lock v1."""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from email import policy
from email.parser import Parser
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.comfyui_requirements import target_marker_environment
from comfyui_docker_helper.config.canonical_lock import (
    ApplicationExtrasLockEntry,
    BuildHookLockEntry,
    CanonicalLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    CudaImageLockEntry,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    LocalExecutableLockEntry,
    LocalFileLockEntry,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    PythonGroupRequestIdentity,
    PyTorchLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    RegistryRequestIdentity,
    ResolvedPythonPackage,
    ResolverRequestIdentity,
    RuntimeHookLockEntry,
    UvImageLockEntry,
    UvToolLockEntry,
    pytorch_core_version_matches_channel,
    uv_image_version_matches_tag,
    validate_exact_distribution_version,
)
from comfyui_docker_helper.config.canonical_resolver import (
    AcquiredCanonicalEntries,
    CanonicalAcquisitionError,
    entries_satisfy_request,
    rebuild_canonical_entries,
)
from comfyui_docker_helper.exact_ledger import (
    COMFYUI_FLOOR_COMMIT,
    COMFYUI_MINIMUM_VERSION,
)
from comfyui_docker_helper.file_admission import consume_regular_absolute_file
from comfyui_docker_helper.host.identity_providers import (
    DirectGitIdentityProvider,
    DirectGitIdentityRequest,
    IdentityProviderError,
    LocalExecutableIdentityProvider,
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
from comfyui_docker_helper.host.uv_docker_executor import (
    PyTorchCompileOperation,
    RequirementsCompileOperation,
    UvDockerExecutor,
    UvDockerExecutorError,
    UvResolverDescriptor,
)
from comfyui_docker_helper.local_executable import LocalExecutableIdentityRequest
from comfyui_docker_helper.local_file_identity import LocalFileIdentityRequest
from comfyui_docker_helper.pytorch_resolution import (
    pytorch_resolution_manifest_bytes,
)

_COMFYUI_FLOOR = Version(COMFYUI_MINIMUM_VERSION)


@dataclass(frozen=True, slots=True)
class ResolvedPythonMember:
    package: str
    version: str


@dataclass(frozen=True, slots=True)
class ResolvedPythonGroup:
    members: tuple[ResolvedPythonMember, ...]
    setuptools_specifier: str | None = None


class PythonGroupResolver(Protocol):
    def resolve(
        self, request: PythonGroupRequestIdentity | ComfyCliRequestIdentity
    ) -> ResolvedPythonGroup: ...


type MetadataReader = Callable[[str], str]
type RequirementsReader = Callable[[ComfyUIRequirementsRequestIdentity], bytes]


def _read_metadata_sidecar(url: str) -> str:
    try:
        response = httpx.get(url, follow_redirects=True, timeout=30.0)
        response.raise_for_status()
        return response.text
    except (httpx.HTTPError, UnicodeError) as error:
        raise CanonicalAcquisitionError(
            "PyTorch wheel metadata acquisition failed"
        ) from error


def _read_comfyui_requirements(
    request: ComfyUIRequirementsRequestIdentity,
) -> bytes:
    url = _comfyui_requirements_url(request)
    try:
        response = httpx.get(url, follow_redirects=True, timeout=30.0)
        response.raise_for_status()
        return response.content
    except httpx.HTTPError as error:
        raise CanonicalAcquisitionError(
            "exact ComfyUI requirements acquisition failed"
        ) from error


@dataclass(frozen=True, slots=True)
class DockerPythonGroupResolver:
    """Resolve one complete group through its exact uv OCI descriptor."""

    executor: UvDockerExecutor | None = None
    metadata_reader: MetadataReader = _read_metadata_sidecar

    def resolve(
        self, request: PythonGroupRequestIdentity | ComfyCliRequestIdentity
    ) -> ResolvedPythonGroup:
        if isinstance(request, ComfyCliRequestIdentity):
            direct = DirectPythonRequestIdentity(
                type="python-group",
                environment=request.environment,
                group="uv-tool",
                python_version=request.python_version,
                platform=request.platform,
                index_url=request.index_url,
                resolver_descriptor_digest=request.resolver_descriptor_digest,
                members=[
                    DirectPythonRequestMember(
                        package=request.package,
                        extras=[],
                        specifier=f">={request.minimum_version}",
                    )
                ],
            )
            return self.resolve(direct)
        if isinstance(request, PyTorchRequestIdentity):
            return self._resolve_pytorch(request)
        requirements = "\n".join(
            member.resolver_requirement for member in request.members
        )
        try:
            result = (self.executor or UvDockerExecutor()).execute(
                UvResolverDescriptor(
                    request.resolver_descriptor_digest, request.platform
                ),
                RequirementsCompileOperation(
                    request.python_version,
                    request.index_url,
                    f"{requirements}\n".encode(),
                ),
            )
        except UvDockerExecutorError as error:
            raise CanonicalAcquisitionError(
                f"Python group resolution failed: {error}"
            ) from error
        try:
            resolved, _packages = _parse_pylock_members(
                result.stdout,
                tuple(member.package for member in request.members),
            )
        except ValueError as error:
            raise CanonicalAcquisitionError(
                "Python resolver returned invalid package metadata"
            ) from error
        return ResolvedPythonGroup(resolved)

    def _resolve_pytorch(self, request: PyTorchRequestIdentity) -> ResolvedPythonGroup:
        manifest = pytorch_resolution_manifest_bytes(
            requirements=tuple(
                member.resolver_requirement for member in request.members
            ),
            pytorch_index_packages=tuple(
                member.package
                for member in request.members
                if member.direct_reference is None
            ),
            python_version=request.python_version,
            python_index_url=request.python_index_url,
            pytorch_index_url=request.pytorch_index_url,
        )
        try:
            result = (self.executor or UvDockerExecutor()).execute(
                UvResolverDescriptor(
                    request.resolver_descriptor_digest, request.platform
                ),
                PyTorchCompileOperation(request.python_version, manifest),
            )
        except UvDockerExecutorError as error:
            raise _pytorch_resolution_error(
                request, f"resolution failed: {error}"
            ) from error
        try:
            resolved, resolved_by_name = _parse_pylock_members(
                result.stdout,
                tuple(member.package for member in request.members),
            )
        except ValueError as error:
            raise _pytorch_resolution_error(
                request, "resolver returned invalid PyTorch metadata"
            ) from error
        index_owned = {
            member.package
            for member in request.members
            if member.direct_reference is None
        }
        if any(
            member.package in index_owned
            and not pytorch_core_version_matches_channel(
                member.package, member.version, request.channel
            )
            for member in resolved
        ):
            raise _pytorch_resolution_error(
                request, "resolver returned an incompatible PyTorch channel"
            )
        torch = resolved_by_name["torch"]
        wheels = torch.get("wheels")
        if (
            not isinstance(wheels, list)
            or len(wheels) != 1
            or not isinstance(wheels[0], dict)
        ):
            raise _pytorch_resolution_error(
                request, "resolver did not select one exact torch wheel"
            )
        wheel_url = wheels[0].get("url")
        if not isinstance(wheel_url, str) or not wheel_url.startswith(
            ("http://", "https://")
        ):
            raise _pytorch_resolution_error(
                request, "resolver returned an invalid torch wheel URL"
            )
        metadata = self.metadata_reader(_metadata_sidecar_url(wheel_url))
        specifier = _setuptools_specifier_from_metadata(
            metadata,
            python_version=request.python_version,
            platform=request.platform,
            machine="x86_64",
            expected_torch_version=resolved_by_name["torch"]["version"],
        )
        return ResolvedPythonGroup(resolved, specifier)


@dataclass(frozen=True, slots=True)
class ProviderIdentityAcquirer:
    """Adapt typed identity providers into minimal canonical lock entries."""

    oci: OciIdentityProvider
    managed_python: ManagedPythonIdentityProvider
    comfyui: OfficialComfyUIIdentityProvider
    registry: RegistryNodeIdentityProvider
    git: DirectGitIdentityProvider
    python_group: PythonGroupResolver
    requirements_reader: RequirementsReader = _read_comfyui_requirements

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
            if request.role == "uv-tool":
                _require_provider_match(
                    uv_image_version_matches_tag(
                        request.tag, identity.resolved_version
                    ),
                    "OCI uv version",
                )
            entry_type = (
                CudaImageLockEntry if request.role == "cuda-base" else UvImageLockEntry
            )
            values = {
                "request_digest": request_digest,
                "repository": identity.repository,
                "tag": identity.tag,
                "digest": identity.descriptor_digest,
                "kind": identity.descriptor_kind,
                "platform": identity.platform,
            }
            if entry_type is UvImageLockEntry:
                values["observed_version"] = identity.resolved_version
            return (entry_type.model_validate(values),)
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
                    request_digest=request_digest,
                    version=identity.version,
                    platform=identity.platform,
                    libc=identity.libc,
                    catalog_digest=identity.catalog_descriptor_digest,
                    artifact_key=identity.catalog_key,
                    artifact_url=identity.catalog_url,
                ),
            )
        if isinstance(request, ComfyUIRequestIdentity):
            identity = self._resolve_comfyui(request)
            _require_provider_match(
                identity.repository == request.repository, "official ComfyUI"
            )
            return (
                OfficialComfyUILockEntry(
                    request_digest=request_digest,
                    repository=identity.repository,
                    commit=identity.commit,
                    formal_release=identity.formal_release,
                ),
            )
        if isinstance(request, ComfyUIRequirementsRequestIdentity):
            try:
                supported = self.comfyui.is_ancestor(
                    request.repository,
                    request.floor_commit,
                    request.commit,
                )
            except IdentityProviderError as error:
                raise CanonicalAcquisitionError(str(error)) from error
            if request.floor_commit != COMFYUI_FLOOR_COMMIT or not supported:
                raise CanonicalAcquisitionError(
                    "official ComfyUI checkout is below the supported "
                    f"v{COMFYUI_MINIMUM_VERSION} floor"
                )
            content = self.requirements_reader(request)
            if not isinstance(content, bytes):
                raise CanonicalAcquisitionError(
                    "exact ComfyUI requirements provider returned invalid content"
                )
            try:
                document = content.decode("utf-8")
                entry = ComfyUIRequirementsLockEntry(
                    request_digest=request_digest,
                    digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
                    content=document,
                )
            except (UnicodeDecodeError, ValueError) as error:
                raise CanonicalAcquisitionError(str(error)) from error
            return (entry,)
        if isinstance(request, ComfyCliRequestIdentity):
            resolution = self.python_group.resolve(request)
            if len(resolution.members) != 1:
                raise CanonicalAcquisitionError(
                    "Python resolver returned an incompatible comfy-cli result"
                )
            member = resolution.members[0]
            if member.package != request.package:
                raise CanonicalAcquisitionError(
                    "Python resolver returned an incompatible comfy-cli result"
                )
            return (
                UvToolLockEntry(
                    request_digest=request_digest,
                    name="comfy-cli",
                    extras=(),
                    version=member.version,
                ),
            )
        if isinstance(request, RegistryRequestIdentity):
            identity = self._resolve_registry(request)
            _require_provider_match(identity.node_id == request.id, "Registry")
            return (
                RegistryNodeLockEntry(
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
                    request_digest=request_digest,
                    url=request.url,
                    commit=commit,
                ),
            )
        resolution = self.python_group.resolve(request)
        resolved = resolution.members
        by_package = {item.package: item.version for item in resolved}
        if set(by_package) != {member.package for member in request.members} or len(
            by_package
        ) != len(resolved):
            raise CanonicalAcquisitionError(
                "Python resolver returned an incompatible direct group"
            )
        if isinstance(request, PyTorchRequestIdentity) and any(
            not pytorch_core_version_matches_channel(
                member.package, by_package[member.package], request.channel
            )
            for member in request.members
        ):
            raise CanonicalAcquisitionError(
                "Python resolver returned an incompatible PyTorch channel"
            )
        packages = tuple(
            ResolvedPythonPackage(
                name=member.package,
                extras=member.extras,
                version=by_package[member.package],
            )
            for member in request.members
        )
        if isinstance(request, PyTorchRequestIdentity):
            return (
                PyTorchLockEntry(
                    request_digest=request_digest,
                    packages=packages,
                    setuptools_specifier=resolution.setuptools_specifier,
                ),
            )
        if request.group == "application-extra":
            return (
                ApplicationExtrasLockEntry(
                    request_digest=request_digest,
                    packages=packages,
                ),
            )
        package = packages[0]
        return (
            UvToolLockEntry(
                request_digest=request_digest,
                name=package.name,
                extras=package.extras,
                version=package.version,
            ),
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
        path = identity.relative_path
        if path.parts[0] == "build-hooks":
            entry_type = BuildHookLockEntry
        elif path.parts[0] == "runtime-hooks":
            entry_type = RuntimeHookLockEntry
        else:
            raise CanonicalAcquisitionError(
                "local executable has no supported hook domain"
            )
        return entry_type(
            relative_path=Path(*path.parts[1:]).as_posix(),
            digest=identity.digest,
        )


@dataclass(frozen=True, slots=True)
class LocalFileEntryAcquirer:
    """Stream-hash one current host-local build file for lock reconciliation."""

    def acquire(self, request: LocalFileIdentityRequest) -> LocalFileLockEntry:
        digest = hashlib.sha256()
        try:
            consume_regular_absolute_file(request.source_path, digest.update)
        except (OSError, ValueError) as error:
            raise CanonicalAcquisitionError("local file could not be read") from error
        return LocalFileLockEntry(
            relative_target=request.relative_target.as_posix(),
            digest=f"sha256:{digest.hexdigest()}",
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


def _parse_pylock_members(
    output: bytes,
    requested: tuple[str, ...],
) -> tuple[tuple[ResolvedPythonMember, ...], dict[str, dict[str, object]]]:
    """Consume only exact requested distribution versions from one uv pylock."""
    try:
        document = tomllib.loads(output.decode("utf-8"))
        packages = document["packages"]
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("resolver output is not one pylock document") from error
    if not isinstance(packages, list) or any(
        not isinstance(item, dict) for item in packages
    ):
        raise ValueError("pylock packages must be one table array")
    requested_names = set(requested)
    matched: dict[str, dict[str, object]] = {}
    versions: dict[str, str] = {}
    for item in packages:
        raw_name = item.get("name")
        if not isinstance(raw_name, str):
            continue
        try:
            name = canonicalize_name(raw_name, validate=True)
        except InvalidName:
            continue
        if name not in requested_names:
            continue
        version = item.get("version")
        if name in matched or not isinstance(version, str):
            raise ValueError("requested package metadata is ambiguous")
        validate_exact_distribution_version(version)
        matched[name] = item
        versions[name] = version
    if set(matched) != requested_names:
        raise ValueError("pylock omitted a requested package")
    return (
        tuple(ResolvedPythonMember(name, versions[name]) for name in requested),
        matched,
    )


def _setuptools_specifier_from_metadata(
    metadata: str,
    *,
    python_version: str,
    platform: str,
    machine: str,
    expected_torch_version: str,
) -> str | None:
    try:
        message = Parser(policy=policy.default).parsestr(metadata)
        names = message.get_all("Name", [])
        versions = message.get_all("Version", [])
        metadata_versions = message.get_all("Metadata-Version", [])
        if message.defects or len(names) != 1 or len(versions) != 1:
            raise ValueError
        if (
            len(metadata_versions) != 1
            or re.fullmatch(r"[0-9]+\.[0-9]+", metadata_versions[0]) is None
            or canonicalize_name(names[0]) != "torch"
        ):
            raise ValueError
        if versions[0] != expected_torch_version:
            raise ValueError
        environment = target_marker_environment(python_version, platform, machine)
        specifiers = []
        for value in message.get_all("Requires-Dist", []):
            requirement = Requirement(value)
            if canonicalize_name(requirement.name) != "setuptools":
                continue
            if requirement.url is not None or requirement.extras:
                raise ValueError
            if requirement.marker is not None and not requirement.marker.evaluate(
                environment
            ):
                continue
            specifiers.extend(str(item) for item in requirement.specifier)
        if not specifiers:
            return None
        return str(SpecifierSet(",".join(specifiers)))
    except (InvalidRequirement, KeyError, TypeError, ValueError) as error:
        raise CanonicalAcquisitionError("PyTorch wheel metadata is invalid") from error


def _metadata_sidecar_url(wheel_url: str) -> str:
    parsed = urlsplit(wheel_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CanonicalAcquisitionError(
            "Python resolver returned an invalid torch wheel URL"
        )
    return urlunsplit(parsed._replace(path=f"{parsed.path}.metadata", fragment=""))


def _comfyui_requirements_url(
    request: ComfyUIRequirementsRequestIdentity,
) -> str:
    parsed = urlsplit(request.repository)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise CanonicalAcquisitionError(
            "official ComfyUI repository cannot provide immutable requirements"
        )
    repository_path = parsed.path.removesuffix(".git").strip("/")
    parts = repository_path.split("/")
    if len(parts) != 2:
        raise CanonicalAcquisitionError(
            "official ComfyUI repository cannot provide immutable requirements"
        )
    return (
        f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/"
        f"{request.commit}/{request.path}"
    )


def _pytorch_resolution_error(
    request: PyTorchRequestIdentity, detail: str
) -> CanonicalAcquisitionError:
    packages = ",".join(member.package for member in request.members)
    return CanonicalAcquisitionError(
        f"PyTorch {detail} for packages [{packages}], channel {request.channel}, "
        f"Python {request.python_version}, platform {request.platform}, "
        f"source {request.pytorch_index_url}"
    )


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
        return True
    if isinstance(request, RegistryRequestIdentity):
        return True
    if isinstance(request, DirectGitRequestIdentity):
        return not _is_commit(request.ref)
    return True
