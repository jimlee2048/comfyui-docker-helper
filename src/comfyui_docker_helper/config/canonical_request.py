"""Pure immutable request graph shared by reconciliation and BuildPlan projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from comfyui_docker_helper.comfyui_requirements import (
    COMFYUI_REQUIREMENTS_PATH,
    ComfyUIRequirementsError,
    merge_pytorch_requirements,
    protected_policy_digest,
)
from comfyui_docker_helper.config.canonical_lock import (
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    DirectGitRequestIdentity,
    DirectPythonRequestIdentity,
    DirectPythonRequestMember,
    ManagedPythonRequestIdentity,
    OciRequestIdentity,
    OfficialComfyUILockEntry,
    ProtectedRequirementProjection,
    PyTorchRequestIdentity,
    RegistryRequestIdentity,
    ResolverRequestIdentity,
    compute_request_digest,
)
from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticError
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_planning import (
    BackendPlan,
    CudaBackendAdapter,
    CudaVersion,
    TargetPlatform,
)
from comfyui_docker_helper.config.final_validation import (
    FinalConfigDomainResult,
    validate_final_config_domains,
)
from comfyui_docker_helper.config.os_packages import DEFAULT_OS_PACKAGES
from comfyui_docker_helper.config.selector_validation import resolve_git_target_dir
from comfyui_docker_helper.exact_ledger import (
    COMFY_CLI_MINIMUM_VERSION,
    COMFYUI_FLOOR_COMMIT,
    COMFYUI_REPOSITORY,
    UV_IMAGE_REPOSITORY,
)

type LockEntryKey = tuple[str, ...]

_VENV_PATH = "/opt/venv"


class CanonicalRequestError(DiagnosticError):
    """Expected canonical-request derivation failure."""


class SelectorStability(StrEnum):
    """Whether upgrade reconciliation refreshes one request."""

    EXACT = "exact"
    MOVING = "moving"


@dataclass(frozen=True, slots=True)
class ManagedPythonReleaseInputs:
    """Exact release-owned inputs that constrain managed Python reuse."""

    pip_version: str
    cdh_version: str
    cdh_source_digest: str
    uv_build_version: str


@dataclass(frozen=True, slots=True)
class PlanningReleaseInputs:
    """Narrow release artifacts needed by request and toolchain projection."""

    managed_python: ManagedPythonReleaseInputs
    requirements_digest: str
    cdh_closure: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class DesiredResolution:
    """One immutable provider acquisition unit."""

    request: ResolverRequestIdentity
    managed_python_release: ManagedPythonReleaseInputs | None = None
    keys: tuple[LockEntryKey, ...] = field(init=False)
    request_digest: str = field(init=False)
    stability: SelectorStability = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.request, ManagedPythonRequestIdentity):
            if self.managed_python_release is None:
                raise ValueError("managed Python requires current release-owned inputs")
        elif self.managed_python_release is not None:
            raise ValueError("release-owned inputs apply only to managed Python")
        object.__setattr__(self, "keys", request_keys(self.request))
        object.__setattr__(self, "request_digest", compute_request_digest(self.request))
        object.__setattr__(self, "stability", request_stability(self.request))


@dataclass(frozen=True, slots=True)
class BuildRequest:
    tags: tuple[str, ...]
    output: Literal["load", "push"]
    platforms: tuple[Literal["linux/amd64"], ...]


@dataclass(frozen=True, slots=True)
class SshRequest:
    enable: bool
    port: int
    password: str
    pub_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DownloaderRequest:
    default: Literal["aria2", "httpx"]
    default_download_mode: Literal["sync", "async"]
    download_max_attempts: int
    download_failure_policy: Literal["continue", "fail"]
    aria2_rpc_port: int
    aria2_split: int
    aria2_max_connection_per_server: int
    aria2_min_split_size: str
    aria2_resume_download: bool
    httpx_timeout: int | float


@dataclass(frozen=True, slots=True)
class FileRequest:
    url: str
    target: str
    overwrite: bool
    checksum: str | None
    downloader: Literal["aria2", "httpx"]
    download_mode: Literal["sync", "async"]


@dataclass(frozen=True, slots=True)
class RegistryNodeRequest:
    type: Literal["registry"]
    id: str
    selector: str
    pre_install: tuple[str, ...]
    post_install: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitNodeRequest:
    type: Literal["git"]
    url: str
    ref: str
    target: str
    pre_install: tuple[str, ...]
    post_install: tuple[str, ...]


type CustomNodeRequest = RegistryNodeRequest | GitNodeRequest


@dataclass(frozen=True, slots=True)
class ApplicationRequest:
    workspace: str
    comfyui_path: str
    os_packages: tuple[str, ...]
    python_index_url: str
    install_manager: bool


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    environment: tuple[tuple[str, str], ...]
    ssh: SshRequest
    shutdown_timeout: int | float
    launch_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CanonicalRequestGraph:
    """One immutable source for reconciliation and phase projection."""

    config_digest: str
    target_platform: TargetPlatform
    backend: BackendPlan
    release: PlanningReleaseInputs
    desired: tuple[DesiredResolution, ...]
    build: BuildRequest
    application: ApplicationRequest
    custom_nodes: tuple[CustomNodeRequest, ...]
    downloader: DownloaderRequest
    files: tuple[FileRequest, ...]
    runtime: RuntimeRequest


def uv_oci_request(config: FinalConfig) -> OciRequestIdentity:
    """Build the canonical uv image request shared by staging and reconciliation."""
    return OciRequestIdentity(
        type="oci",
        role="uv-tool",
        repository=UV_IMAGE_REPOSITORY,
        tag=config.python.uv_version,
        platform=TargetPlatform(config.build.platforms[0]).value,
    )


def comfyui_request(config: FinalConfig) -> ComfyUIRequestIdentity:
    """Build the canonical ComfyUI request shared by staging and reconciliation."""
    return ComfyUIRequestIdentity(
        type="comfyui",
        repository=COMFYUI_REPOSITORY,
        selector=config.comfyui.version,
    )


def build_canonical_request_graph(
    config: FinalConfig,
    *,
    release: PlanningReleaseInputs,
    uv_descriptor_digest: str,
    comfyui_entry: OfficialComfyUILockEntry,
    requirements_entry: ComfyUIRequirementsLockEntry,
) -> CanonicalRequestGraph:
    """Project validated config and accepted staged identities exactly once."""
    domains = validate_final_config_domains(config)
    platform = TargetPlatform(config.build.platforms[0])
    backend = CudaBackendAdapter().derive(
        CudaVersion.from_validated(config.compute_platform.cuda.version),
        image_flavor=config.compute_platform.cuda.image_flavor,
        image_distro=config.compute_platform.cuda.image_distro,
    )
    repository, tag = backend.base_image.split(":", 1)
    requirements_request = comfyui_requirements_request(config, comfyui_entry)
    if (
        requirements_entry.request_digest
        != compute_request_digest(requirements_request)
        or requirements_entry.repository != requirements_request.repository
        or requirements_entry.commit != requirements_request.commit
        or requirements_entry.floor_commit != requirements_request.floor_commit
        or requirements_entry.path != requirements_request.path
        or requirements_entry.python_version != requirements_request.python_version
        or requirements_entry.platform != requirements_request.platform
        or requirements_entry.protected_names != requirements_request.protected_names
        or requirements_entry.protected_policy_digest
        != requirements_request.protected_policy_digest
    ):
        raise ValueError("ComfyUI requirements identity does not match final config")

    requests: list[ResolverRequestIdentity] = [
        OciRequestIdentity(
            type="oci",
            role="cuda-base",
            repository=repository,
            tag=tag,
            platform=platform.value,
        ),
        uv_oci_request(config),
        ManagedPythonRequestIdentity(
            type="managed-python",
            version=config.python.version,
            implementation="cpython",
            platform=platform.value,
            libc="gnu",
            catalog_descriptor_digest=uv_descriptor_digest,
        ),
        comfyui_request(config),
        requirements_request,
    ]
    if config.comfyui.install_cli:
        requests.append(
            ComfyCliRequestIdentity(
                type="comfy-cli",
                package="comfy-cli",
                policy="highest-target-compatible-stable",
                minimum_version=COMFY_CLI_MINIMUM_VERSION,
                environment="uv-tool:comfy-cli",
                index_url=config.python.index_url,
                python_version=config.python.version,
                platform=platform.value,
            )
        )
    python_members = _members(domains, "python")
    if python_members:
        requests.append(
            DirectPythonRequestIdentity(
                type="python-group",
                environment="application",
                group="application-extra",
                python_version=config.python.version,
                platform=platform.value,
                index_url=config.python.index_url,
                members=python_members,
            )
        )
    for member in _members(domains, "python", field="uv_tools"):
        requests.append(
            DirectPythonRequestIdentity(
                type="python-group",
                environment=f"uv-tool:{member.package}",
                group="uv-tool",
                python_version=config.python.version,
                platform=platform.value,
                index_url=config.python.index_url,
                members=(member,),
            )
        )
    upstream = tuple(
        DirectPythonRequestMember(
            package=item.package,
            extras=item.extras,
            selector=item.selector,
        )
        for item in requirements_entry.protected
    )
    try:
        pytorch_members = merge_pytorch_requirements(
            DirectPythonRequestMember(
                package="torch", extras=(), selector=f"=={config.pytorch.version}"
            ),
            upstream,
            tuple(_members(domains, "pytorch")),
        )
    except ComfyUIRequirementsError as error:
        raise CanonicalRequestError(
            (
                Diagnostic(
                    path=("pytorch", "extra_packages"),
                    code="pytorch.protected_requirement_conflict",
                    message="protected PyTorch requirements conflict",
                ),
            )
        ) from error
    python_owner_names = {member.package for member in python_members}
    protected_owner_names = {member.package for member in pytorch_members}
    overlap = python_owner_names.intersection(
        {*protected_owner_names, "pip", "setuptools"}
    )
    if overlap:
        raise ValueError(
            "application Python extras overlap protected package owners: "
            f"{sorted(overlap)!r}"
        )
    requests.append(
        PyTorchRequestIdentity(
            type="pytorch-group",
            environment="application",
            group="pytorch",
            backend="cuda",
            channel=backend.package_channel,
            python_version=config.python.version,
            platform=platform.value,
            python_index_url=config.python.index_url,
            pytorch_index_url=(
                f"{config.pytorch.index_base_url.rstrip('/')}/{backend.package_channel}"
            ),
            upstream_protected=tuple(
                ProtectedRequirementProjection(
                    package=item.package,
                    extras=item.extras,
                    selector=item.selector,
                )
                for item in upstream
            ),
            members=tuple(pytorch_members),
        )
    )

    workspace = str(PurePosixPath(config.system.workspace))
    comfyui_path = str(
        PurePosixPath(config.system.comfyui_path)
        if config.system.comfyui_path is not None
        else PurePosixPath(workspace) / "ComfyUI"
    )
    nodes: list[CustomNodeRequest] = []
    for node in config.comfyui.custom_nodes:
        pre = tuple(node.pre_install_scripts)
        post = tuple(node.post_install_scripts)
        if node.type == "registry":
            selector = node.version or "latest"
            requests.append(
                RegistryRequestIdentity(type="registry", id=node.id, selector=selector)
            )
            nodes.append(RegistryNodeRequest("registry", node.id, selector, pre, post))
        else:
            ref = node.ref or "HEAD"
            requests.append(DirectGitRequestIdentity(type="git", url=node.url, ref=ref))
            target = str(
                PurePosixPath(comfyui_path)
                / "custom_nodes"
                / resolve_git_target_dir(node.url, node.target_dir)
            )
            nodes.append(GitNodeRequest("git", node.url, ref, target, pre, post))

    desired = tuple(
        DesiredResolution(
            request,
            managed_python_release=(
                release.managed_python
                if isinstance(request, ManagedPythonRequestIdentity)
                else None
            ),
        )
        for request in requests
    )
    downloader = DownloaderRequest(
        default=config.cdh.default_downloader,
        default_download_mode=config.cdh.default_download_mode,
        download_max_attempts=config.cdh.download_max_attempts,
        download_failure_policy=config.cdh.download_failure_policy,
        aria2_rpc_port=config.cdh.downloader.aria2.rpc_port,
        aria2_split=config.cdh.downloader.aria2.split,
        aria2_max_connection_per_server=(
            config.cdh.downloader.aria2.max_connection_per_server
        ),
        aria2_min_split_size=config.cdh.downloader.aria2.min_split_size,
        aria2_resume_download=config.cdh.downloader.aria2.resume_download,
        httpx_timeout=config.cdh.downloader.httpx.timeout,
    )
    files = tuple(
        FileRequest(
            url=item.url,
            target=str(PurePosixPath(comfyui_path) / item.dir / item.filename),
            overwrite=item.overwrite,
            checksum=item.checksum,
            downloader=item.downloader or downloader.default,
            download_mode=item.download_mode or downloader.default_download_mode,
        )
        for item in config.files
    )
    return CanonicalRequestGraph(
        config_digest=_config_digest(config),
        target_platform=platform,
        backend=backend,
        release=release,
        desired=desired,
        build=BuildRequest(
            tuple(config.build.tags), config.build.output, tuple(config.build.platforms)
        ),
        application=ApplicationRequest(
            workspace=workspace,
            comfyui_path=comfyui_path,
            os_packages=(*DEFAULT_OS_PACKAGES, *config.system.extra_packages),
            python_index_url=config.python.index_url,
            install_manager=config.comfyui.install_manager,
        ),
        custom_nodes=tuple(nodes),
        downloader=downloader,
        files=files,
        runtime=RuntimeRequest(
            environment=tuple(sorted(config.system.env.items())),
            ssh=SshRequest(
                config.system.ssh.enable,
                config.system.ssh.port,
                config.system.ssh.password,
                tuple(config.system.ssh.pub_keys),
            ),
            shutdown_timeout=config.cdh.shutdown_timeout,
            launch_command=(
                f"{_VENV_PATH}/bin/python",
                f"{comfyui_path}/main.py",
                "--listen",
                config.comfyui.listen,
                "--port",
                str(config.comfyui.port),
                "--disable-auto-launch",
                *config.comfyui.extra_args,
            ),
        ),
    )


def comfyui_requirements_request(
    config: FinalConfig,
    comfyui: OfficialComfyUILockEntry,
) -> ComfyUIRequirementsRequestIdentity:
    names = CudaBackendAdapter().protected_requirement_names
    return ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository=comfyui.repository,
        commit=comfyui.commit,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path=COMFYUI_REQUIREMENTS_PATH,
        python_version=config.python.version,
        platform=config.build.platforms[0],
        protected_names=names,
        protected_policy_digest=protected_policy_digest(names),
    )


def request_keys(request: ResolverRequestIdentity) -> tuple[LockEntryKey, ...]:
    if isinstance(request, OciRequestIdentity):
        return (("oci", request.role),)
    if isinstance(request, ManagedPythonRequestIdentity):
        return (("managed-python", request.implementation, request.platform),)
    if isinstance(request, ComfyUIRequestIdentity):
        return (("comfyui", request.repository),)
    if isinstance(request, ComfyUIRequirementsRequestIdentity):
        return (("comfyui-requirements", request.repository),)
    if isinstance(request, ComfyCliRequestIdentity):
        return (("comfy-cli", request.package, request.environment),)
    if isinstance(request, RegistryRequestIdentity):
        return (("registry", request.id),)
    if isinstance(request, DirectGitRequestIdentity):
        return (("git", request.url),)
    if isinstance(request, DirectPythonRequestIdentity):
        return tuple(
            ("python-package", request.environment, member.package)
            for member in request.members
        )
    return (
        *(
            ("python-package", request.environment, member.package)
            for member in request.members
        ),
        ("pytorch-compatibility", request.environment),
    )


def request_stability(request: ResolverRequestIdentity) -> SelectorStability:
    if isinstance(request, OciRequestIdentity):
        return SelectorStability.MOVING
    if isinstance(request, ManagedPythonRequestIdentity):
        return SelectorStability.EXACT
    if isinstance(request, ComfyUIRequestIdentity):
        return (
            SelectorStability.EXACT
            if _is_commit(request.selector) or request.selector[0].isdigit()
            else SelectorStability.MOVING
        )
    if isinstance(request, ComfyUIRequirementsRequestIdentity):
        return SelectorStability.MOVING
    if isinstance(request, ComfyCliRequestIdentity):
        return SelectorStability.MOVING
    if isinstance(request, RegistryRequestIdentity):
        return (
            SelectorStability.MOVING
            if request.selector == "latest"
            or any(character in request.selector for character in "<>=!,")
            else SelectorStability.EXACT
        )
    if isinstance(request, DirectGitRequestIdentity):
        return (
            SelectorStability.EXACT
            if _is_commit(request.ref)
            else SelectorStability.MOVING
        )
    return (
        SelectorStability.EXACT
        if all(member.selector.startswith("==") for member in request.members)
        else SelectorStability.MOVING
    )


def _is_commit(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )


def _members(
    domains: FinalConfigDomainResult,
    group: str,
    *,
    field: str = "extra_packages",
) -> tuple[DirectPythonRequestMember, ...]:
    return tuple(
        DirectPythonRequestMember(
            package=item.name,
            extras=item.extras,
            selector=item.specifier,
        )
        for item in domains.package_requirements
        if item.path[:2] == (group, field)
    )


def _config_digest(config: FinalConfig) -> str:
    canonical = (
        json.dumps(
            config.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
