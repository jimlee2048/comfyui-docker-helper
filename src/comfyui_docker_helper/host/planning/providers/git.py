"""Direct Git identity provider and Git command helpers."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from comfyui_docker_helper.config.credentials.process_policy import (
    noninteractive_git_environment,
)
from comfyui_docker_helper.config.validation.values import is_argv_value
from comfyui_docker_helper.host.credentials.git_process import (
    GitCredentialProcessBinding,
)
from comfyui_docker_helper.host.planning.providers.contracts import (
    _COMMIT_PATTERN,
    DirectGitIdentity,
    DirectGitIdentityRequest,
    IdentityProviderError,
    ProviderFailureKind,
)

_GIT_TIMEOUT_SECONDS = 30.0

type ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class GitDirectIdentityProvider:
    git_executable: str = "git"
    runner: ProcessRunner = subprocess.run
    credential_binding: GitCredentialProcessBinding | None = None

    def resolve(self, request: DirectGitIdentityRequest) -> DirectGitIdentity:
        commit = _resolve_git_ref(
            request.url,
            request.ref,
            source="direct Git",
            git_executable=self.git_executable,
            runner=self.runner,
            credential_binding=self.credential_binding,
        )
        return DirectGitIdentity(type="git", url=request.url, commit=commit)


def _run_git_ls_remote(
    url: str,
    *,
    options: tuple[str, ...],
    patterns: tuple[str, ...],
    source: str,
    git_executable: str,
    runner: ProcessRunner,
    credential_binding: GitCredentialProcessBinding | None = None,
) -> str:
    if not is_argv_value(url) or any(
        not is_argv_value(value) for value in (*options, *patterns)
    ):
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
    config_args: tuple[str, ...] = ()
    environment: Mapping[str, str] = {}
    if credential_binding is not None:
        config_args = credential_binding.config_args
        environment = credential_binding.environment
    env = noninteractive_git_environment(os.environ, overlay=environment)
    try:
        completed = runner(
            (
                git_executable,
                *config_args,
                "ls-remote",
                *options,
                "--end-of-options",
                url,
                *patterns,
            ),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
    except OSError as error:
        raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
    if completed.returncode != 0:
        raise IdentityProviderError(source, ProviderFailureKind.NETWORK)
    return completed.stdout


def _parse_git_output(output: str, source: str) -> tuple[tuple[str, str], ...]:
    resolved: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2 or not _COMMIT_PATTERN.fullmatch(parts[0]):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
        resolved.append((parts[0], parts[1]))
    return tuple(resolved)


def _resolve_git_ref(
    url: str,
    ref: str,
    *,
    source: str,
    git_executable: str,
    runner: ProcessRunner,
    credential_binding: GitCredentialProcessBinding | None = None,
) -> str:
    output = _run_git_ls_remote(
        url,
        options=(),
        patterns=(ref,),
        source=source,
        git_executable=git_executable,
        runner=runner,
        credential_binding=credential_binding,
    )
    resolved = list(_parse_git_output(output, source))
    if not resolved:
        raise IdentityProviderError(source, ProviderFailureKind.NOT_FOUND)
    base_refs = {candidate.removesuffix("^{}") for _, candidate in resolved}
    if ref != "HEAD" and not ref.startswith("refs/") and len(base_refs) != 1:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    for commit, candidate in resolved:
        if candidate.endswith("^{}"):
            return commit
    if len(base_refs) != 1:
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)
    return resolved[0][0]
