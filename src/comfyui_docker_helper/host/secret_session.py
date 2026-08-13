"""Command-scoped host Secret acquisition and consumer-specific snapshots."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from comfyui_docker_helper.config.credential_secrets import (
    CREDENTIAL_SECRET_MAX_BYTES,
    BearerTokenError,
    downloader_credential_secret_id,
    validate_bearer_token,
)
from comfyui_docker_helper.config.diagnostics import Diagnostic, DiagnosticSeverity
from comfyui_docker_helper.config.git_credentials import (
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
from comfyui_docker_helper.host.descriptor_lock import (
    acquire_descriptor_lock,
    release_descriptor_lock,
)
from comfyui_docker_helper.host.git_credential_process import (
    GitCredentialProcessBinding,
    git_credential_helper_command,
)
from comfyui_docker_helper.host.private_state import (
    create_private_directory,
    create_private_file,
    open_private_lock_file,
)

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
_PRIVATE_FILE_ATTEMPTS = 128

_close_descriptor = os.close
_close_lock_descriptor = os.close
_platform_name = os.name


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
    _known_warning_markers: set[str] = field(default_factory=set)
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
            root = create_private_directory(prefix="cdh-secret-session-")
        except (OSError, ValueError):
            raise HostSecretSessionError("session_create_failed") from None
        try:
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
            self._pending_warnings.append(
                Diagnostic(
                    ("secrets",),
                    "secret.cleanup_failed",
                    "Host Secret session cleanup failed; temporary files may remain",
                    DiagnosticSeverity.WARNING,
                )
            )

    @property
    def root(self) -> Path:
        """Return the active private session root."""
        return self._require_root()

    def git_binding(self) -> GitCredentialProcessBinding | None:
        """Return fixed cdh-only Git credential configuration when routes exist."""
        if not self._routes:
            return None
        root = self._require_root()
        helper = git_credential_helper_command(sys.executable)
        return GitCredentialProcessBinding(
            config_args=git_credential_config_args(helper),
            environment={GIT_CREDENTIAL_SESSION_ENV: os.fspath(root)},
        )

    def snapshot_git_password(self, name: str) -> Path:
        """Resolve one logical Secret and admit it as a Git password."""
        snapshot = self.raw_snapshot(name)
        try:
            data = snapshot.read_bytes()
        except OSError:
            raise HostSecretSessionError("snapshot_failed", name) from None
        _validate_git_password(data, name)
        return snapshot

    def raw_snapshot(self, name: str) -> Path:
        """Resolve and cache one logical Secret without consumer validation."""
        root = self._require_root()
        source = self._sources.get(name)
        if source is None:
            raise HostSecretSessionError("unknown_secret", name)
        snapshot = root / f"{_SNAPSHOT_PREFIX}{name}"
        lock_path = root / f"{_LOCK_PREFIX}{name}"
        failure_path = root / f"{_FAILURE_PREFIX}{name}"
        with _snapshot_lock(lock_path, name):
            try:
                if snapshot.exists():
                    return snapshot
                cached_failure = _read_cached_failure(failure_path, name)
                if cached_failure is not None:
                    raise cached_failure
                try:
                    data, mode = self._read_source(name, source)
                    if mode is not None and stat.S_IMODE(mode) & 0o077:
                        self._record_mode_warning(name)
                    _write_snapshot(snapshot, data)
                    return snapshot
                except HostSecretSessionError as error:
                    _record_cached_failure(failure_path, error)
                    raise
                except Exception:
                    error = HostSecretSessionError("snapshot_failed", name)
                    _record_cached_failure(failure_path, error)
                    raise error from None
            except HostSecretSessionError:
                raise
            except Exception:
                raise HostSecretSessionError("snapshot_failed", name) from None

    def snapshot_git_credential(self, secret_id: str) -> Path:
        """Resolve one accepted BuildPlan credential ID through this session."""
        for route in self._routes:
            if git_credential_secret_id(route.secret) == secret_id:
                return self.snapshot_git_password(route.secret)
        raise HostSecretSessionError("unknown_credential_secret")

    def snapshot_downloader_credential(self, secret_id: str) -> Path:
        """Resolve one downloader credential ID and admit an exact Bearer token."""
        for name in self._sources:
            if downloader_credential_secret_id(name) != secret_id:
                continue
            snapshot = self.raw_snapshot(name)
            try:
                value = snapshot.read_bytes()
                validate_bearer_token(value)
            except BearerTokenError:
                raise HostSecretSessionError("invalid_bearer_value", name) from None
            except OSError:
                raise HostSecretSessionError("snapshot_failed", name) from None
            return snapshot
        raise HostSecretSessionError("unknown_credential_secret")

    def drain_warnings(self) -> tuple[Diagnostic, ...]:
        """Return newly recorded content-free warnings in stable recording order."""
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
            return _read_environment_source(source.locator, name), None
        if source.kind == "file":
            try:
                admitted = read_bounded_regular_absolute_file(
                    source.locator,
                    max_bytes=CREDENTIAL_SECRET_MAX_BYTES,
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
        except (OSError, ValueError):
            raise HostSecretSessionError("warning_record_failed", name) from None

    def _collect_warning_markers(self) -> None:
        root = self._root
        if root is None:
            return
        for name in self._sources:
            for marker_prefix, code, message in (
                (
                    _WARNING_PREFIX,
                    "secret.permissive_file_mode",
                    "Secret file has group or world permission bits set",
                ),
            ):
                marker = f"{marker_prefix}{name}"
                if marker in self._known_warning_markers:
                    continue
                if not (root / marker).is_file():
                    continue
                self._known_warning_markers.add(marker)
                self._pending_warnings.append(
                    Diagnostic(
                        ("secrets", name, "file"),
                        code,
                        message,
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
        or len(value) > CREDENTIAL_SECRET_MAX_BYTES
        or any(character in value for character in b"\0\r\n")
    ):
        raise HostSecretSessionError("invalid_value", name)


def _read_environment_source(locator: str, name: str) -> bytes:
    if _platform_name == "posix":
        environment = getattr(os, "environb", None)
        try:
            data = (
                None
                if environment is None
                else environment.get(locator.encode("ascii"))
            )
        except (AttributeError, OSError, UnicodeEncodeError):
            raise HostSecretSessionError("environment_unavailable", name) from None
    elif _platform_name == "nt":
        try:
            value = os.environ.get(locator)
            data = None if value is None else value.encode("utf-8")
        except (AttributeError, OSError, UnicodeEncodeError):
            raise HostSecretSessionError("environment_unavailable", name) from None
    else:
        raise HostSecretSessionError("environment_unavailable", name)
    if data is None:
        raise HostSecretSessionError("source_unavailable", name)
    if len(data) > CREDENTIAL_SECRET_MAX_BYTES:
        raise HostSecretSessionError("source_too_large", name)
    return data


@contextmanager
def _snapshot_lock(path: Path, name: str) -> Iterator[None]:
    descriptor = -1
    try:
        descriptor = open_private_lock_file(path)
        if not acquire_descriptor_lock(descriptor):
            raise OSError("private descriptor lock was not acquired")
    except BaseException as error:
        if descriptor >= 0:
            with suppress(BaseException):
                _close_lock_descriptor(descriptor)
        if isinstance(error, Exception):
            raise HostSecretSessionError("snapshot_failed", name) from None
        raise

    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            release_descriptor_lock(descriptor)
        except BaseException as error:
            cleanup_error = error
        try:
            _close_lock_descriptor(descriptor)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        # Body/acquire errors outrank unlock errors, which outrank close errors.
        if cleanup_error is not None and not body_failed:
            if isinstance(cleanup_error, Exception):
                raise HostSecretSessionError("snapshot_failed", name) from None
            raise cleanup_error


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
    temporary: Path | None = None
    for _attempt in range(_PRIVATE_FILE_ATTEMPTS):
        candidate = path.parent / f".private-{secrets.token_hex(16)}"
        try:
            _write_private_file(candidate, data)
        except FileExistsError:
            continue
        temporary = candidate
        break
    if temporary is None:
        raise FileExistsError("private file candidate space is unavailable")
    try:
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            os.unlink(temporary)


def _write_private_file(path: Path, data: bytes) -> None:
    descriptor = create_private_file(path)
    try:
        _write_descriptor(descriptor, data)
    except BaseException:
        with suppress(BaseException):
            _close_descriptor(descriptor)
        with suppress(OSError):
            os.unlink(path)
        raise
    try:
        _close_descriptor(descriptor)
    except BaseException:
        with suppress(OSError):
            os.unlink(path)
        raise


def _write_descriptor(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("private write failed")
        view = view[written:]
