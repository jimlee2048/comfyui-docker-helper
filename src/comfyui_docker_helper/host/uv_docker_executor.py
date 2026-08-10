"""Private Docker execution boundary for exact uv-backed resolution."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Literal

from python_on_whales import Container, DockerClient, Image
from python_on_whales.client_config import ParsingError
from python_on_whales.exceptions import DockerException, NoSuchContainer, NoSuchImage

from comfyui_docker_helper.errors import ApplicationError
from comfyui_docker_helper.exact_ledger import UV_IMAGE_REPOSITORY
from comfyui_docker_helper.host.private_state import (
    create_private_directory,
    create_private_file,
)

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
_OWNER_LABEL = "comfyui-docker-helper.uv-operation"
_CONTAINER_PREFIX = "cdh-uv-resolver-"
_UV_PATH = "/usr/local/bin/uv"
_UV_VERSION_LABEL = "org.opencontainers.image.version"
_KEEPER_COMMAND = ("/usr/bin/sleep", "infinity")
_REQUIREMENTS_INPUT_PATH = "/tmp/requirements.in"
_PYTORCH_INPUT_PATH = "/tmp/pyproject.toml"
_PYTHON_INSTALL_ROOT = "/tmp/cdh-python"
_MAX_INPUT_BYTES = 1024 * 1024
_MAX_STDOUT_BYTES = 16 * 1024 * 1024
_MAX_STDERR_BYTES = 4 * 1024 * 1024
_UV_ENVIRONMENT = {
    "UV_NO_CONFIG": "1",
    "UV_NO_PROGRESS": "1",
    "UV_NO_CACHE": "1",
    "UV_PYTHON_INSTALL_DIR": _PYTHON_INSTALL_ROOT,
}


class UvDockerExecutorError(ApplicationError):
    """A bounded, user-facing uv resolver container failure."""

    def __init__(
        self,
        message: str,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(message)


class UvImageEvidenceError(UvDockerExecutorError):
    """An inspected uv image does not match its required exact evidence."""


@dataclass(frozen=True, slots=True)
class UvResolverDescriptor:
    """Exact official uv provider identity accepted by the executor."""

    digest: str
    platform: Literal["linux/amd64"] = "linux/amd64"

    def __post_init__(self) -> None:
        if _DIGEST_PATTERN.fullmatch(self.digest) is None:
            raise ValueError("uv resolver digest must be an exact sha256 descriptor")

    @property
    def image_reference(self) -> str:
        return f"{UV_IMAGE_REPOSITORY}@{self.digest}"


@dataclass(frozen=True, slots=True)
class ManagedPythonCatalogOperation:
    """Request the managed-Python catalog for one exact target version."""

    python_version: str

    def __post_init__(self) -> None:
        _validate_python_version(self.python_version)


@dataclass(frozen=True, slots=True)
class RequirementsCompileOperation:
    """Compile one ordinary direct-requirement group for an exact target."""

    python_version: str
    index_url: str
    requirements: bytes

    def __post_init__(self) -> None:
        _validate_python_version(self.python_version)
        _validate_input(self.requirements)
        if not self.index_url.startswith(("http://", "https://")):
            raise ValueError("Python index URL must use HTTP or HTTPS")


@dataclass(frozen=True, slots=True)
class PyTorchCompileOperation:
    """Compile one generated PyTorch routing project for an exact target."""

    python_version: str
    pyproject: bytes

    def __post_init__(self) -> None:
        _validate_python_version(self.python_version)
        _validate_input(self.pyproject)


type UvResolverOperation = (
    ManagedPythonCatalogOperation
    | RequirementsCompileOperation
    | PyTorchCompileOperation
)


@dataclass(frozen=True, slots=True)
class UvResolverResult:
    """Bounded stdout result and diagnostic stream from one exact operation."""

    stdout: bytes
    stderr: bytes


class UvDockerExecutor:
    """Execute only the three current uv resolver operation families."""

    def execute(
        self,
        descriptor: UvResolverDescriptor,
        operation: UvResolverOperation,
    ) -> UvResolverResult:
        if threading.current_thread() is not threading.main_thread():
            raise UvDockerExecutorError(
                "uv resolver execution requires the main thread"
            )
        identity = _OwnedContainerIdentity.allocate()
        client: DockerClient | None = None
        creation_attempted = False
        result: UvResolverResult | None = None
        primary: BaseException | None = None

        try:
            with _operation_signal_scope():
                client = DockerClient()
                image = _ensure_exact_image(client, descriptor)
                creation_attempted = True
                container = _create_owned_container(client, image, descriptor, identity)
                controlled_input = _operation_input(operation)
                if controlled_input is not None:
                    destination, input_bytes = controlled_input
                    _copy_controlled_input(client, container, destination, input_bytes)
                container.start()
                _run_fixed_step(
                    container,
                    (
                        _UV_PATH,
                        "--no-config",
                        "python",
                        "install",
                        "--managed-python",
                        "--no-cache",
                        "--no-bin",
                        "--install-dir",
                        _PYTHON_INSTALL_ROOT,
                        operation.python_version,
                    ),
                    canonical_stdout=False,
                )
                result = _run_fixed_step(
                    container,
                    _resolver_argv(operation),
                    canonical_stdout=True,
                    workdir="/tmp",
                )
        except BaseException as error:
            primary = error

        cleanup_identity = (
            _remove_exact_owned_container(client, identity)
            if client is not None and creation_attempted
            else None
        )
        if primary is not None:
            if cleanup_identity is not None:
                message = f"cleanup_incomplete: {cleanup_identity}"
                if isinstance(primary, (KeyboardInterrupt, SystemExit)):
                    raise UvDockerExecutorError(
                        f"uv resolver cancelled; {message}"
                    ) from primary
                stdout = (
                    primary.stdout
                    if isinstance(primary, UvDockerExecutorError)
                    else b""
                )
                stderr = (
                    primary.stderr
                    if isinstance(primary, UvDockerExecutorError)
                    else b""
                )
                primary_message = (
                    str(primary)
                    if isinstance(primary, UvDockerExecutorError)
                    else "uv resolver container operation failed"
                )
                raise UvDockerExecutorError(
                    f"{primary_message}; {message}",
                    stdout=stdout,
                    stderr=stderr,
                ) from primary
            if isinstance(primary, (KeyboardInterrupt, SystemExit)):
                raise primary.with_traceback(primary.__traceback__)
            if isinstance(primary, UvDockerExecutorError):
                raise primary.with_traceback(primary.__traceback__)
            raise UvDockerExecutorError(
                "uv resolver container operation failed"
            ) from primary
        if cleanup_identity is not None:
            raise UvDockerExecutorError(f"cleanup_incomplete: {cleanup_identity}")
        if result is None:  # pragma: no cover - all branches assign or raise
            raise AssertionError("uv resolver operation produced no result")
        return result


def uv_image_version_label(descriptor: UvResolverDescriptor) -> str | None:
    """Ensure one exact uv image and return its unparsed version label."""
    try:
        image = _ensure_exact_image(DockerClient(), descriptor)
        config = image.config
        labels = config.labels if config is not None else None
        if not isinstance(labels, Mapping):
            return None
        value = labels.get(_UV_VERSION_LABEL)
        return value if isinstance(value, str) else None
    except UvImageEvidenceError:
        raise
    except UvDockerExecutorError:
        raise
    except (DockerException, ParsingError, json.JSONDecodeError, OSError) as error:
        raise UvDockerExecutorError("uv resolver image operation failed") from error


@dataclass(frozen=True, slots=True)
class _OwnedContainerIdentity:
    name: str
    label_value: str

    @classmethod
    def allocate(cls) -> _OwnedContainerIdentity:
        token = uuid.uuid4().hex
        return cls(f"{_CONTAINER_PREFIX}{token}", token)

    @property
    def diagnostic(self) -> str:
        return f"name={self.name}, label={_OWNER_LABEL}={self.label_value}"


def _validate_python_version(version: str) -> None:
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("target Python must be an exact stable version")


def _validate_input(content: bytes) -> None:
    if not content or len(content) > _MAX_INPUT_BYTES:
        raise ValueError("resolver input must be non-empty and within the size limit")


def _ensure_exact_image(
    client: DockerClient,
    descriptor: UvResolverDescriptor,
) -> Image:
    reference = descriptor.image_reference
    try:
        image = client.image.inspect(reference)
    except NoSuchImage:
        client.pull(reference, platform=descriptor.platform)
        image = client.image.inspect(reference)
    if (
        image.os != "linux"
        or image.architecture != "amd64"
        or f"{UV_IMAGE_REPOSITORY}@{descriptor.digest}"
        not in (image.repo_digests or ())
    ):
        raise UvImageEvidenceError(
            "uv resolver image does not match the exact descriptor and platform"
        )
    return image


def _create_owned_container(
    client: DockerClient,
    image: Image,
    descriptor: UvResolverDescriptor,
    identity: _OwnedContainerIdentity,
) -> Container:
    labels = {_OWNER_LABEL: identity.label_value}
    try:
        client.container.create(
            image,
            _KEEPER_COMMAND,
            name=identity.name,
            labels=labels,
            platform=descriptor.platform,
            pull="never",
            tty=False,
            interactive=False,
            remove=False,
        )
    except (DockerException, OSError):
        recovered = _inspect_owned_container(client, identity)
        if recovered is None:
            raise
        return recovered
    recovered = _inspect_owned_container(client, identity)
    if recovered is None:  # pragma: no cover - daemon contract violation
        raise UvDockerExecutorError("created uv resolver container was not found")
    return recovered


def _inspect_owned_container(
    client: DockerClient,
    identity: _OwnedContainerIdentity,
) -> Container | None:
    try:
        container = client.container.inspect(identity.name)
    except NoSuchContainer:
        return None
    labels = container.config.labels or {}
    if (
        container.name != identity.name
        or labels.get(_OWNER_LABEL) != identity.label_value
    ):
        raise UvDockerExecutorError(
            f"uv resolver container ownership mismatch: {identity.diagnostic}"
        )
    return container


def _copy_controlled_input(
    client: DockerClient,
    container: Container,
    destination: str,
    content: bytes,
) -> None:
    root = create_private_directory(prefix="cdh-uv-input-")
    source = root / "input"
    descriptor: int | None = None
    primary: BaseException | None = None
    try:
        descriptor = create_private_file(source)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
        mode = Path(source).lstat().st_mode
        if stat.S_ISREG(mode) is False or stat.S_ISLNK(mode):
            raise UvDockerExecutorError(
                "controlled resolver input is not a regular file"
            )
        client.container.copy(Path(source), (container, destination))
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_error: OSError | None = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_error = error
        try:
            source.unlink(missing_ok=True)
        except OSError as error:
            if cleanup_error is None:
                cleanup_error = error
        try:
            root.rmdir()
        except OSError as error:
            if cleanup_error is None:
                cleanup_error = error
        if primary is None and cleanup_error is not None:
            raise UvDockerExecutorError(
                "controlled resolver input cleanup failed"
            ) from cleanup_error


def _operation_input(operation: UvResolverOperation) -> tuple[str, bytes] | None:
    if isinstance(operation, RequirementsCompileOperation):
        return _REQUIREMENTS_INPUT_PATH, operation.requirements
    if isinstance(operation, PyTorchCompileOperation):
        return _PYTORCH_INPUT_PATH, operation.pyproject
    return None


def _resolver_argv(operation: UvResolverOperation) -> tuple[str, ...]:
    common = (_UV_PATH, "--no-config")
    if isinstance(operation, ManagedPythonCatalogOperation):
        return (
            *common,
            "python",
            "list",
            "--only-downloads",
            "--all-versions",
            "--all-platforms",
            "--all-arches",
            "--show-urls",
            "--output-format",
            "json",
            operation.python_version,
        )
    if isinstance(operation, RequirementsCompileOperation):
        return (
            *common,
            "pip",
            "compile",
            _REQUIREMENTS_INPUT_PATH,
            "--no-header",
            "--no-annotate",
            "--no-strip-extras",
            "--python-version",
            operation.python_version,
            "--python-platform",
            "x86_64-unknown-linux-gnu",
            "--default-index",
            operation.index_url,
            "--resolution",
            "highest",
            "--prerelease",
            "disallow",
            "--no-sources",
            "--no-python-downloads",
            "--color",
            "never",
        )
    return (
        *common,
        "pip",
        "compile",
        _PYTORCH_INPUT_PATH,
        "--format",
        "pylock.toml",
        "--no-header",
        "--python-version",
        operation.python_version,
        "--python-platform",
        "x86_64-unknown-linux-gnu",
        "--resolution",
        "highest",
        "--prerelease",
        "disallow",
        "--no-python-downloads",
        "--color",
        "never",
        "--project",
        "/tmp",
    )


def _run_fixed_step(
    container: Container,
    argv: tuple[str, ...],
    *,
    canonical_stdout: bool,
    workdir: str | None = None,
) -> UvResolverResult:
    try:
        chunks = container.execute(
            argv,
            envs=_UV_ENVIRONMENT,
            workdir=workdir,
            stream=True,
            tty=False,
            interactive=False,
        )
        if chunks is None or isinstance(chunks, str):  # pragma: no cover - API contract
            raise UvDockerExecutorError("uv resolver did not provide a byte stream")
        stdout, stderr = _drain_bounded_streams(chunks)
    except KeyboardInterrupt:
        raise
    except DockerException as error:
        raise UvDockerExecutorError("uv resolver command failed") from error
    if canonical_stdout is False:
        stdout = b""
    return UvResolverResult(stdout, stderr)


def _drain_bounded_streams(
    chunks: Iterable[tuple[str, bytes]],
) -> tuple[bytes, bytes]:
    stdout = bytearray()
    stderr = bytearray()
    try:
        for channel, content in chunks:
            if channel == "stdout":
                target, limit = stdout, _MAX_STDOUT_BYTES
            elif channel == "stderr":
                target, limit = stderr, _MAX_STDERR_BYTES
            else:
                raise UvDockerExecutorError(
                    "uv resolver returned an unknown stream",
                    stdout=bytes(stdout),
                    stderr=bytes(stderr),
                )
            if len(target) + len(content) > limit:
                raise UvDockerExecutorError(
                    f"uv resolver {channel} exceeded the supported size",
                    stdout=bytes(stdout),
                    stderr=bytes(stderr),
                )
            target.extend(content)
    except DockerException as error:
        raise UvDockerExecutorError(
            "uv resolver command failed",
            stdout=bytes(stdout),
            stderr=bytes(stderr),
        ) from error
    return bytes(stdout), bytes(stderr)


def _remove_exact_owned_container(
    client: DockerClient,
    identity: _OwnedContainerIdentity,
) -> str | None:
    try:
        container = _inspect_owned_container(client, identity)
    except (DockerException, OSError, UvDockerExecutorError):
        return identity.diagnostic
    if container is None:
        return None
    try:
        client.container.remove(container, force=True)
        return None
    except (DockerException, OSError):
        try:
            remaining = _inspect_owned_container(client, identity)
        except (DockerException, OSError, UvDockerExecutorError):
            return identity.diagnostic
        if remaining is None:
            return None
        try:
            client.container.remove(remaining, force=True)
        except (DockerException, OSError):
            try:
                if _inspect_owned_container(client, identity) is None:
                    return None
            except (DockerException, OSError, UvDockerExecutorError):
                pass
            return identity.diagnostic
        return None


@contextmanager
def _operation_signal_scope() -> Iterator[None]:
    previous: dict[signal.Signals, signal.Handlers] = {}

    def cancel(signum: int, frame: FrameType | None) -> None:
        del frame
        raise KeyboardInterrupt(f"uv resolver cancelled by signal {signum}")

    try:
        for selected in (signal.SIGINT, signal.SIGTERM):
            previous[selected] = signal.getsignal(selected)
            signal.signal(selected, cancel)
        yield
    finally:
        for selected, handler in previous.items():
            signal.signal(selected, handler)
