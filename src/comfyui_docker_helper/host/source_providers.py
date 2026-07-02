"""Default host-side source providers for lock resolution."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from comfyui_docker_helper.config import (
    COMFY_CLI_PACKAGE_NAME,
    COMFYUI_REPO_URL,
    ComfyCliVersionCandidate,
    ComfyUIReleaseCandidate,
    RegistryInstallMetadata,
    RegistryVersionCandidate,
    SourceResolvers,
    UpstreamResponseError,
)

_GITHUB_TAG_REFS_PREFIX = "refs/tags/"


@dataclass(frozen=True, slots=True)
class GitRemoteProvider:
    """Resolve Git refs through ``git ls-remote``."""

    git_executable: str = "git"

    def list_releases(self) -> Sequence[ComfyUIReleaseCandidate]:
        refs = _run_git(
            self.git_executable,
            "ls-remote",
            "--tags",
            COMFYUI_REPO_URL,
        )
        tag_refs: dict[str, str] = {}
        peeled_refs: dict[str, str] = {}
        for line in refs.splitlines():
            commit, ref = _split_ls_remote_line(line)
            if not ref.startswith(_GITHUB_TAG_REFS_PREFIX):
                continue
            version = ref.removeprefix(_GITHUB_TAG_REFS_PREFIX)
            if version.endswith("^{}"):
                peeled_refs[version.removesuffix("^{}")] = commit
            else:
                tag_refs[version] = commit
        return [
            ComfyUIReleaseCandidate(
                version=version,
                commit=peeled_refs.get(version, commit),
            )
            for version, commit in tag_refs.items()
        ]

    def get_nightly_commit(self) -> str:
        refs = _run_git(self.git_executable, "ls-remote", COMFYUI_REPO_URL, "HEAD")
        commit, _ = _split_ls_remote_line(refs.splitlines()[0])
        return commit

    def resolve_default_branch_head(self, url: str) -> str:
        refs = _run_git(self.git_executable, "ls-remote", url, "HEAD")
        commit, _ = _split_ls_remote_line(refs.splitlines()[0])
        return commit

    def resolve_ref(self, url: str, ref: str) -> str:
        refs = _run_git(self.git_executable, "ls-remote", url, ref)
        lines = refs.splitlines()
        if not lines:
            raise UpstreamResponseError(
                source="git ls-remote",
                selector=ref,
                reason=f"no remote ref matched url {url!r}",
            )
        commit, _ = _split_ls_remote_line(lines[0])
        return commit


@dataclass(frozen=True, slots=True)
class PyPIComfyCliProvider:
    """Read comfy-cli versions from the PyPI JSON API."""

    client: httpx.Client

    def list_versions(self) -> Sequence[ComfyCliVersionCandidate]:
        data = _get_json(
            self.client,
            f"https://pypi.org/pypi/{COMFY_CLI_PACKAGE_NAME}/json",
            source="PyPI comfy-cli",
            selector="versions",
        )
        releases = data.get("releases") if isinstance(data, Mapping) else None
        if not isinstance(releases, dict):
            raise UpstreamResponseError(
                source="PyPI comfy-cli",
                selector="versions",
                reason="response did not contain a releases table",
            )
        return [ComfyCliVersionCandidate(version=str(version)) for version in releases]


@dataclass(frozen=True, slots=True)
class HttpRegistryProvider:
    """Read Comfy Registry metadata through its HTTP API."""

    client: httpx.Client
    base_url: str = "https://registry.comfy.org"

    def get_install_metadata(
        self,
        node_id: str,
        version: str | None = None,
    ) -> RegistryInstallMetadata:
        params = {} if version is None else {"version": version}
        data = _get_json(
            self.client,
            f"{self.base_url}/nodes/{node_id}/install",
            source="Comfy Registry install",
            selector=version or "latest",
            params=params,
        )
        return _registry_install_metadata(data, node_id, version)

    def list_versions(self, node_id: str) -> Sequence[RegistryVersionCandidate]:
        data = _get_json(
            self.client,
            f"{self.base_url}/nodes/{node_id}/versions",
            source="Comfy Registry versions",
            selector=node_id,
        )
        items = data.get("versions") if isinstance(data, Mapping) else data
        if not isinstance(items, list):
            raise UpstreamResponseError(
                source="Comfy Registry versions",
                selector=node_id,
                reason="response did not contain a versions list",
            )
        return [_registry_version_candidate(item, node_id) for item in items]


def create_default_source_resolvers() -> SourceResolvers:
    """Return concrete source providers used by host render/build commands."""
    client = httpx.Client(timeout=30.0, follow_redirects=True)
    git = GitRemoteProvider()
    return SourceResolvers(
        comfyui=git,
        comfy_cli=PyPIComfyCliProvider(client),
        registry=HttpRegistryProvider(client),
        git=git,
    )


def _run_git(git_executable: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            (git_executable, *args),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise UpstreamResponseError(
            source="git",
            selector=" ".join(args),
            reason=str(error),
        ) from error
    if completed.returncode != 0:
        reason = completed.stderr.strip() or completed.stdout.strip()
        raise UpstreamResponseError(
            source="git",
            selector=" ".join(args),
            reason=reason or f"git exited with code {completed.returncode}",
        )
    return completed.stdout


def _get_json(
    client: httpx.Client,
    url: str,
    *,
    source: str,
    selector: str,
    params: Mapping[str, str] | None = None,
) -> Any:
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        raise UpstreamResponseError(
            source=source,
            selector=selector,
            reason=f"HTTP request failed with status {status_code}",
        ) from error
    except httpx.RequestError as error:
        raise UpstreamResponseError(
            source=source,
            selector=selector,
            reason=f"HTTP request failed: {error}",
        ) from error
    except ValueError as error:
        raise UpstreamResponseError(
            source=source,
            selector=selector,
            reason="response body was not valid JSON",
        ) from error


def _split_ls_remote_line(line: str) -> tuple[str, str]:
    parts = line.split()
    if len(parts) != 2:
        raise UpstreamResponseError(
            source="git ls-remote",
            selector="response",
            reason="unexpected output line",
        )
    return parts[0], parts[1]


def _registry_install_metadata(
    data: object,
    node_id: str,
    version: str | None,
) -> RegistryInstallMetadata:
    if not isinstance(data, dict):
        raise UpstreamResponseError(
            source="Comfy Registry install",
            selector=version or "latest",
            reason="response was not an object",
        )
    resolved_version = data.get("version", version)
    if not isinstance(resolved_version, str) or resolved_version == "":
        raise UpstreamResponseError(
            source="Comfy Registry install",
            selector=version or "latest",
            reason="response did not contain a version string",
        )
    return RegistryInstallMetadata(
        node_id=str(data.get("id", data.get("node_id", node_id))),
        version=resolved_version,
        active=bool(data.get("active", True)),
        installable=bool(data.get("installable", True)),
        deprecated=bool(data.get("deprecated", False)),
    )


def _registry_version_candidate(
    data: object,
    node_id: str,
) -> RegistryVersionCandidate:
    if not isinstance(data, dict):
        raise UpstreamResponseError(
            source="Comfy Registry versions",
            selector=node_id,
            reason="version item was not an object",
        )
    version = data.get("version")
    if not isinstance(version, str) or version == "":
        raise UpstreamResponseError(
            source="Comfy Registry versions",
            selector=node_id,
            reason="version item did not contain a version string",
        )
    return RegistryVersionCandidate(
        node_id=str(data.get("id", data.get("node_id", node_id))),
        version=version,
        active=bool(data.get("active", True)),
        installable=bool(data.get("installable", True)),
        deprecated=bool(data.get("deprecated", False)),
    )
