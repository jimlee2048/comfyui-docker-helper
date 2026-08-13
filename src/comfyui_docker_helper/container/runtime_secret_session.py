"""Generation-scoped lazy runtime downloader Secret acquisition."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import httpx

from comfyui_docker_helper.config.credential_secrets import (
    CREDENTIAL_SECRET_MAX_BYTES,
    BearerTokenError,
    validate_bearer_token,
)
from comfyui_docker_helper.config.downloader_credentials import (
    DownloaderCredentialContext,
    downloader_httpx_request_context,
    parse_downloader_credential_context,
    select_downloader_credential_context,
)
from comfyui_docker_helper.config.runtime_models import RuntimeConfig
from comfyui_docker_helper.container.downloader_credentials import (
    DownloaderCredentialError,
)


class RuntimeSecretSessionError(DownloaderCredentialError):
    """A content-free local credential acquisition failure."""

    def __init__(self, code: str, secret_name: str | None = None) -> None:
        self.code = code
        self.secret_name = secret_name
        super().__init__(f"runtime downloader credential is unavailable ({code})")


@dataclass(frozen=True, slots=True)
class RuntimeSecretSource:
    """One admitted container-visible Secret source."""

    kind: str
    locator: str


@dataclass(slots=True)
class RuntimeSecretSession:
    """Resolve each logical Secret at most once for one runtime generation."""

    sources: Mapping[str, RuntimeSecretSource]
    environ: Mapping[str, str]
    _values: dict[str, bytes] = field(default_factory=dict, init=False, repr=False)
    _failures: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def bearer_token(self, secret_name: str) -> bytes:
        """Return one cached, consumer-admitted bare Bearer token."""
        with self._lock:
            cached = self._values.get(secret_name)
            if cached is not None:
                return cached
            failure_code = self._failures.get(secret_name)
            if failure_code is not None:
                raise RuntimeSecretSessionError(failure_code, secret_name)
            try:
                value = self._read_source(secret_name)
                validate_bearer_token(value)
            except RuntimeSecretSessionError as error:
                self._failures[secret_name] = error.code
                raise
            except BearerTokenError:
                error = RuntimeSecretSessionError("invalid_value", secret_name)
                self._failures[secret_name] = error.code
                raise error from None
            self._values[secret_name] = value
            return value

    def _read_source(self, secret_name: str) -> bytes:
        source = self.sources.get(secret_name)
        if source is None:
            raise RuntimeSecretSessionError("unknown_secret", secret_name)
        if source.kind == "env":
            value = self.environ.get(source.locator)
            if value is None:
                raise RuntimeSecretSessionError("source_unavailable", secret_name)
            try:
                data = value.encode("utf-8")
            except UnicodeEncodeError:
                raise RuntimeSecretSessionError(
                    "source_unavailable", secret_name
                ) from None
            if len(data) > CREDENTIAL_SECRET_MAX_BYTES:
                raise RuntimeSecretSessionError("invalid_value", secret_name)
            return data
        if source.kind == "file":
            try:
                return _read_projected_secret_file(source.locator)
            except (OSError, ValueError):
                raise RuntimeSecretSessionError(
                    "source_unavailable", secret_name
                ) from None
        raise RuntimeSecretSessionError("invalid_source", secret_name)


@dataclass(frozen=True, slots=True)
class RuntimeDownloaderCredentialPolicy:
    """Select runtime Bearer grants for actual HTTPX request URLs."""

    contexts: tuple[DownloaderCredentialContext, ...]
    secret_names: tuple[str, ...]
    session: RuntimeSecretSession = field(repr=False)

    @classmethod
    def from_config(
        cls,
        config: RuntimeConfig,
        *,
        environ: Mapping[str, str],
    ) -> RuntimeDownloaderCredentialPolicy:
        sources: dict[str, RuntimeSecretSource] = {}
        for name, source in config.secrets.items():
            if source.env is not None:
                sources[name] = RuntimeSecretSource("env", source.env)
            elif source.file is not None:
                sources[name] = RuntimeSecretSource("file", source.file)
        routes = config.cdh.downloader.credentials
        return cls(
            contexts=tuple(
                parse_downloader_credential_context(route.match) for route in routes
            ),
            secret_names=tuple(route.token.secret for route in routes),
            session=RuntimeSecretSession(sources, environ),
        )

    def authorization_for(self, url: httpx.URL) -> bytes | None:
        """Return a complete Authorization value for one selected route."""
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
        return b"Bearer " + self.session.bearer_token(self.secret_names[selected])


def _read_projected_secret_file(path: str) -> bytes:
    """Follow a deployment projection and read one final regular descriptor."""
    parsed = PurePosixPath(path)
    if (
        not path
        or not parsed.is_absolute()
        or path.startswith("//")
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError("runtime Secret path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    primary_error = False
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("runtime Secret source must resolve to a regular file")
        if before.st_size < 0 or before.st_size > CREDENTIAL_SECRET_MAX_BYTES:
            raise OSError("runtime Secret source exceeds the maximum byte count")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, CREDENTIAL_SECRET_MAX_BYTES - total + 1),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > CREDENTIAL_SECRET_MAX_BYTES:
                raise OSError("runtime Secret source exceeds the maximum byte count")
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or before.st_size != after.st_size
            or total != before.st_size
        ):
            raise OSError("runtime Secret source changed during its bounded read")
        return b"".join(chunks)
    except BaseException:
        primary_error = True
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError:
            if not primary_error:
                raise
