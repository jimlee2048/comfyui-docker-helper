"""Tests for the private exact uv Docker execution boundary."""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from python_on_whales.exceptions import DockerException, NoSuchContainer, NoSuchImage

from comfyui_docker_helper.host.uv_docker_executor import (
    ManagedPythonCatalogOperation,
    PyTorchCompileOperation,
    RequirementsCompileOperation,
    UvDockerExecutor,
    UvDockerExecutorError,
    UvImageEvidenceError,
    UvResolverDescriptor,
    uv_image_version_label,
)

_DIGEST = f"sha256:{'1' * 64}"
_WORKER_JOIN_TIMEOUT_SECONDS = 10


class _FakeImage:
    def __init__(
        self,
        *,
        os_name: str = "linux",
        architecture: str = "amd64",
        repo_digests: tuple[str, ...] | None = (f"astral/uv@{_DIGEST}",),
        labels: object = None,
        config_error: BaseException | None = None,
    ) -> None:
        self.os = os_name
        self.architecture = architecture
        self.repo_digests = repo_digests
        self._labels = (
            {"org.opencontainers.image.version": "0.11.28-trixie-slim"}
            if labels is None
            else labels
        )
        self._config_error = config_error

    @property
    def config(self) -> object:
        if self._config_error is not None:
            raise self._config_error
        return SimpleNamespace(labels=self._labels)


class _FakeContainer:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.config = SimpleNamespace(labels=labels)
        self.started = False
        self.execute_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def start(self) -> None:
        self.started = True

    def execute(self, argv: tuple[str, ...], **kwargs: object):
        self.execute_calls.append((argv, kwargs))
        if len(self.execute_calls) == 1:
            yield "stderr", b"installed\n"
        else:
            yield "stdout", b'{"result":"exact"}\n'
            yield "stderr", b"diagnostic\n"


class _FakeImageApi:
    def __init__(
        self, *, cache_miss: bool = False, image: _FakeImage | None = None
    ) -> None:
        self.cache_miss = cache_miss
        self.image = image or _FakeImage()
        self.inspect_calls: list[str] = []
        self.pulls: list[tuple[str, str]] = []

    def inspect(self, reference: str) -> _FakeImage:
        self.inspect_calls.append(reference)
        if self.cache_miss and len(self.inspect_calls) == 1:
            raise NoSuchImage(["docker", "image", "inspect"], 1)
        return self.image


class _FakeContainerApi:
    def __init__(self) -> None:
        self.container: _FakeContainer | None = None
        self.create_calls: list[tuple[object, tuple[str, ...], dict[str, object]]] = []
        self.copy_calls: list[tuple[Path, object]] = []
        self.remove_calls: list[tuple[object, bool]] = []

    def create(
        self, image: object, command: tuple[str, ...], **kwargs: object
    ) -> _FakeContainer:
        self.create_calls.append((image, command, kwargs))
        self.container = _FakeContainer(
            str(kwargs["name"]),
            dict(kwargs["labels"]),  # type: ignore[arg-type]
        )
        return self.container

    def inspect(self, name: str) -> _FakeContainer:
        if self.container is None or self.container.name != name:
            raise NoSuchContainer(["docker", "container", "inspect"], 1)
        return self.container

    def copy(self, source: Path, destination: object) -> None:
        assert source.is_file()
        if os.name == "posix":
            assert source.stat().st_mode & 0o777 == 0o600
        self.copy_calls.append((source, destination))

    def remove(self, container: object, *, force: bool) -> None:
        self.remove_calls.append((container, force))
        self.container = None


class _FakeDockerClient:
    def __init__(
        self, *, cache_miss: bool = False, image: _FakeImage | None = None
    ) -> None:
        self.image = _FakeImageApi(cache_miss=cache_miss, image=image)
        self.container = _FakeContainerApi()

    def pull(self, reference: str, *, platform: str) -> _FakeImage:
        self.image.pulls.append((reference, platform))
        return self.image.image


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _FakeDockerClient) -> None:
    monkeypatch.setattr(
        "comfyui_docker_helper.host.uv_docker_executor.DockerClient",
        lambda: client,
    )


# The success block protects exact image/platform ownership, controlled input,
# fixed commands, independent streams, and exact terminal cleanup.
def test_requirements_compile_uses_one_exact_owned_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)

    result = UvDockerExecutor().execute(
        UvResolverDescriptor(_DIGEST),
        RequirementsCompileOperation(
            python_version="3.13.14",
            index_url="https://pypi.org/simple",
            requirements=b"requests>=2\n",
        ),
    )

    reference = f"astral/uv@{_DIGEST}"
    assert client.image.inspect_calls == [reference]
    assert client.image.pulls == []
    assert len(client.container.create_calls) == 1
    _, keeper, create = client.container.create_calls[0]
    assert keeper == ("/usr/bin/sleep", "infinity")
    assert create["platform"] == "linux/amd64"
    assert create["pull"] == "never"
    assert create["tty"] is False
    assert create["interactive"] is False
    assert len(client.container.copy_calls) == 1
    copied_source, copied_destination = client.container.copy_calls[0]
    assert copied_destination[1] == "/tmp/requirements.in"
    assert copied_source.exists() is False
    assert copied_source.parent.exists() is False
    assert result.stdout == b'{"result":"exact"}\n'
    assert result.stderr == b"diagnostic\n"
    assert len(client.container.remove_calls) == 1
    assert client.container.remove_calls[0][1] is True

    container = client.container.remove_calls[0][0]
    assert container.started is True
    install, resolve = container.execute_calls
    assert install[0][:5] == (
        "/usr/local/bin/uv",
        "--no-config",
        "python",
        "install",
        "--managed-python",
    )
    assert install[0][-1] == "3.13.14"
    assert resolve[0][:5] == (
        "/usr/local/bin/uv",
        "--no-config",
        "pip",
        "compile",
        "/tmp/requirements.in",
    )
    assert "--default-index" in resolve[0]
    assert resolve[0][resolve[0].index("--format") + 1] == "pylock.toml"
    # The bound uv version owns its compatible prerelease default.
    assert "--prerelease" not in resolve[0]
    assert resolve[1]["workdir"] == "/tmp"
    assert resolve[1]["tty"] is False
    assert resolve[1]["interactive"] is False
    assert resolve[1]["envs"] == {
        "UV_NO_CONFIG": "1",
        "UV_NO_PROGRESS": "1",
        "UV_NO_CACHE": "1",
        "UV_PYTHON_INSTALL_DIR": "/tmp/cdh-python",
    }


def test_cache_miss_pulls_and_reinspects_exact_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient(cache_miss=True)
    _install_client(monkeypatch, client)

    UvDockerExecutor().execute(
        UvResolverDescriptor(_DIGEST),
        ManagedPythonCatalogOperation("3.12.13"),
    )

    reference = f"astral/uv@{_DIGEST}"
    assert client.image.inspect_calls == [reference, reference]
    assert client.image.pulls == [(reference, "linux/amd64")]
    container = client.container.remove_calls[0][0]
    resolver_argv = container.execute_calls[1][0]
    assert resolver_argv[2:5] == ("python", "list", "--only-downloads")
    assert resolver_argv[-1] == "3.12.13"


def test_controlled_input_copy_failure_cleans_private_state_and_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)
    copied_sources: list[Path] = []

    def fail_copy(source: Path, destination: object) -> None:
        del destination
        assert source.is_file()
        copied_sources.append(source)
        raise DockerException(["docker", "container", "cp"], 1)

    client.container.copy = fail_copy  # type: ignore[method-assign]

    with pytest.raises(UvDockerExecutorError, match="container operation failed"):
        UvDockerExecutor().execute(
            UvResolverDescriptor(_DIGEST),
            RequirementsCompileOperation(
                python_version="3.13.14",
                index_url="https://pypi.org/simple",
                requirements=b"requests>=2\n",
            ),
        )

    assert len(copied_sources) == 1
    assert copied_sources[0].exists() is False
    assert copied_sources[0].parent.exists() is False
    assert len(client.container.remove_calls) == 1


def test_input_cancellation_outranks_cleanup_failures_and_attempts_every_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)
    root = Path("private-input-root")
    cleanup_calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        "comfyui_docker_helper.host.uv_docker_executor.create_private_directory",
        lambda **_kwargs: root,
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.host.uv_docker_executor.create_private_file",
        lambda _path: 91,
    )

    def cancel_fdopen(descriptor: int, mode: str) -> object:
        del descriptor, mode
        raise KeyboardInterrupt("cancelled while preparing resolver input")

    monkeypatch.setattr(os, "fdopen", cancel_fdopen)

    def fail_close(descriptor: int) -> None:
        cleanup_calls.append(("close", descriptor))
        raise OSError("close failed")

    def fail_unlink(path: Path, *, missing_ok: bool = False) -> None:
        cleanup_calls.append(("unlink", (path, missing_ok)))
        raise OSError("unlink failed")

    def fail_rmdir(path: Path) -> None:
        cleanup_calls.append(("rmdir", path))
        raise OSError("rmdir failed")

    monkeypatch.setattr(os, "close", fail_close)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    monkeypatch.setattr(Path, "rmdir", fail_rmdir)

    with pytest.raises(KeyboardInterrupt, match="cancelled while preparing"):
        UvDockerExecutor().execute(
            UvResolverDescriptor(_DIGEST),
            RequirementsCompileOperation(
                python_version="3.13.14",
                index_url="https://pypi.org/simple",
                requirements=b"requests>=2\n",
            ),
        )

    assert cleanup_calls == [
        ("close", 91),
        ("unlink", (root / "input", True)),
        ("rmdir", root),
    ]
    assert len(client.container.remove_calls) == 1


@pytest.mark.parametrize("cache_miss", [False, True])
def test_uv_image_version_label_reuses_exact_image_evidence(
    monkeypatch: pytest.MonkeyPatch,
    cache_miss: bool,
) -> None:
    client = _FakeDockerClient(cache_miss=cache_miss)
    _install_client(monkeypatch, client)
    descriptor = UvResolverDescriptor(_DIGEST)

    label = uv_image_version_label(descriptor)

    reference = f"astral/uv@{_DIGEST}"
    assert label == "0.11.28-trixie-slim"
    assert client.image.inspect_calls == (
        [reference, reference] if cache_miss else [reference]
    )
    assert client.image.pulls == ([(reference, "linux/amd64")] if cache_miss else [])


def test_uv_evidence_then_executor_does_not_pull_exact_image_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient(cache_miss=True)
    _install_client(monkeypatch, client)
    descriptor = UvResolverDescriptor(_DIGEST)

    assert uv_image_version_label(descriptor) == "0.11.28-trixie-slim"
    UvDockerExecutor().execute(
        descriptor,
        ManagedPythonCatalogOperation("3.12.13"),
    )

    reference = f"astral/uv@{_DIGEST}"
    assert client.image.inspect_calls == [reference, reference, reference]
    assert client.image.pulls == [(reference, "linux/amd64")]


@pytest.mark.parametrize("labels", [None, {}, {"unrelated": "value"}])
def test_uv_image_version_label_returns_none_without_string_evidence(
    monkeypatch: pytest.MonkeyPatch,
    labels: object,
) -> None:
    image = _FakeImage(labels=labels if labels is not None else [])
    client = _FakeDockerClient(image=image)
    _install_client(monkeypatch, client)

    assert uv_image_version_label(UvResolverDescriptor(_DIGEST)) is None


def test_uv_image_version_label_maps_complete_attribute_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient(image=_FakeImage(config_error=OSError("secret")))
    _install_client(monkeypatch, client)

    with pytest.raises(UvDockerExecutorError) as raised:
        uv_image_version_label(UvResolverDescriptor(_DIGEST))

    assert type(raised.value) is UvDockerExecutorError
    assert str(raised.value) == "uv resolver image operation failed"
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("os", "windows"),
        ("architecture", "arm64"),
        ("repo_digests", (f"registry.example.test/unrelated/image@{_DIGEST}",)),
    ],
)
def test_image_verification_rejects_wrong_descriptor_or_platform(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    value: object,
) -> None:
    image = _FakeImage()
    client = _FakeDockerClient(image=image)
    _install_client(monkeypatch, client)
    setattr(image, attribute, value)

    with pytest.raises(UvImageEvidenceError, match="exact descriptor"):
        uv_image_version_label(UvResolverDescriptor(_DIGEST))

    assert client.container.create_calls == []


def test_pytorch_operation_copies_project_and_requests_pylock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)

    UvDockerExecutor().execute(
        UvResolverDescriptor(_DIGEST),
        PyTorchCompileOperation("3.14.6", b"[project]\nname='resolver'\n"),
    )

    container = client.container.remove_calls[0][0]
    resolver_argv = container.execute_calls[1][0]
    assert resolver_argv[:5] == (
        "/usr/local/bin/uv",
        "--no-config",
        "pip",
        "compile",
        "/tmp/pyproject.toml",
    )
    assert resolver_argv[resolver_argv.index("--format") + 1] == "pylock.toml"
    assert "--prerelease" not in resolver_argv
    assert resolver_argv[-2:] == ("--project", "/tmp")


# Ambiguous lifecycle responses are recovered only through the preallocated
# exact name and matching ownership label, never a broad label search.
def test_ambiguous_create_recovers_exact_owned_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)
    original_create = client.container.create

    def create(
        image: object, command: tuple[str, ...], **kwargs: object
    ) -> _FakeContainer:
        original_create(image, command, **kwargs)
        raise DockerException(["docker", "container", "create"], 125)

    client.container.create = create  # type: ignore[method-assign]

    result = UvDockerExecutor().execute(
        UvResolverDescriptor(_DIGEST),
        ManagedPythonCatalogOperation("3.13.14"),
    )

    assert result.stdout == b'{"result":"exact"}\n'
    assert len(client.container.remove_calls) == 1


def test_ambiguous_create_rejects_foreign_owner_without_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)
    original_create = client.container.create

    def create(
        image: object, command: tuple[str, ...], **kwargs: object
    ) -> _FakeContainer:
        container = original_create(image, command, **kwargs)
        owner_label = next(iter(container.config.labels))
        container.config.labels = {owner_label: "foreign-owner"}
        raise DockerException(["docker", "container", "create"], 125)

    client.container.create = create  # type: ignore[method-assign]

    with pytest.raises(UvDockerExecutorError) as raised:
        UvDockerExecutor().execute(
            UvResolverDescriptor(_DIGEST),
            ManagedPythonCatalogOperation("3.13.14"),
        )

    assert "ownership mismatch" in str(raised.value)
    assert "cleanup_incomplete" in str(raised.value)
    assert client.container.remove_calls == []


def test_ambiguous_remove_accepts_verified_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)

    def remove(container: object, *, force: bool) -> None:
        del container, force
        client.container.container = None
        raise DockerException(["docker", "container", "rm"], 1)

    client.container.remove = remove  # type: ignore[method-assign]

    result = UvDockerExecutor().execute(
        UvResolverDescriptor(_DIGEST),
        ManagedPythonCatalogOperation("3.13.14"),
    )

    assert result.stdout == b'{"result":"exact"}\n'


# The failure block proves bounded incremental admission and keeps upstream
# exception stderr out of stable diagnostics while always cleaning exact state.
def test_stream_overflow_fails_closed_and_cleans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)
    monkeypatch.setattr(
        "comfyui_docker_helper.host.uv_docker_executor._MAX_STDOUT_BYTES", 3
    )

    with pytest.raises(UvDockerExecutorError, match="stdout exceeded"):
        UvDockerExecutor().execute(
            UvResolverDescriptor(_DIGEST),
            ManagedPythonCatalogOperation("3.13.14"),
        )

    assert len(client.container.remove_calls) == 1


def test_nonzero_command_preserves_generic_error_and_cleans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)

    def execute(argv: tuple[str, ...], **kwargs: object):
        del argv, kwargs
        yield "stderr", b"bounded diagnostic"
        raise DockerException(
            ["docker", "exec"],
            42,
            stderr=b"bounded diagnostic must not be repeated",
        )

    original_create = client.container.create

    def create(
        image: object, command: tuple[str, ...], **kwargs: object
    ) -> _FakeContainer:
        container = original_create(image, command, **kwargs)
        container.execute = execute  # type: ignore[method-assign]
        return container

    client.container.create = create  # type: ignore[method-assign]

    with pytest.raises(UvDockerExecutorError) as raised:
        UvDockerExecutor().execute(
            UvResolverDescriptor(_DIGEST),
            ManagedPythonCatalogOperation("3.13.14"),
        )

    assert str(raised.value) == "uv resolver command failed"
    assert "bounded diagnostic" not in str(raised.value)
    assert raised.value.stderr == b"bounded diagnostic"
    assert len(client.container.remove_calls) == 1


def test_keyboard_interrupt_restores_handlers_and_cleans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)
    selected_signals = (signal.SIGINT, signal.SIGTERM)
    previous = {selected: object() for selected in selected_signals}
    installed: dict[signal.Signals, object] = dict(previous)

    monkeypatch.setattr(signal, "getsignal", lambda selected: installed[selected])

    def install_handler(selected: signal.Signals, handler: object) -> object:
        prior = installed[selected]
        installed[selected] = handler
        return prior

    monkeypatch.setattr(signal, "signal", install_handler)

    original_create = client.container.create

    def create(
        image: object, command: tuple[str, ...], **kwargs: object
    ) -> _FakeContainer:
        container = original_create(image, command, **kwargs)

        def execute(argv: tuple[str, ...], **call_kwargs: object):
            del argv, call_kwargs
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)
            yield  # pragma: no cover

        container.execute = execute  # type: ignore[method-assign]
        return container

    client.container.create = create  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        UvDockerExecutor().execute(
            UvResolverDescriptor(_DIGEST),
            ManagedPythonCatalogOperation("3.13.14"),
        )

    assert installed == previous
    assert len(client.container.remove_calls) == 1


def test_cancellation_with_cleanup_failure_reports_controlled_owned_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)
    original_create = client.container.create

    def create(
        image: object, command: tuple[str, ...], **kwargs: object
    ) -> _FakeContainer:
        container = original_create(image, command, **kwargs)

        def execute(argv: tuple[str, ...], **call_kwargs: object):
            del argv, call_kwargs
            raise KeyboardInterrupt("upstream cancellation detail")
            yield  # pragma: no cover

        container.execute = execute  # type: ignore[method-assign]
        return container

    def remove(container: object, *, force: bool) -> None:
        del container, force
        raise DockerException(["docker", "container", "rm", "secret"], 1)

    client.container.create = create  # type: ignore[method-assign]
    client.container.remove = remove  # type: ignore[method-assign]

    with pytest.raises(UvDockerExecutorError) as raised:
        UvDockerExecutor().execute(
            UvResolverDescriptor(_DIGEST),
            ManagedPythonCatalogOperation("3.13.14"),
        )

    message = str(raised.value)
    assert message.startswith(
        "uv resolver cancelled; cleanup_incomplete: name=cdh-uv-resolver-"
    )
    assert "label=comfyui-docker-helper.uv-operation=" in message
    assert "upstream cancellation detail" not in message
    assert "secret" not in message


def test_cleanup_failure_reports_exact_owned_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)

    def remove(container: object, *, force: bool) -> None:
        del container, force
        raise DockerException(["docker", "container", "rm"], 1)

    client.container.remove = remove  # type: ignore[method-assign]

    with pytest.raises(UvDockerExecutorError) as raised:
        UvDockerExecutor().execute(
            UvResolverDescriptor(_DIGEST),
            ManagedPythonCatalogOperation("3.13.14"),
        )

    message = str(raised.value)
    assert message.startswith("cleanup_incomplete: name=cdh-uv-resolver-")
    assert "label=comfyui-docker-helper.uv-operation=" in message


def test_resolver_and_cleanup_failures_preserve_both_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    _install_client(monkeypatch, client)
    original_create = client.container.create

    def create(
        image: object, command: tuple[str, ...], **kwargs: object
    ) -> _FakeContainer:
        container = original_create(image, command, **kwargs)
        calls = 0

        def execute(argv: tuple[str, ...], **call_kwargs: object):
            nonlocal calls
            del argv, call_kwargs
            calls += 1
            if calls == 1:
                yield "stderr", b"install diagnostic"
                return
            yield "stdout", b"partial result"
            yield "stderr", b"resolver diagnostic"
            raise DockerException(["docker", "exec"], 42)

        container.execute = execute  # type: ignore[method-assign]
        return container

    def remove(container: object, *, force: bool) -> None:
        del container, force
        raise DockerException(["docker", "container", "rm"], 1)

    client.container.create = create  # type: ignore[method-assign]
    client.container.remove = remove  # type: ignore[method-assign]

    with pytest.raises(UvDockerExecutorError) as raised:
        UvDockerExecutor().execute(
            UvResolverDescriptor(_DIGEST),
            ManagedPythonCatalogOperation("3.13.14"),
        )

    message = str(raised.value)
    assert message.startswith(
        "uv resolver command failed; cleanup_incomplete: name=cdh-uv-resolver-"
    )
    assert "label=comfyui-docker-helper.uv-operation=" in message
    assert raised.value.stdout == b"partial result"
    assert raised.value.stderr == b"resolver diagnostic"


def test_worker_thread_rejects_before_docker_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = False

    def docker_client() -> object:
        nonlocal constructed
        constructed = True
        return object()

    monkeypatch.setattr(
        "comfyui_docker_helper.host.uv_docker_executor.DockerClient",
        docker_client,
    )
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            UvDockerExecutor().execute(
                UvResolverDescriptor(_DIGEST),
                ManagedPythonCatalogOperation("3.13.14"),
            )
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    worker.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
    if worker.is_alive():
        pytest.fail("uv Docker executor worker did not stop within timeout")

    assert len(failures) == 1
    assert isinstance(failures[0], UvDockerExecutorError)
    assert str(failures[0]) == "uv resolver execution requires the main thread"
    assert constructed is False


# Constructor validation prevents the private boundary from becoming an
# arbitrary image, target, index, or oversized-input execution surface.
@pytest.mark.parametrize(
    "operation",
    [
        ManagedPythonCatalogOperation,
        lambda value: RequirementsCompileOperation(
            value, "https://pypi.org/simple", b"requests\n"
        ),
        lambda value: PyTorchCompileOperation(value, b"[project]\n"),
    ],
)
def test_operations_require_exact_stable_python(
    operation: object,
) -> None:
    with pytest.raises(ValueError, match="exact stable"):
        operation("3.13")  # type: ignore[operator]


def test_descriptor_index_and_controlled_input_are_bounded() -> None:
    with pytest.raises(ValueError, match="sha256"):
        UvResolverDescriptor("sha256:bad")
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        RequirementsCompileOperation("3.13.14", "file:///tmp/index", b"requests\n")
    with pytest.raises(ValueError, match="size limit"):
        RequirementsCompileOperation("3.13.14", "https://pypi.org/simple", b"")
    with pytest.raises(ValueError, match="size limit"):
        RequirementsCompileOperation(
            "3.13.14",
            "https://pypi.org/simple",
            b"x" * (1024 * 1024 + 1),
        )
