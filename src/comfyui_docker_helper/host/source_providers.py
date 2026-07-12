"""Default host-side source providers for lock resolution."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
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
_GIT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class GitRemoteProvider:
    """Resolve Git refs through ``git ls-remote``."""

    git_executable: str = "git"

    def list_releases(self) -> Sequence[ComfyUIReleaseCandidate]:
        refs = _run_git(
            self.git_executable,
            "ls-remote",
            "--tags",
            "--end-of-options",
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
        refs = _run_git(
            self.git_executable,
            "ls-remote",
            "--end-of-options",
            COMFYUI_REPO_URL,
            "HEAD",
        )
        commit, _ = _select_single_ls_remote_ref(
            refs,
            selector="HEAD",
            url=COMFYUI_REPO_URL,
        )
        return commit

    def resolve_default_branch_head(self, url: str) -> str:
        refs = _run_git(
            self.git_executable,
            "ls-remote",
            "--end-of-options",
            url,
            "HEAD",
        )
        commit, _ = _select_single_ls_remote_ref(refs, selector="HEAD", url=url)
        return commit

    def resolve_ref(self, url: str, ref: str) -> str:
        refs = _run_git(
            self.git_executable,
            "ls-remote",
            "--end-of-options",
            url,
            ref,
        )
        lines = refs.splitlines()
        if not lines:
            raise UpstreamResponseError(
                source="git ls-remote",
                selector=ref,
                reason=f"no remote ref matched url {url!r}",
            )
        commit, _ = _select_resolved_ref(lines, selector=ref)
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
    base_url: str = "https://api.comfy.org"

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


@dataclass(slots=True)
class DefaultSourceResolvers:
    """Default live source resolvers plus their owned HTTP client."""

    resolvers: SourceResolvers
    client: httpx.Client
    _closed: bool = False

    def __enter__(self) -> SourceResolvers:
        return self.resolvers

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        """Close the owned HTTP client exactly once."""
        if self._closed:
            return
        self._closed = True
        self.client.close()


def create_default_source_resolvers() -> DefaultSourceResolvers:
    """Return concrete source providers used by host render/build commands."""
    client = httpx.Client(timeout=30.0, follow_redirects=True)
    git = GitRemoteProvider()
    return DefaultSourceResolvers(
        resolvers=SourceResolvers(
            comfyui=git,
            comfy_cli=PyPIComfyCliProvider(client),
            registry=HttpRegistryProvider(client),
            git=git,
        ),
        client=client,
    )


def _run_git(git_executable: str, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
        "SSH_ASKPASS": "",
    }
    try:
        completed = subprocess.run(
            (git_executable, *args),
            text=True,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise UpstreamResponseError(
            source="git",
            selector=" ".join(args),
            reason=f"git command timed out after {_GIT_TIMEOUT_SECONDS:g} seconds",
        ) from error
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


def _select_single_ls_remote_ref(
    refs: str,
    *,
    selector: str,
    url: str,
) -> tuple[str, str]:
    lines = refs.splitlines()
    if not lines:
        raise UpstreamResponseError(
            source="git ls-remote",
            selector=selector,
            reason=f"no remote ref matched url {url!r}",
        )
    return _select_resolved_ref(lines, selector=selector)


def _select_resolved_ref(lines: Sequence[str], *, selector: str) -> tuple[str, str]:
    resolved_refs = [_split_ls_remote_line(line) for line in lines]
    _reject_ambiguous_shorthand_ref(resolved_refs, selector)
    for commit, ref in resolved_refs:
        if ref.endswith("^{}"):
            return commit, ref
    return resolved_refs[0]


def _reject_ambiguous_shorthand_ref(
    resolved_refs: Sequence[tuple[str, str]],
    selector: str,
) -> None:
    if selector == "HEAD" or selector.startswith("refs/"):
        return
    base_refs = {ref.removesuffix("^{}") for _, ref in resolved_refs}
    if len(base_refs) <= 1:
        return
    formatted_refs = ", ".join(sorted(base_refs))
    raise UpstreamResponseError(
        source="git ls-remote",
        selector=selector,
        reason=(
            f"ambiguous remote ref matched {formatted_refs}; "
            "use a full ref such as refs/heads/name or refs/tags/name"
        ),
    )


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
        node_id=str(data.get("node_id", data.get("id", node_id))),
        version=resolved_version,
        active=_registry_bool(
            data,
            "active",
            default=True,
            source="Comfy Registry install",
            selector=version or "latest",
        ),
        installable=_registry_bool(
            data,
            "installable",
            default=True,
            source="Comfy Registry install",
            selector=version or "latest",
        ),
        deprecated=_registry_bool(
            data,
            "deprecated",
            default=False,
            source="Comfy Registry install",
            selector=version or "latest",
        ),
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
        node_id=str(data.get("node_id", data.get("id", node_id))),
        version=version,
        active=_registry_bool(
            data,
            "active",
            default=True,
            source="Comfy Registry versions",
            selector=node_id,
        ),
        installable=_registry_bool(
            data,
            "installable",
            default=True,
            source="Comfy Registry versions",
            selector=node_id,
        ),
        deprecated=_registry_bool(
            data,
            "deprecated",
            default=False,
            source="Comfy Registry versions",
            selector=node_id,
        ),
    )


def _registry_bool(
    data: Mapping[str, object],
    field: str,
    *,
    default: bool,
    source: str,
    selector: str,
) -> bool:
    value = data.get(field, default)
    if isinstance(value, bool):
        return value
    raise UpstreamResponseError(
        source=source,
        selector=selector,
        reason=f"response field {field!r} must be a boolean",
    )
