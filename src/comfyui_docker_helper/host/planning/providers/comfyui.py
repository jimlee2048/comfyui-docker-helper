"""Official ComfyUI Git identity provider."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from packaging.version import Version

from comfyui_docker_helper.exact_ledger import COMFYUI_REPOSITORY
from comfyui_docker_helper.host.planning.providers.contracts import (
    _COMMIT_PATTERN,
    IdentityProviderError,
    OfficialComfyUIIdentity,
    OfficialComfyUIIdentityRequest,
    ProviderFailureKind,
    _formal_release_from_ref,
)
from comfyui_docker_helper.host.planning.providers.git import (
    _GIT_TIMEOUT_SECONDS,
    ProcessRunner,
    _parse_git_output,
    _resolve_git_ref,
    _run_git_ls_remote,
)


@dataclass(frozen=True, slots=True)
class GitOfficialComfyUIIdentityProvider:
    git_executable: str = "git"
    runner: ProcessRunner = subprocess.run

    def list_releases(self, repository: str) -> tuple[OfficialComfyUIIdentity, ...]:
        source = "official ComfyUI"
        _require_official_comfyui_repository(repository)
        output = _run_git_ls_remote(
            repository,
            options=("--tags",),
            patterns=(),
            source=source,
            git_executable=self.git_executable,
            runner=self.runner,
        )
        tags: dict[str, str] = {}
        peeled: dict[str, str] = {}
        for commit, ref in _parse_git_output(output, source):
            if not ref.startswith("refs/tags/"):
                continue
            base_ref = ref.removesuffix("^{}")
            if _formal_release_from_ref(base_ref) is None:
                continue
            if ref.endswith("^{}"):
                peeled[base_ref] = commit
            else:
                tags[base_ref] = commit
        identities = [
            OfficialComfyUIIdentity(
                repository=COMFYUI_REPOSITORY,
                commit=peeled.get(ref, commit),
                formal_release=_formal_release_from_ref(ref),
            )
            for ref, commit in tags.items()
        ]
        return tuple(
            sorted(identities, key=lambda item: Version(item.formal_release or "0"))
        )

    def resolve(
        self, request: OfficialComfyUIIdentityRequest
    ) -> OfficialComfyUIIdentity:
        _require_official_comfyui_repository(request.repository)
        commit = _resolve_git_ref(
            request.repository,
            request.ref,
            source="official ComfyUI",
            git_executable=self.git_executable,
            runner=self.runner,
        )
        return OfficialComfyUIIdentity(
            repository=COMFYUI_REPOSITORY,
            commit=commit,
            formal_release=_formal_release_from_ref(request.ref),
        )

    def is_ancestor(self, repository: str, ancestor: str, descendant: str) -> bool:
        """Prove one resolved commit is inside the supported official history."""
        source = "official ComfyUI ancestry"
        _require_official_comfyui_repository(repository)
        if not _COMMIT_PATTERN.fullmatch(ancestor) or not _COMMIT_PATTERN.fullmatch(
            descendant
        ):
            raise IdentityProviderError(source, ProviderFailureKind.INVALID_REQUEST)
        environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
            "SSH_ASKPASS": "",
        }
        try:
            with tempfile.TemporaryDirectory(prefix="cdh-comfyui-ancestry-") as raw:
                checkout = Path(raw) / "repository.git"
                cloned = self.runner(
                    (
                        self.git_executable,
                        "clone",
                        "--bare",
                        "--filter=blob:none",
                        "--",
                        repository,
                        os.fspath(checkout),
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=_GIT_TIMEOUT_SECONDS,
                    env=environment,
                )
                if cloned.returncode != 0:
                    raise IdentityProviderError(source, ProviderFailureKind.NETWORK)
                checked = self.runner(
                    (
                        self.git_executable,
                        "-C",
                        os.fspath(checkout),
                        "merge-base",
                        "--is-ancestor",
                        ancestor,
                        descendant,
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=_GIT_TIMEOUT_SECONDS,
                    env=environment,
                )
        except subprocess.TimeoutExpired as error:
            raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
        except OSError as error:
            raise IdentityProviderError(source, ProviderFailureKind.NETWORK) from error
        if checked.returncode == 0:
            return True
        if checked.returncode == 1:
            return False
        raise IdentityProviderError(source, ProviderFailureKind.INVALID_RESPONSE)


def _require_official_comfyui_repository(repository: str) -> None:
    if repository != COMFYUI_REPOSITORY:
        raise IdentityProviderError(
            "official ComfyUI", ProviderFailureKind.INVALID_REQUEST
        )
