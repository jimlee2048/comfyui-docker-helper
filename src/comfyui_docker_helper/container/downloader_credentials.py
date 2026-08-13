"""Narrow transport contract for downloader credential resolution."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

from comfyui_docker_helper.config.credential_secrets import (
    CREDENTIAL_SECRET_MAX_BYTES,
    BearerTokenError,
    downloader_credential_secret_target,
    validate_bearer_token,
)
from comfyui_docker_helper.config.downloader_credentials import (
    DownloaderCredentialContext,
    downloader_httpx_request_context,
    parse_downloader_credential_context,
    select_downloader_credential_context,
)
from comfyui_docker_helper.file_admission import read_bounded_regular_absolute_file

if TYPE_CHECKING:
    from comfyui_docker_helper.config.build_plan import DownloaderCredentialRoutePlan


class DownloaderCredentialError(Exception):
    """Content-safe local failure before an authenticated request is sent."""

    def __init__(self, message: str, *, network_attempted: bool = False) -> None:
        self.network_attempted = network_attempted
        super().__init__(message)


class DownloaderCredentialPolicy(Protocol):
    """Resolve the complete CDH-managed Authorization value for one request."""

    def authorization_for(self, url: httpx.URL) -> bytes | None: ...


@dataclass(slots=True)
class MountedDownloaderCredentialPolicy:
    """Lazily admit BuildKit-mounted Bearer tokens for Plan-owned routes."""

    contexts: tuple[DownloaderCredentialContext, ...]
    secret_ids: tuple[str, ...]
    _values: dict[str, bytes] = field(default_factory=dict, init=False, repr=False)
    _failures: set[str] = field(default_factory=set, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    @classmethod
    def from_routes(
        cls, routes: tuple[DownloaderCredentialRoutePlan, ...]
    ) -> MountedDownloaderCredentialPolicy:
        return cls(
            contexts=tuple(
                parse_downloader_credential_context(route.match) for route in routes
            ),
            secret_ids=tuple(route.secret_id for route in routes),
        )

    def authorization_for(self, url: httpx.URL) -> bytes | None:
        request = downloader_httpx_request_context(
            scheme=url.scheme,
            host=url.host,
            port=url.port,
            raw_path=url.raw_path,
            query=url.query,
        )
        selected = select_downloader_credential_context(self.contexts, request)
        if selected is None:
            return None
        return b"Bearer " + self._bearer_token(self.secret_ids[selected])

    def _bearer_token(self, secret_id: str) -> bytes:
        with self._lock:
            value = self._values.get(secret_id)
            if value is not None:
                return value
            if secret_id in self._failures:
                raise DownloaderCredentialError("Downloader credential is unavailable")
            try:
                target = Path(downloader_credential_secret_target(secret_id))
                value = read_bounded_regular_absolute_file(
                    target,
                    max_bytes=CREDENTIAL_SECRET_MAX_BYTES,
                ).data
                validate_bearer_token(value)
            except (OSError, ValueError, BearerTokenError):
                self._failures.add(secret_id)
                raise DownloaderCredentialError(
                    "Downloader credential is unavailable"
                ) from None
            self._values[secret_id] = value
            return value
