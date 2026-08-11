"""Pure immutable request graph shared by reconciliation and BuildPlan projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, cast

from comfyui_docker_helper.comfyui_requirements import (
    COMFYUI_REQUIREMENTS_PATH,
    ComfyUIRequirementsError,
    ParsedComfyUIRequirements,
    merge_pytorch_requirements,
    parse_comfyui_requirements,
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
)
from comfyui_docker_helper.config.git_credentials import (
    canonicalize_git_credential_context,
)
from comfyui_docker_helper.config.os_packages import DEFAULT_OS_PACKAGES
from comfyui_docker_helper.config.requirement_validation import (
    direct_selector_is_exact,
)
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
class PlanningReleaseInputs:
    """Exact release-owned inputs projected into BuildPlan, never the lock."""

    pip_version: str
    cdh_version: str
    cdh_wheel_digest: str


@dataclass(frozen=True, slots=True)
class DesiredResolution:
    """One immutable provider acquisition unit."""

    request: ResolverRequestIdentity
    keys: tuple[LockEntryKey, ...] = field(init=False)
    request_digest: str = field(init=False)
    stability: SelectorStability = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "keys", request_keys(self.request))
        object.__setattr__(self, "request_digest", compute_request_digest(self.request))
        object.__setattr__(self, "stability", request_stability(self.request))


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
    pre_install_hooks: tuple[str, ...]
    post_install_hooks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitNodeRequest:
    type: Literal["git"]
    url: str
    ref: str
    target: str
    pre_install_hooks: tuple[str, ...]
    post_install_hooks: tuple[str, ...]


type CustomNodeRequest = RegistryNodeRequest | GitNodeRequest


@dataclass(frozen=True, slots=True)
class GitCredentialRouteRequest:
    """Safe in-memory Git credential intent before BuildPlan projection."""

    match: str
    username: str
    secret: str


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

    image_config_digest: str
    target_platform: TargetPlatform
    backend: BackendPlan
    release: PlanningReleaseInputs
    protected_requirement_names: tuple[str, ...]
    comfyui_requirements: ParsedComfyUIRequirements
    desired: tuple[DesiredResolution, ...]
    application: ApplicationRequest
    custom_nodes: tuple[CustomNodeRequest, ...]
    git_credentials: tuple[GitCredentialRouteRequest, ...]
    downloader: DownloaderRequest
    files: tuple[FileRequest, ...]
    runtime: RuntimeRequest


def uv_oci_request(config: FinalConfig) -> OciRequestIdentity:
    """Build the canonical uv image request shared by staging and reconciliation."""
    return OciRequestIdentity(
        type="oci",
        role="uv-tool",
        repository=UV_IMAGE_REPOSITORY,
        tag=uv_provider_tag(config.python.uv_version),
        platform=TargetPlatform(config.build.platforms[0]).value,
    )


def uv_provider_tag(selector: str) -> str:
    """Map one validated public uv release selector to cdh's provider family."""
    return "debian-slim" if selector == "latest" else f"{selector}-debian-slim"


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
    domains: FinalConfigDomainResult,
    release: PlanningReleaseInputs,
    uv_descriptor_digest: str,
    comfyui_entry: OfficialComfyUILockEntry,
    requirements_entry: ComfyUIRequirementsLockEntry,
) -> CanonicalRequestGraph:
    """Project validated config and accepted staged identities exactly once."""
    platform = TargetPlatform(config.build.platforms[0])
    backend = CudaBackendAdapter().derive(
        CudaVersion.from_validated(config.compute_platform.cuda.version),
        image_flavor=config.compute_platform.cuda.image_flavor,
        image_distro=config.compute_platform.cuda.image_distro,
    )
    repository, tag = backend.base_image.split(":", 1)
    requirements_request = comfyui_requirements_request(comfyui_entry)
    if requirements_entry.request_digest != compute_request_digest(
        requirements_request
    ):
        raise ValueError("ComfyUI requirements identity does not match final config")
    protected_requirement_names = CudaBackendAdapter().protected_requirement_names
    try:
        requirements_projection = parse_comfyui_requirements(
            requirements_entry.content.encode("utf-8"),
            python_version=config.python.version,
            platform=platform.value,
            machine="x86_64",
            protected_names=protected_requirement_names,
        )
    except ComfyUIRequirementsError as error:
        raise CanonicalRequestError(
            (
                Diagnostic(
                    path=("comfyui", "requirements"),
                    code="comfyui.requirements_invalid",
                    message=str(error),
                ),
            )
        ) from error

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
                resolver_descriptor_digest=uv_descriptor_digest,
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
                resolver_descriptor_digest=uv_descriptor_digest,
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
                resolver_descriptor_digest=uv_descriptor_digest,
                members=(member,),
            )
        )
    upstream = tuple(
        DirectPythonRequestMember(
            package=item.package,
            extras=item.extras,
            selector=item.selector,
        )
        for item in requirements_projection.protected
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
            resolver_descriptor_digest=uv_descriptor_digest,
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
        pre_install_hooks = tuple(node.pre_install_hooks)
        post_install_hooks = tuple(node.post_install_hooks)
        if node.type == "registry":
            selector = node.version or "latest"
            requests.append(
                RegistryRequestIdentity(type="registry", id=node.id, selector=selector)
            )
            nodes.append(
                RegistryNodeRequest(
                    "registry",
                    node.id,
                    selector,
                    pre_install_hooks,
                    post_install_hooks,
                )
            )
        else:
            ref = node.ref or "HEAD"
            requests.append(DirectGitRequestIdentity(type="git", url=node.url, ref=ref))
            target = str(
                PurePosixPath(comfyui_path)
                / "custom_nodes"
                / resolve_git_target_dir(node.url, node.target_dir)
            )
            nodes.append(
                GitNodeRequest(
                    "git",
                    node.url,
                    ref,
                    target,
                    pre_install_hooks,
                    post_install_hooks,
                )
            )

    desired = tuple(DesiredResolution(request) for request in requests)
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
            target=str(PurePosixPath(comfyui_path) / normalized.relative_target),
            overwrite=item.overwrite,
            checksum=item.checksum,
            downloader=item.downloader or downloader.default,
            download_mode=item.download_mode or downloader.default_download_mode,
        )
        for item, normalized in zip(config.files, domains.files, strict=True)
    )
    return CanonicalRequestGraph(
        image_config_digest=_image_config_digest(config, domains),
        target_platform=platform,
        backend=backend,
        release=release,
        protected_requirement_names=protected_requirement_names,
        comfyui_requirements=requirements_projection,
        desired=desired,
        application=ApplicationRequest(
            workspace=workspace,
            comfyui_path=comfyui_path,
            os_packages=(
                *DEFAULT_OS_PACKAGES,
                *(item.value for item in domains.apt_packages),
            ),
            python_index_url=config.python.index_url,
            install_manager=config.comfyui.install_manager,
        ),
        custom_nodes=tuple(nodes),
        git_credentials=tuple(
            GitCredentialRouteRequest(
                match=canonicalize_git_credential_context(route.match),
                username=route.username,
                secret=route.password.secret,
            )
            for route in config.cdh.git.credentials
        ),
        downloader=downloader,
        files=files,
        runtime=RuntimeRequest(
            environment=tuple(sorted(config.system.env.items())),
            ssh=SshRequest(
                config.system.ssh.enable,
                config.system.ssh.port,
                config.system.ssh.password,
                domains.ssh_public_keys,
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
    comfyui: OfficialComfyUILockEntry,
) -> ComfyUIRequirementsRequestIdentity:
    return ComfyUIRequirementsRequestIdentity(
        type="comfyui-requirements",
        repository=comfyui.repository,
        commit=comfyui.commit,
        floor_commit=COMFYUI_FLOOR_COMMIT,
        path=COMFYUI_REQUIREMENTS_PATH,
    )


def request_keys(request: ResolverRequestIdentity) -> tuple[LockEntryKey, ...]:
    if isinstance(request, OciRequestIdentity):
        return (("images", "cuda" if request.role == "cuda-base" else "uv"),)
    if isinstance(request, ManagedPythonRequestIdentity):
        return (("python", "interpreter"),)
    if isinstance(request, ComfyUIRequestIdentity):
        return (("comfyui",),)
    if isinstance(request, ComfyUIRequirementsRequestIdentity):
        return (("comfyui", "requirements"),)
    if isinstance(request, ComfyCliRequestIdentity):
        return (("python", "uv_tools", request.package),)
    if isinstance(request, RegistryRequestIdentity):
        return (("custom_nodes", "registry", request.id),)
    if isinstance(request, DirectGitRequestIdentity):
        return (("custom_nodes", "git", request.url),)
    if isinstance(request, DirectPythonRequestIdentity):
        if request.group == "application-extra":
            return (("python", "package_groups", "application_extras"),)
        return (("python", "uv_tools", request.members[0].package),)
    return (("python", "package_groups", "pytorch"),)


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
        return SelectorStability.EXACT
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
        if all(direct_selector_is_exact(member.selector) for member in request.members)
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


def _image_config_digest(
    config: FinalConfig,
    domains: FinalConfigDomainResult,
) -> str:
    canonical = (
        json.dumps(
            _image_config_projection(config, domains),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _image_config_projection(
    config: FinalConfig,
    domains: FinalConfigDomainResult,
) -> dict[str, object]:
    document: dict[str, object] = config.model_dump(mode="json")
    document.pop("secrets")
    build = cast(dict[str, object], document["build"])
    cdh = cast(dict[str, object], document["cdh"])
    git = cast(dict[str, object], cdh["git"])
    system = cast(dict[str, object], document["system"])
    python = cast(dict[str, object], document["python"])
    pytorch = cast(dict[str, object], document["pytorch"])
    document["files"] = [
        {
            **item.model_dump(mode="json"),
            "dir": normalized.directory.as_posix(),
        }
        for item, normalized in zip(config.files, domains.files, strict=True)
    ]
    system["extra_packages"] = [item.value for item in domains.apt_packages]
    ssh = cast(dict[str, object], system["ssh"])
    ssh["pub_keys"] = list(domains.ssh_public_keys)
    python["extra_packages"] = [
        item.canonical_value
        for item in domains.package_requirements
        if item.path[:2] == ("python", "extra_packages")
    ]
    python["uv_tools"] = [
        item.canonical_value
        for item in domains.package_requirements
        if item.path[:2] == ("python", "uv_tools")
    ]
    pytorch["extra_packages"] = [
        item.canonical_value
        for item in domains.package_requirements
        if item.path[:2] == ("pytorch", "extra_packages")
    ]
    git["credentials"] = [
        {
            "match": canonicalize_git_credential_context(route.match),
            "username": route.username,
            "password": {"secret": route.password.secret},
        }
        for route in config.cdh.git.credentials
    ]
    return {
        **document,
        "build": {
            key: value for key, value in build.items() if key not in {"tags", "output"}
        },
    }
