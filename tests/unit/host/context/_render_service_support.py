"""Shared fixtures for Host render-context behavior owners."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from tests.build_plan_support import canonical_wheel

from comfyui_docker_helper.config.authored.service import load_validate_config_result
from comfyui_docker_helper.config.planning.canonical_lock import (
    ApplicationExtrasLockEntry,
    CanonicalLockEntry,
    ComfyCliRequestIdentity,
    ComfyUIRequestIdentity,
    ComfyUIRequirementsLockEntry,
    ComfyUIRequirementsRequestIdentity,
    CudaImageLockEntry,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
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
    UvImageLockEntry,
    UvToolLockEntry,
)
from comfyui_docker_helper.config.planning.resolver import (
    AcquiredCanonicalEntries,
)
from comfyui_docker_helper.host.buildx import BuildxOutput
from comfyui_docker_helper.host.context.service import (
    PlanningOptions,
    admit_build_hook_source,
    prepare_render_context,
)
from comfyui_docker_helper.host.planning.acquisition import (
    LocalExecutableEntryAcquirer,
)
from comfyui_docker_helper.host.planning.authority import (
    CachingCanonicalAcquirer,
)
from comfyui_docker_helper.host.planning.providers.local import (
    FilesystemLocalExecutableIdentityProvider,
)

DIGEST_A = f"sha256:{'a' * 64}"

DIGEST_B = f"sha256:{'b' * 64}"

DIGEST_C = f"sha256:{'c' * 64}"

COMMIT = "1" * 40

CANONICAL_WHEEL = canonical_wheel()


def _config(
    *,
    with_uv_tool: bool = False,
    install_cli: bool = True,
    install_manager: bool = False,
    python_version: str = "3.13.14",
    image_flavor: str = "cudnn-devel",
    image_distro: str = "ubuntu24.04",
    uv_version: str | None = "0.11.28",
) -> str:
    uv_tools = 'uv_tools = ["ruff>=0.15,<0.16"]' if with_uv_tool else ""
    uv_selector = f'uv_version = "{uv_version}"' if uv_version is not None else ""
    return f"""
[compute_platform]
type = "cuda"
[compute_platform.cuda]
version = "13.0.3"
image_flavor = "{image_flavor}"
image_distro = "{image_distro}"
[python]
version = "{python_version}"
{uv_selector}
{uv_tools}
[pytorch]
version = "2.12.1"
[comfyui]
version = "0.11.0"
install_cli = {str(install_cli).lower()}
install_manager = {str(install_manager).lower()}
[build]
tags = ["example:test"]
platforms = ["linux/amd64"]
"""


@dataclass
class FakeAcquirer:
    calls: list[str] = field(default_factory=list)
    requirements_content: bytes = b"torch\ntorchvision\ntorchaudio\n"
    formal_release: str | None = "0.11.0"

    def acquire(self, request, request_digest: str) -> AcquiredCanonicalEntries:
        self.calls.append(request.type)
        entries: tuple[CanonicalLockEntry, ...]
        if isinstance(request, OciRequestIdentity):
            common = dict(
                request_digest=request_digest,
                repository=request.repository,
                tag=request.tag,
                digest=(DIGEST_B if request.role == "uv-tool" else DIGEST_A),
                kind="index",
                platform=request.platform,
            )
            entries = (
                UvImageLockEntry(**common, observed_version="0.11.28")
                if request.role == "uv-tool"
                else CudaImageLockEntry(**common),
            )
        elif isinstance(request, ManagedPythonRequestIdentity):
            entries = (
                ManagedPythonLockEntry(
                    request_digest=request_digest,
                    version=request.version,
                    platform=request.platform,
                    libc=request.libc,
                    catalog_digest=request.catalog_descriptor_digest,
                    artifact_key="cpython-3.13.14-linux-x86_64-gnu",
                    artifact_url="https://example.test/python.tar.zst",
                ),
            )
        elif isinstance(request, ComfyUIRequestIdentity):
            entries = (
                OfficialComfyUILockEntry(
                    request_digest=request_digest,
                    repository=request.repository,
                    commit=COMMIT,
                    formal_release=self.formal_release,
                ),
            )
        elif isinstance(request, ComfyUIRequirementsRequestIdentity):
            content = self.requirements_content
            entries = (
                ComfyUIRequirementsLockEntry(
                    request_digest=request_digest,
                    digest=(f"sha256:{hashlib.sha256(content).hexdigest()}"),
                    content=content.decode("utf-8"),
                ),
            )
        elif isinstance(request, ComfyCliRequestIdentity):
            entries = (
                UvToolLockEntry(
                    request_digest=request_digest,
                    name="comfy-cli",
                    extras=(),
                    version="1.8.0",
                ),
            )
        elif isinstance(request, RegistryRequestIdentity):
            entries = (
                RegistryNodeLockEntry(
                    request_digest=request_digest,
                    id=request.id,
                    version="1.0.0",
                ),
            )
        elif isinstance(request, DirectGitRequestIdentity):
            entries = (
                DirectGitLockEntry(
                    request_digest=request_digest,
                    url=request.url,
                    commit=COMMIT,
                ),
            )
        elif isinstance(request, PythonGroupRequestIdentity):
            versions = {
                "torch": "2.12.1+cu130",
                "torchaudio": "2.11.0+cu130",
                "torchvision": "0.27.1+cu130",
                "ruff": "0.15.18",
            }
            packages = tuple(
                ResolvedPythonPackage(
                    name=member.package,
                    extras=member.extras,
                    version=versions[member.package],
                )
                for member in request.members
            )
            if isinstance(request, PyTorchRequestIdentity):
                entries = (
                    PyTorchLockEntry(
                        request_digest=request_digest,
                        packages=packages,
                        setuptools_specifier="<82",
                    ),
                )
            elif request.group == "application-extra":
                entries = (
                    ApplicationExtrasLockEntry(
                        request_digest=request_digest,
                        packages=packages,
                    ),
                )
            else:
                member = packages[0]
                entries = (
                    UvToolLockEntry(
                        request_digest=request_digest,
                        name=member.name,
                        extras=member.extras,
                        version=member.version,
                    ),
                )
        else:  # pragma: no cover
            raise AssertionError(request)
        return AcquiredCanonicalEntries(entries, True)


def _prepare(
    config: Path | list[Path],
    output: Path,
    fake: object,
    *,
    options: PlanningOptions | None = None,
    overwrite: bool = False,
    runtime_hooks_dir: Path | None = None,
    working_directory: Path | None = None,
    build_hooks_dir: Path | str | None = None,
    tag_templates: tuple[str, ...] = (),
    output_mode: BuildxOutput = "load",
    event_sink=None,
):
    configuration_result = load_validate_config_result(
        config, build_hooks_dir=build_hooks_dir
    )
    build_hook_source_root = admit_build_hook_source(
        configuration_result,
        build_hooks_dir,
        output,
        working_directory=working_directory,
    )
    return prepare_render_context(
        output,
        configuration_result=configuration_result,
        build_hook_source_root=build_hook_source_root,
        acquirer=CachingCanonicalAcquirer(fake),
        local_acquirer=LocalExecutableEntryAcquirer(
            FilesystemLocalExecutableIdentityProvider()
        ),
        canonical_wheel=CANONICAL_WHEEL,
        tag_templates=tag_templates,
        output_mode=output_mode,
        options=options,
        overwrite=overwrite,
        runtime_hooks_dir=runtime_hooks_dir,
        working_directory=working_directory,
        event_sink=event_sink,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _runtime_hooks(root: Path) -> Path:
    files = {
        "pre-start.d/10-pre.sh": "pre\n",
        "post-start.d/20-post.py": "print('post')\n",
        "stop.d/30-stop.sh": "stop\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)
    return root
