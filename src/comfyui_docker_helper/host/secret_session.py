"""Command-scoped host Secret snapshots for Git credential helpers."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticSeverity
from comfyui_docker_helper.config.git_credentials import (
    GIT_CREDENTIAL_VALUE_MAX_BYTES,
    canonicalize_git_credential_context,
    git_credential_secret_id,
    parse_git_credential_context,
)
from comfyui_docker_helper.config.service import ConfigurationResult
from comfyui_docker_helper.file_admission import (
    read_bounded_regular_absolute_file,
)
from comfyui_docker_helper.git_credential_policy import git_credential_config_args
from comfyui_docker_helper.git_credential_protocol import GitCredentialRuntimeRoute

__all__ = [
    "GIT_CREDENTIAL_SESSION_ENV",
    "GitCredentialProcessBinding",
    "HostSecretSession",
    "HostSecretSessionError",
]

GIT_CREDENTIAL_SESSION_ENV = "CDH_GIT_CREDENTIAL_SESSION"

_METADATA_FILE = "metadata.json"
_SNAPSHOT_PREFIX = "snapshot-"
_LOCK_PREFIX = "lock-"
_FAILURE_PREFIX = "failure-"
_WARNING_PREFIX = "warning-permissive-mode-"


@dataclass(frozen=True, slots=True)
class GitCredentialProcessBinding:
    """Safe Git configuration and environment for one private helper session."""

    config_args: tuple[str, ...]
    environment: Mapping[str, str]


class HostSecretSessionError(ValueError):
    """A content-free expected Secret-session failure."""

    def __init__(self, code: str, secret_name: str | None = None) -> None:
        self.code = code
        self.secret_name = secret_name
        subject = (
            "host Secret session" if secret_name is None else f"Secret {secret_name}"
        )
        super().__init__(f"{subject} failed ({code})")


@dataclass(frozen=True, slots=True)
class _SecretSource:
    kind: str
    locator: str


@dataclass(frozen=True, slots=True)
class _CredentialRoute:
    match: str
    username: str
    secret: str


@dataclass(slots=True)
class HostSecretSession:
    """Own lazy Secret snapshots for the lifetime of one host command."""

    _sources: dict[str, _SecretSource]
    _routes: tuple[_CredentialRoute, ...]
    _root: Path | None = None
    _owns_root: bool = True
    _known_warning_names: set[str] = field(default_factory=set)
    _pending_warnings: list[Diagnostic] = field(default_factory=list)

    @classmethod
    def from_configuration(cls, result: ConfigurationResult) -> HostSecretSession:
        """Create an inactive lazy session from one validated configuration."""
        sources: dict[str, _SecretSource] = {}
        for name, source in result.config.secrets.items():
            if source.env is not None:
                sources[name] = _SecretSource("env", source.env)
            elif source.file is not None:
                sources[name] = _SecretSource(
                    "file",
                    _absolute_secret_path(result.secret_file_base, source.file),
                )
            else:  # pragma: no cover - final validation proves one source
                raise HostSecretSessionError("invalid_metadata", name)
        routes = tuple(
            _CredentialRoute(
                match=canonicalize_git_credential_context(route.match),
                username=route.username,
                secret=route.password.secret,
            )
            for route in result.config.cdh.git.credentials
        )
        return cls(sources, routes)

    @classmethod
    def _attach(cls, root: Path) -> HostSecretSession:
        """Attach one helper process to an existing command session."""
        try:
            document = json.loads((root / _METADATA_FILE).read_bytes())
            sources = {
                name: _SecretSource(value["kind"], value["locator"])
                for name, value in document["sources"].items()
            }
            routes = tuple(
                _CredentialRoute(item["match"], item["username"], item["secret"])
                for item in document["routes"]
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            raise HostSecretSessionError("invalid_metadata") from None
        return cls(sources, routes, _root=root, _owns_root=False)

    def __enter__(self) -> HostSecretSession:
        if self._root is not None:
            raise HostSecretSessionError("invalid_lifecycle")
        try:
            root = Path(tempfile.mkdtemp(prefix="cdh-secret-session-"))
        except OSError:
            raise HostSecretSessionError("session_create_failed") from None
        try:
            root.chmod(0o700)
            self._root = root
            _write_private_file(root / _METADATA_FILE, self._metadata_bytes())
        except (OSError, ValueError):
            self._root = None
            shutil.rmtree(root, ignore_errors=True)
            raise HostSecretSessionError("session_create_failed") from None
        except BaseException:
            self._root = None
            shutil.rmtree(root, ignore_errors=True)
            raise
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        root = self._root
        if root is None or not self._owns_root:
            return
        self._collect_warning_markers()
        self._root = None
        try:
            shutil.rmtree(root)
        except OSError:
            if _exc_type is None:
                raise HostSecretSessionError("cleanup_failed") from None

    @property
    def root(self) -> Path:
        """Return the active private session root."""
        return self._require_root()

    def git_binding(self) -> GitCredentialProcessBinding | None:
        """Return fixed cdh-only Git credential configuration when routes exist."""
        if not self._routes:
            return None
        root = self._require_root()
        helper = (
            f"!exec {shlex.quote(sys.executable)} "
            "-m comfyui_docker_helper.host.git_credential_helper"
        )
        return GitCredentialProcessBinding(
            config_args=git_credential_config_args(helper),
            environment={GIT_CREDENTIAL_SESSION_ENV: os.fspath(root)},
        )

    def snapshot(self, name: str) -> Path:
        """Resolve and cache one logical Secret as a private regular file."""
        root = self._require_root()
        source = self._sources.get(name)
        if source is None:
            raise HostSecretSessionError("unknown_secret", name)
        snapshot = root / f"{_SNAPSHOT_PREFIX}{name}"
        lock_path = root / f"{_LOCK_PREFIX}{name}"
        failure_path = root / f"{_FAILURE_PREFIX}{name}"
        lock_fd = -1
        try:
            lock_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(lock_fd, 0o600)
        except OSError:
            if lock_fd >= 0:
                with suppress(OSError):
                    os.close(lock_fd)
            raise HostSecretSessionError("snapshot_failed", name) from None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if snapshot.exists():
                return snapshot
            cached_failure = _read_cached_failure(failure_path, name)
            if cached_failure is not None:
                raise cached_failure
            try:
                data, mode = self._read_source(name, source)
                if mode is not None and stat.S_IMODE(mode) & 0o077:
                    self._record_mode_warning(name)
                _validate_git_password(data, name)
                _write_snapshot(snapshot, data)
                return snapshot
            except HostSecretSessionError as error:
                _record_cached_failure(failure_path, error)
                raise
            except OSError:
                error = HostSecretSessionError("snapshot_failed", name)
                _record_cached_failure(failure_path, error)
                raise error from None
        except OSError:
            raise HostSecretSessionError("snapshot_failed", name) from None
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def snapshot_git_credential(self, secret_id: str) -> Path:
        """Resolve one accepted BuildPlan credential ID through this session."""
        for route in self._routes:
            if git_credential_secret_id(route.secret) == secret_id:
                return self.snapshot(route.secret)
        raise HostSecretSessionError("unknown_credential_secret")

    def drain_warnings(self) -> tuple[Diagnostic, ...]:
        """Return newly recorded content-free warnings in stable route-name order."""
        self._collect_warning_markers()
        warnings = tuple(self._pending_warnings)
        self._pending_warnings.clear()
        return warnings

    def helper_routes(
        self,
    ) -> tuple[tuple[GitCredentialRuntimeRoute, str], ...]:
        """Return runtime routes paired with their logical Secret names."""
        return tuple(
            (
                GitCredentialRuntimeRoute(
                    context=parse_git_credential_context(route.match),
                    username=route.username.encode("utf-8"),
                ),
                route.secret,
            )
            for route in self._routes
        )

    def _metadata_bytes(self) -> bytes:
        document = {
            "sources": {
                name: {"kind": source.kind, "locator": source.locator}
                for name, source in self._sources.items()
            },
            "routes": [
                {
                    "match": route.match,
                    "username": route.username,
                    "secret": route.secret,
                }
                for route in self._routes
            ],
        }
        return (
            json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    def _read_source(
        self, name: str, source: _SecretSource
    ) -> tuple[bytes, int | None]:
        if source.kind == "env":
            try:
                data = os.environb.get(source.locator.encode("ascii"))
            except (AttributeError, UnicodeEncodeError):
                raise HostSecretSessionError("environment_unavailable", name) from None
            if data is None:
                raise HostSecretSessionError("source_unavailable", name)
            return data, None
        if source.kind == "file":
            try:
                admitted = read_bounded_regular_absolute_file(
                    source.locator,
                    max_bytes=GIT_CREDENTIAL_VALUE_MAX_BYTES,
                )
            except (OSError, ValueError):
                raise HostSecretSessionError("source_unavailable", name) from None
            return admitted.data, admitted.mode
        raise HostSecretSessionError("invalid_metadata", name)

    def _record_mode_warning(self, name: str) -> None:
        root = self._require_root()
        marker = root / f"{_WARNING_PREFIX}{name}"
        if marker.exists():
            return
        try:
            _write_private_file(marker, b"")
        except FileExistsError:
            pass
        except OSError:
            raise HostSecretSessionError("warning_record_failed", name) from None

    def _collect_warning_markers(self) -> None:
        root = self._root
        if root is None:
            return
        for name in self._sources:
            if name in self._known_warning_names:
                continue
            if not (root / f"{_WARNING_PREFIX}{name}").is_file():
                continue
            self._known_warning_names.add(name)
            self._pending_warnings.append(
                Diagnostic(
                    ("secrets", name, "file"),
                    "secret.permissive_file_mode",
                    "Secret file has group or world permission bits set",
                    DiagnosticSeverity.WARNING,
                )
            )

    def _require_root(self) -> Path:
        if self._root is None:
            raise HostSecretSessionError("inactive")
        return self._root


def _absolute_secret_path(base: Path, locator: str) -> str:
    value = (
        locator if os.path.isabs(locator) else os.path.join(os.fspath(base), locator)
    )
    return os.path.abspath(os.path.normpath(value))


def _validate_git_password(value: bytes, name: str) -> None:
    if (
        not value
        or len(value) > GIT_CREDENTIAL_VALUE_MAX_BYTES
        or any(character in value for character in b"\0\r\n")
    ):
        raise HostSecretSessionError("invalid_value", name)


def _read_cached_failure(path: Path, name: str) -> HostSecretSessionError | None:
    if not path.exists():
        return None
    try:
        code = path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise HostSecretSessionError("invalid_metadata", name) from None
    if not code or any(
        character not in "abcdefghijklmnopqrstuvwxyz_" for character in code
    ):
        raise HostSecretSessionError("invalid_metadata", name)
    return HostSecretSessionError(code, name)


def _record_cached_failure(path: Path, error: HostSecretSessionError) -> None:
    try:
        _write_private_file(path, error.code.encode("ascii"))
    except FileExistsError:
        return
    except (OSError, UnicodeEncodeError):
        raise HostSecretSessionError("snapshot_failed", error.secret_name) from None


def _write_snapshot(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("private write failed")
            view = view[written:]
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def _write_private_file(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("private write failed")
            view = view[written:]
    finally:
        os.close(descriptor)
