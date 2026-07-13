"""Provider adapters and exact uv group resolution for canonical lock v1."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
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
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from comfyui_docker_helper.comfyui_requirements import (
    ComfyUIRequirementsError,
    parse_comfyui_requirements,
)
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLockEntry,
    ComfyCliLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    DirectPythonLockEntry,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    LocalExecutableLockEntry,
    ManagedPythonLockEntry,
    ManagedPythonRequestIdentity,
    OciLockEntry,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    ProtectedRequirementProjection,
    PythonGroupRequestIdentity,
    PyTorchCompatibilityLockEntry,
    PyTorchRequestIdentity,
    RegistryNodeLockEntry,
    RegistryRequestIdentity,
    ResolverRequestIdentity,
    pytorch_core_version_matches_channel,
    uv_image_version_matches_tag,
)
from comfyui_docker_helper.config.canonical_resolver import (
    AcquiredCanonicalEntries,
    CanonicalAcquisitionError,
    ManagedPythonReleaseInputs,
    entries_satisfy_request,
    rebuild_canonical_entries,
)
from comfyui_docker_helper.exact_ledger import (
    COMFYUI_FLOOR_COMMIT,
    COMFYUI_MINIMUM_VERSION,
)
from comfyui_docker_helper.host.identity_providers import (
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
from comfyui_docker_helper.pytorch_resolution import (
    pytorch_resolution_manifest_bytes,
)

_COMFYUI_FLOOR = Version(COMFYUI_MINIMUM_VERSION)
_LINUX_AMD64_UV_PLATFORM = "x86_64-unknown-linux-gnu"


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


type ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
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
class UvPythonGroupResolver:
    """Resolve one complete group through the exact absolute cdh-owned uv."""

    uv: HostUvRunner
    runner: ProcessRunner = subprocess.run
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
                members=[
                    DirectPythonRequestMember(
                        package=request.package,
                        extras=[],
                        selector=f">={request.minimum_version}",
                    )
                ],
            )
            return self.resolve(direct)
        if isinstance(request, PyTorchRequestIdentity):
            return self._resolve_pytorch(request)
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
        resolved = _parse_direct_members(completed.stdout, request)
        if isinstance(request, PyTorchRequestIdentity) and any(
            not pytorch_core_version_matches_channel(
                member.package, member.version, request.channel
            )
            for member in resolved
        ):
            raise CanonicalAcquisitionError(
                "Python resolver returned an incompatible PyTorch channel"
            )
        return ResolvedPythonGroup(resolved)

    def _resolve_pytorch(self, request: PyTorchRequestIdentity) -> ResolvedPythonGroup:
        manifest = pytorch_resolution_manifest_bytes(
            requirements=tuple(
                _requirement_text(member.package, member.extras, member.selector)
                for member in request.members
            ),
            direct_packages=tuple(member.package for member in request.members),
            python_version=request.python_version,
            python_index_url=request.python_index_url,
            pytorch_index_url=request.pytorch_index_url,
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("UV_", "PIP_"))
        }
        environment.update({"UV_NO_CONFIG": "1", "UV_NO_PROGRESS": "1"})
        with tempfile.TemporaryDirectory(prefix="cdh-pytorch-resolution-") as root:
            path = Path(root) / "pyproject.toml"
            path.write_bytes(manifest)
            argv = self.uv.argv(
                (
                    "pip",
                    "compile",
                    str(path),
                    "--format",
                    "pylock.toml",
                    "--no-header",
                    "--python-version",
                    request.python_version,
                    "--python-platform",
                    _uv_platform(request.platform),
                    "--resolution",
                    "highest",
                    "--prerelease",
                    "disallow",
                    "--no-python-downloads",
                    "--color",
                    "never",
                    "--project",
                    root,
                )
            )
            try:
                completed = self.runner(
                    argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=root,
                    env=environment,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise _pytorch_resolution_error(request, "resolution failed") from error
        if completed.returncode != 0:
            raise _pytorch_resolution_error(request, "resolution failed")
        try:
            document = tomllib.loads(completed.stdout)
            packages = document["packages"]
        except (KeyError, TypeError, tomllib.TOMLDecodeError) as error:
            raise _pytorch_resolution_error(
                request, "resolver returned invalid PyTorch metadata"
            ) from error
        direct = {member.package for member in request.members}
        if not isinstance(packages, list) or any(
            not isinstance(item, dict) for item in packages
        ):
            raise _pytorch_resolution_error(
                request, "resolver returned invalid PyTorch metadata"
            )
        selected = [item for item in packages if item.get("name") in direct]
        resolved_by_name = {item["name"]: item for item in selected}
        if len(selected) != len(resolved_by_name) or set(resolved_by_name) != direct:
            raise _pytorch_resolution_error(
                request, "resolver returned an incompatible direct group"
            )
        if any(
            not isinstance(item.get("version"), str)
            for item in resolved_by_name.values()
        ):
            raise _pytorch_resolution_error(
                request, "resolver returned invalid PyTorch metadata"
            )
        resolved = tuple(
            ResolvedPythonMember(
                member.package, resolved_by_name[member.package]["version"]
            )
            for member in request.members
        )
        if any(
            not pytorch_core_version_matches_channel(
                member.package, member.version, request.channel
            )
            for member in resolved
        ):
            raise _pytorch_resolution_error(
                request, "resolver returned an incompatible PyTorch channel"
            )
        torch = resolved_by_name["torch"]
        wheels = torch.get("wheels")
        if not isinstance(wheels, list) or len(wheels) != 1:
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
    release: ManagedPythonReleaseInputs
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
            if request.role == "uv-tool":
                _require_provider_match(
                    uv_image_version_matches_tag(
                        request.tag, identity.resolved_version
                    ),
                    "OCI uv version",
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
                    resolved_version=identity.resolved_version,
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
                    "official ComfyUI checkout is below the supported v0.11.0 floor"
                )
            content = self.requirements_reader(request)
            try:
                parsed = parse_comfyui_requirements(
                    content,
                    python_version=request.python_version,
                    platform=request.platform,
                    protected_names=tuple(request.protected_names),
                )
            except ComfyUIRequirementsError as error:
                raise CanonicalAcquisitionError(str(error)) from error
            return (
                ComfyUIRequirementsLockEntry(
                    type="comfyui-requirements",
                    request_digest=request_digest,
                    repository=request.repository,
                    commit=request.commit,
                    floor_commit=request.floor_commit,
                    path=request.path,
                    python_version=request.python_version,
                    platform=request.platform,
                    protected_names=request.protected_names,
                    protected_policy_digest=request.protected_policy_digest,
                    requirements_digest=parsed.digest,
                    protected=[
                        ProtectedRequirementProjection(
                            package=item.package,
                            extras=item.extras,
                            selector=item.selector,
                        )
                        for item in parsed.protected
                    ],
                ),
            )
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
                ComfyCliLockEntry(
                    type="comfy-cli",
                    request_digest=request_digest,
                    package="comfy-cli",
                    version=member.version,
                    environment=request.environment,
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
        entries: tuple[CanonicalLockEntry, ...] = tuple(
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
        if isinstance(request, PyTorchRequestIdentity):
            entries = (
                *entries,
                PyTorchCompatibilityLockEntry(
                    type="pytorch-compatibility",
                    request_digest=request_digest,
                    environment="application",
                    setuptools_specifier=resolution.setuptools_specifier,
                ),
            )
        return entries

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
    output: str, request: PythonGroupRequestIdentity
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
        version = _stable_direct_version(exact[0].version)
        if version is None or package in matches:
            raise CanonicalAcquisitionError("Python resolver returned invalid data")
        matches[package] = str(version)
    if set(matches) != requested:
        raise CanonicalAcquisitionError("Python resolver omitted a direct package")
    return tuple(
        ResolvedPythonMember(member.package, matches[member.package])
        for member in request.members
    )


def _setuptools_specifier_from_metadata(
    metadata: str,
    *,
    python_version: str,
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
        environment = _target_marker_environment(python_version)
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


def _target_marker_environment(python_version: str) -> dict[str, str]:
    return {
        "implementation_name": "cpython",
        "implementation_version": python_version,
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": "Linux",
        "platform_version": "",
        "python_full_version": python_version,
        "python_version": ".".join(python_version.split(".")[:2]),
        "sys_platform": "linux",
        "extra": "",
    }


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


def _requirement_text(package: str, extras: list[str], selector: str) -> str:
    rendered_extras = f"[{','.join(extras)}]" if extras else ""
    return f"{package}{rendered_extras}{selector}"


def _pytorch_resolution_error(
    request: PyTorchRequestIdentity, detail: str
) -> CanonicalAcquisitionError:
    packages = ",".join(member.package for member in request.members)
    return CanonicalAcquisitionError(
        f"PyTorch {detail} for packages [{packages}], channel {request.channel}, "
        f"Python {request.python_version}, platform {request.platform}, "
        f"source {request.pytorch_index_url}"
    )


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


def _stable_direct_version(value: str) -> Version | None:
    """Return one complete canonical stable distribution version."""
    try:
        version = Version(value)
    except InvalidVersion:
        return None
    if version.is_prerelease or version.is_devrelease:
        return None
    if str(version) != value:
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
