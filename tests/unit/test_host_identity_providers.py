"""Exact identity-provider contracts."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

import httpx
import pytest
from docker.errors import APIError, DockerException

from comfyui_docker_helper import file_admission
from comfyui_docker_helper.exact_ledger import COMFYUI_REPOSITORY
from comfyui_docker_helper.host.git_credential_process import (
    GitCredentialProcessBinding,
)
from comfyui_docker_helper.host.identity_providers import (
    DirectGitIdentityRequest,
    DockerEngineOciIdentityProvider,
    DockerManagedPythonIdentityProvider,
    FilesystemLocalExecutableIdentityProvider,
    GitDirectIdentityProvider,
    GitOfficialComfyUIIdentityProvider,
    HttpRegistryNodeIdentityProvider,
    IdentityProviderError,
    ManagedPythonIdentityRequest,
    OciIdentityRequest,
    OfficialComfyUIIdentityRequest,
    ProviderFailureKind,
    RegistryNodeIdentityRequest,
    SelectorStability,
)
from comfyui_docker_helper.host.uv_docker_executor import (
    UvDockerExecutorError,
    UvImageEvidenceError,
    UvResolverResult,
)
from comfyui_docker_helper.local_executable import LocalExecutableIdentityRequest

INDEX_DIGEST_A = f"sha256:{'a' * 64}"
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


_MEDIA_TYPES = (
    ("application/vnd.docker.distribution.manifest.list.v2+json", "index"),
    ("application/vnd.oci.image.index.v1+json", "index"),
    ("application/vnd.docker.distribution.manifest.v2+json", "manifest"),
    ("application/vnd.oci.image.manifest.v1+json", "manifest"),
)


def _distribution_document(
    *,
    digest: object = INDEX_DIGEST_A,
    media_type: object = "application/vnd.oci.image.index.v1+json",
    platforms: object = None,
) -> object:
    return {
        "Descriptor": {
            "digest": digest,
            "mediaType": media_type,
            "size": "ignored",
            "future-field": {"ignored": True},
        },
        "Platforms": (
            [
                {"os": "linux", "architecture": "amd64", "variant": "ignored"},
                {"os": "linux", "architecture": "arm64"},
            ]
            if platforms is None
            else platforms
        ),
        "future-top-level": "ignored",
    }


class _FakeDistributionApi:
    def __init__(self, response: object, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[str] = []

    def inspect_distribution(self, reference: str) -> object:
        self.calls.append(reference)
        if self.error is not None:
            raise self.error
        return self.response


class _FakeDockerSdkClient:
    def __init__(self, response: object, error: BaseException | None = None) -> None:
        self.api = _FakeDistributionApi(response, error)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize(("media_type", "kind"), _MEDIA_TYPES)
def test_engine_oci_accepts_supported_descriptor_media_types(
    media_type: str, kind: str
) -> None:
    client = _FakeDockerSdkClient(_distribution_document(media_type=media_type))
    evidence_calls: list[object] = []
    provider = DockerEngineOciIdentityProvider(
        lambda: client,
        evidence_calls.append,  # type: ignore[arg-type]
    )

    identity = provider.resolve(
        OciIdentityRequest("cuda-base", "nvidia/cuda", "13.0.3-cudnn-devel-ubuntu24.04")
    )

    assert identity.descriptor_digest == INDEX_DIGEST_A
    assert identity.descriptor_kind == kind
    assert identity.platform == "linux/amd64"
    assert identity.resolved_version is None
    assert client.api.calls == ["nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04"]
    assert client.close_calls == 1
    assert evidence_calls == []


def test_engine_oci_ignores_unused_fields_and_duplicate_target_platforms() -> None:
    client = _FakeDockerSdkClient(
        _distribution_document(
            platforms=[
                {"os": "linux", "architecture": "amd64"},
                {"os": "windows", "architecture": "amd64"},
                {"os": "linux", "architecture": "amd64", "future": True},
                "ignored platform",
            ]
        )
    )

    identity = DockerEngineOciIdentityProvider(lambda: client).resolve(
        OciIdentityRequest("cuda-base", "nvidia/cuda", "exact")
    )

    assert identity.descriptor_digest == INDEX_DIGEST_A


@pytest.mark.parametrize(
    "oci_request",
    [
        OciIdentityRequest("cuda-base", "nvidia/cuda", ""),
        OciIdentityRequest("cuda-base", "bad:repository", "tag"),
        OciIdentityRequest("invalid", "nvidia/cuda", "tag"),  # type: ignore[arg-type]
        OciIdentityRequest(
            "cuda-base",
            "nvidia/cuda",
            "tag",
            "linux/arm64",  # type: ignore[arg-type]
        ),
    ],
)
def test_engine_oci_rejects_invalid_request_before_client_construction(
    oci_request: OciIdentityRequest,
) -> None:
    factory_calls = 0

    def factory() -> _FakeDockerSdkClient:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeDockerSdkClient(_distribution_document())

    provider = DockerEngineOciIdentityProvider(factory)  # type: ignore[arg-type]

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(oci_request)

    assert raised.value.kind is ProviderFailureKind.INVALID_REQUEST
    assert factory_calls == 0


@pytest.mark.parametrize(
    "document",
    [
        None,
        {},
        {"Descriptor": {}, "Platforms": []},
        _distribution_document(digest="sha256:not-canonical"),
        _distribution_document(media_type="application/octet-stream"),
        _distribution_document(platforms=[{"os": "linux", "architecture": "arm64"}]),
    ],
)
def test_engine_oci_rejects_missing_or_malformed_consumed_response_fields(
    document: object,
) -> None:
    provider = DockerEngineOciIdentityProvider(
        lambda: _FakeDockerSdkClient(document)  # type: ignore[arg-type]
    )

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(OciIdentityRequest("cuda-base", "nvidia/cuda", "tag"))

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


@pytest.mark.parametrize(
    "error",
    [
        DockerException("secret daemon failure"),
        APIError("secret not found", response=httpx.Response(404)),
        OSError("secret socket failure"),
    ],
)
def test_engine_oci_maps_sdk_and_engine_failures_to_coarse_network(
    error: BaseException,
) -> None:
    client = _FakeDockerSdkClient(_distribution_document(), error)
    provider = DockerEngineOciIdentityProvider(lambda: client)  # type: ignore[arg-type]

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(OciIdentityRequest("cuda-base", "nvidia/cuda", "missing"))

    assert raised.value.kind is ProviderFailureKind.NETWORK
    assert "secret" not in str(raised.value)
    assert client.api.calls == ["nvidia/cuda:missing"]
    assert client.close_calls == 1


def test_engine_oci_maps_client_factory_failure_without_cleanup() -> None:
    def factory() -> _FakeDockerSdkClient:
        raise OSError("secret connection failure")

    provider = DockerEngineOciIdentityProvider(factory)  # type: ignore[arg-type]

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(OciIdentityRequest("cuda-base", "nvidia/cuda", "tag"))

    assert raised.value.kind is ProviderFailureKind.NETWORK


@pytest.mark.parametrize(
    ("label", "resolved_version"),
    [
        ("0.11.28", "0.11.28"),
        ("0.11.28-trixie-slim", "0.11.28"),
    ],
)
def test_engine_oci_uv_uses_exact_image_label_evidence(
    label: str, resolved_version: str
) -> None:
    client = _FakeDockerSdkClient(_distribution_document())
    descriptors = []

    def evidence(descriptor):
        descriptors.append(descriptor)
        return label

    identity = DockerEngineOciIdentityProvider(lambda: client, evidence).resolve(
        OciIdentityRequest("uv-tool", "astral/uv", "latest")
    )

    assert identity.resolved_version == resolved_version
    assert descriptors[0].digest == INDEX_DIGEST_A
    assert descriptors[0].platform == "linux/amd64"


@pytest.mark.parametrize("label", [None, "", "v0.11.28", "0.11"])
def test_engine_oci_uv_rejects_missing_or_malformed_version_label(
    label: str | None,
) -> None:
    provider = DockerEngineOciIdentityProvider(
        lambda: _FakeDockerSdkClient(_distribution_document()),
        lambda descriptor: label,
    )

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(OciIdentityRequest("uv-tool", "astral/uv", "latest"))

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (UvImageEvidenceError("mismatch"), ProviderFailureKind.INVALID_RESPONSE),
        (UvDockerExecutorError("pull failed"), ProviderFailureKind.NETWORK),
    ],
)
def test_engine_oci_uv_maps_narrow_image_evidence_failures(
    error: UvDockerExecutorError, kind: ProviderFailureKind
) -> None:
    def evidence(descriptor):
        del descriptor
        raise error

    provider = DockerEngineOciIdentityProvider(
        lambda: _FakeDockerSdkClient(_distribution_document()), evidence
    )

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(OciIdentityRequest("uv-tool", "astral/uv", "latest"))

    assert raised.value.kind is kind


def _completed(stdout: str, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ("provider",), returncode, stdout, "secret stderr"
    )


# Managed-Python catalog rows must select one exact canonical target identity.
class _UvExecutor:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.calls: list[tuple[object, object]] = []

    def execute(self, descriptor, operation):
        self.calls.append((descriptor, operation))
        return UvResolverResult(self.stdout, b"")


class _FailingUvExecutor:
    def execute(self, descriptor, operation):
        del descriptor, operation
        raise UvDockerExecutorError(
            "uv resolver cancelled; cleanup_incomplete: "
            "name=cdh-uv-resolver-test, "
            "label=comfyui-docker-helper.uv-operation=test"
        )


@pytest.mark.parametrize("version", ["3.12.13", "3.13.14", "3.14.6"])
def test_managed_python_selects_exact_uv_catalog_identity(version: str) -> None:
    rows = [
        {
            "key": f"cpython-{version}-linux-x86_64-gnu",
            "version": version,
            "path": None,
            "url": f"https://releases.astral.sh/python/{version}.tar.gz",
            "os": "linux",
            "variant": "default",
            "implementation": "cpython",
            "arch": "x86_64",
            "libc": "gnu",
        },
        {
            "key": f"cpython-{version}-linux-aarch64-gnu",
            "version": version,
            "path": None,
            "url": f"https://releases.astral.sh/python/{version}-arm.tar.gz",
            "os": "linux",
            "variant": "default",
            "implementation": "cpython",
            "arch": "aarch64",
            "libc": "gnu",
        },
    ]
    executor = _UvExecutor(json.dumps(rows).encode())
    provider = DockerManagedPythonIdentityProvider(executor)
    request = ManagedPythonIdentityRequest(version, INDEX_DIGEST_A)

    identity = provider.resolve(request)

    assert request.stability is SelectorStability.EXACT
    assert identity.version == version
    assert identity.implementation == "cpython"
    assert identity.platform == "linux/amd64"
    assert identity.libc == "gnu"
    assert identity.provider == "uv-managed"
    assert identity.catalog_descriptor_digest == INDEX_DIGEST_A
    assert identity.catalog_key.endswith("linux-x86_64-gnu")
    descriptor, operation = executor.calls[0]
    assert descriptor.digest == INDEX_DIGEST_A
    assert descriptor.platform == "linux/amd64"
    assert operation.python_version == version


def test_managed_python_preserves_controlled_executor_cleanup_identity() -> None:
    provider = DockerManagedPythonIdentityProvider(_FailingUvExecutor())

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(ManagedPythonIdentityRequest("3.13.14", INDEX_DIGEST_A))

    assert raised.value.kind is ProviderFailureKind.NETWORK
    assert str(raised.value) == (
        "managed Python catalog: identity provider request failed: "
        "uv resolver cancelled; cleanup_incomplete: "
        "name=cdh-uv-resolver-test, "
        "label=comfyui-docker-helper.uv-operation=test"
    )


def test_managed_python_rejects_missing_and_duplicate_catalog_rows() -> None:
    request = ManagedPythonIdentityRequest("3.13.14", INDEX_DIGEST_A)
    provider = DockerManagedPythonIdentityProvider(_UvExecutor(b"[]"))
    with pytest.raises(IdentityProviderError) as missing:
        provider.resolve(request)
    assert missing.value.kind is ProviderFailureKind.NOT_FOUND

    row = {
        "key": "cpython-3.13.14-linux-x86_64-gnu",
        "version": "3.13.14",
        "path": None,
        "url": "https://releases.astral.sh/python.tar.gz",
        "os": "linux",
        "variant": "default",
        "implementation": "cpython",
        "arch": "x86_64",
        "libc": "gnu",
    }
    duplicate = DockerManagedPythonIdentityProvider(
        _UvExecutor(json.dumps([row, row]).encode())
    )
    with pytest.raises(IdentityProviderError) as ambiguous:
        duplicate.resolve(request)
    assert ambiguous.value.kind is ProviderFailureKind.INVALID_RESPONSE


@pytest.mark.parametrize(
    "row",
    [
        {
            "key": "cpython-bad-linux-x86_64-gnu",
            "version": "not-a-version",
            "path": None,
            "url": "https://releases.astral.sh/python.tar.gz",
            "os": "linux",
            "variant": "default",
            "implementation": "cpython",
            "arch": "x86_64",
            "libc": "gnu",
        },
        {
            "version": "3.13.14",
            "path": None,
            "url": "https://releases.astral.sh/python.tar.gz",
            "os": "linux",
            "variant": "default",
            "implementation": "cpython",
            "arch": "x86_64",
            "libc": "gnu",
        },
    ],
)
def test_managed_python_maps_malformed_catalog_rows_to_invalid_response(
    row: dict[str, object],
) -> None:
    provider = DockerManagedPythonIdentityProvider(
        _UvExecutor(json.dumps([row]).encode())
    )

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(ManagedPythonIdentityRequest("3.13.14", INDEX_DIGEST_A))

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


# Provider admission rejects catalog keys that could escape the managed root,
# while retaining safe opaque result identities.
@pytest.mark.parametrize(
    "catalog_key",
    ["", ".", "..", "../python", "python/key", "python\\key", " key", "key ", "key\n"],
)
def test_managed_python_maps_unsafe_catalog_key_to_invalid_response(
    catalog_key: str,
) -> None:
    version = "3.13.14"
    row = {
        "key": catalog_key,
        "version": version,
        "path": None,
        "url": "https://releases.astral.sh/python.tar.gz",
        "os": "linux",
        "variant": "default",
        "implementation": "cpython",
        "arch": "x86_64",
        "libc": "gnu",
    }
    provider = DockerManagedPythonIdentityProvider(
        _UvExecutor(json.dumps([row]).encode())
    )

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(ManagedPythonIdentityRequest(version, INDEX_DIGEST_A))

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


def test_managed_python_preserves_safe_opaque_catalog_key() -> None:
    version = "3.13.14"
    row = {
        "key": "alternate.catalog+key",
        "version": version,
        "path": None,
        "url": "https://releases.astral.sh/python.tar.gz",
        "os": "linux",
        "variant": "default",
        "implementation": "cpython",
        "arch": "x86_64",
        "libc": "gnu",
    }
    provider = DockerManagedPythonIdentityProvider(
        _UvExecutor(json.dumps([row]).encode())
    )

    identity = provider.resolve(ManagedPythonIdentityRequest(version, INDEX_DIGEST_A))

    assert identity.catalog_key == "alternate.catalog+key"


def test_managed_python_rejects_invalid_catalog_descriptor_before_uv() -> None:
    executor = _UvExecutor(b"[]")
    provider = DockerManagedPythonIdentityProvider(executor)
    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(ManagedPythonIdentityRequest("3.13.14", "sha256:bad"))

    assert raised.value.kind is ProviderFailureKind.INVALID_REQUEST
    assert executor.calls == []


def _git_runner(
    output: str, calls: list[tuple[Sequence[str], Mapping[str, object]]]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def runner(
        args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return _completed(output)

    return runner


# Official ComfyUI and direct-Git providers preserve ref, release, and ancestry
# identity.
def test_official_comfyui_resolves_peeled_release_commit_and_release_identity() -> None:
    calls: list[tuple[Sequence[str], Mapping[str, object]]] = []
    provider = GitOfficialComfyUIIdentityProvider(
        runner=_git_runner(
            f"{COMMIT_A}\trefs/tags/v0.4.0\n{COMMIT_B}\trefs/tags/v0.4.0^{{}}\n",
            calls,
        )
    )
    request = OfficialComfyUIIdentityRequest(COMFYUI_REPOSITORY, "refs/tags/v0.4.0")

    identity = provider.resolve(request)

    assert request.stability is SelectorStability.EXACT
    assert identity.repository == COMFYUI_REPOSITORY
    assert identity.commit == COMMIT_B
    assert identity.formal_release == "0.4.0"
    assert calls[0][0][1:3] == ("ls-remote", "--end-of-options")


def test_official_comfyui_release_catalog_returns_sorted_exact_identities() -> None:
    output = (
        f"{COMMIT_A}\trefs/tags/v0.5.0\n"
        f"{COMMIT_B}\trefs/tags/v0.4.0\n"
        f"{'3' * 40}\trefs/tags/v0.4.0^{{}}\n"
        f"{'4' * 40}\trefs/tags/v0.6.0-rc1\n"
    )
    calls: list[tuple[Sequence[str], Mapping[str, object]]] = []
    provider = GitOfficialComfyUIIdentityProvider(runner=_git_runner(output, calls))

    releases = provider.list_releases(COMFYUI_REPOSITORY)

    assert [item.formal_release for item in releases] == ["0.4.0", "0.5.0"]
    assert releases[0].commit == "3" * 40
    assert calls[0][0][1:4] == ("ls-remote", "--tags", "--end-of-options")


@pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
def test_official_comfyui_proves_ancestry_from_filtered_official_history(
    returncode: int, expected: bool
) -> None:
    calls: list[Sequence[str]] = []

    def runner(args: Sequence[str], **_kwargs: object):
        calls.append(args)
        return _completed("", returncode=(0 if len(calls) == 1 else returncode))

    provider = GitOfficialComfyUIIdentityProvider(runner=runner)

    assert provider.is_ancestor(COMFYUI_REPOSITORY, COMMIT_A, COMMIT_B) is expected
    assert calls[0][1:4] == ("clone", "--bare", "--filter=blob:none")
    assert calls[0][-3] == "--"
    assert calls[1][-4:] == (
        "merge-base",
        "--is-ancestor",
        COMMIT_A,
        COMMIT_B,
    )


def test_official_comfyui_rejects_unprovable_ancestry_response() -> None:
    calls = 0

    def runner(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        return _completed("", returncode=(0 if calls == 1 else 128))

    with pytest.raises(IdentityProviderError) as raised:
        GitOfficialComfyUIIdentityProvider(runner=runner).is_ancestor(
            COMFYUI_REPOSITORY, COMMIT_A, COMMIT_B
        )

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


def test_official_comfyui_rejects_non_official_repository_before_git() -> None:
    called = False

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return _completed("")

    with pytest.raises(IdentityProviderError) as raised:
        GitOfficialComfyUIIdentityProvider(runner=runner).resolve(
            OfficialComfyUIIdentityRequest(
                "https://example.test/fork/ComfyUI.git", "HEAD"
            )
        )

    assert raised.value.kind is ProviderFailureKind.INVALID_REQUEST
    assert called is False


def test_direct_git_resolves_moving_ref_to_full_commit() -> None:
    calls: list[tuple[Sequence[str], Mapping[str, object]]] = []
    request = DirectGitIdentityRequest("https://example.test/node.git", "main")
    provider = GitDirectIdentityProvider(
        runner=_git_runner(f"{COMMIT_A}\trefs/heads/main\n", calls)
    )

    identity = provider.resolve(request)

    assert request.stability is SelectorStability.MOVING
    assert identity.type == "git"
    assert identity.url == request.url
    assert identity.commit == COMMIT_A
    assert calls[0][0] == (
        "git",
        "ls-remote",
        "--end-of-options",
        request.url,
        "main",
    )


def test_direct_git_places_credential_config_before_ls_remote_and_adds_session_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Sequence[str], Mapping[str, object]]] = []
    request = DirectGitIdentityRequest("https://example.test/team/node.git", "main")
    binding = GitCredentialProcessBinding(
        config_args=(
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!cdh-private-helper",
            "-c",
            "credential.useHttpPath=true",
        ),
        environment={"CDH_GIT_CREDENTIAL_SESSION": "/private/session"},
    )
    monkeypatch.setenv("GIT_SSL_CAINFO", "/preserved/ca.pem")
    provider = GitDirectIdentityProvider(
        runner=_git_runner(f"{COMMIT_A}\trefs/heads/main\n", calls),
        credential_binding=binding,
    )

    identity = provider.resolve(request)

    assert identity.commit == COMMIT_A
    args, kwargs = calls[0]
    assert tuple(args) == (
        "git",
        *binding.config_args,
        "ls-remote",
        "--end-of-options",
        request.url,
        "main",
    )
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["CDH_GIT_CREDENTIAL_SESSION"] == "/private/session"
    assert environment["GIT_SSL_CAINFO"] == "/preserved/ca.pem"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_ASKPASS"] == ""


def test_direct_git_full_commit_request_is_classified_exact() -> None:
    request = DirectGitIdentityRequest("https://example.test/node.git", COMMIT_A)
    assert request.stability is SelectorStability.EXACT


# Registry catalogs preserve normalized published versions and exact requested identity.
def test_registry_confirms_exact_node_and_version_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/nodes/example-node/versions"
        return httpx.Response(
            200,
            request=request,
            json={
                "versions": [
                    {"node_id": "example-node", "version": "1.0.0"},
                    {"node_id": "example-node", "version": "2.0.0"},
                ]
            },
        )

    with _client(handler) as client:
        request = RegistryNodeIdentityRequest("example-node", "v2.0.0")
        identity = HttpRegistryNodeIdentityProvider(client).resolve(request)

    assert request.stability is SelectorStability.EXACT
    assert identity.type == "registry"
    assert identity.node_id == "example-node"
    assert identity.version == "2.0.0"


def test_registry_catalog_accepts_direct_list_response_and_sorts_versions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=[
                {"node_id": "example-node", "version": "2.0.0"},
                {"node_id": "example-node", "version": "1.0.0"},
            ],
        )

    with _client(handler) as client:
        identities = HttpRegistryNodeIdentityProvider(client).list_versions(
            "example-node"
        )

    assert [identity.version for identity in identities] == ["1.0.0", "2.0.0"]


def test_registry_preserves_normalized_semver_prerelease_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"versions": [{"node_id": "example-node", "version": "1.0.0-rc.1"}]},
        )

    with _client(handler) as client:
        identity = HttpRegistryNodeIdentityProvider(client).resolve(
            RegistryNodeIdentityRequest("example-node", "v1.0.0-rc.1")
        )

    assert identity.version == "1.0.0-rc.1"


def test_registry_maps_malformed_upstream_version_to_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "versions": [{"node_id": "example-node", "version": "not-a-version"}]
            },
        )

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpRegistryNodeIdentityProvider(client).list_versions("example-node")

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (404, ProviderFailureKind.NOT_FOUND),
        (401, ProviderFailureKind.AUTHENTICATION),
        (403, ProviderFailureKind.AUTHENTICATION),
        (429, ProviderFailureKind.RATE_LIMIT),
        (503, ProviderFailureKind.NETWORK),
    ],
)
# Provider errors remain typed, concise, deterministic, and free of source secrets.
def test_http_provider_status_errors_are_short_stable_and_secret_free(
    status: int, kind: ProviderFailureKind
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, text="token=super-secret")

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpRegistryNodeIdentityProvider(
            client, base_url="https://user:password@example.test"
        ).list_versions("example-node")

    assert raised.value.kind is kind
    rendered = str(raised.value)
    assert "super-secret" not in rendered
    assert "password" not in rendered
    assert "example-node" not in rendered


def test_http_provider_maps_network_and_invalid_payload() -> None:
    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("token=super-secret", request=request)

    with (
        _client(network_failure) as client,
        pytest.raises(IdentityProviderError) as network,
    ):
        HttpRegistryNodeIdentityProvider(client).list_versions("example-node")
    assert network.value.kind is ProviderFailureKind.NETWORK
    assert "super-secret" not in str(network.value)

    def invalid_payload(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text="not json secret")

    with (
        _client(invalid_payload) as client,
        pytest.raises(IdentityProviderError) as invalid,
    ):
        HttpRegistryNodeIdentityProvider(client).list_versions("example-node")
    assert invalid.value.kind is ProviderFailureKind.INVALID_RESPONSE
    assert "secret" not in str(invalid.value)


def test_git_provider_errors_do_not_reproduce_url_or_stderr_secrets() -> None:
    request = DirectGitIdentityRequest(
        "https://user:password@example.test/node.git?token=secret", "main"
    )
    provider = GitDirectIdentityProvider(
        runner=lambda *args, **kwargs: _completed("", returncode=128),
        credential_binding=GitCredentialProcessBinding(
            config_args=("-c", "credential.helper=!cdh-private-helper"),
            environment={
                "CDH_GIT_CREDENTIAL_SESSION": "/private/session-source-marker"
            },
        ),
    )

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(request)

    assert raised.value.kind is ProviderFailureKind.NETWORK
    assert "password" not in str(raised.value)
    assert "secret" not in str(raised.value)
    assert "session-source-marker" not in str(raised.value)


# Local executable identity binds canonical regular-file paths and content digests.
def test_local_executable_identity_is_canonical_and_content_sensitive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "scripts"
    script = root / "nested" / "hook.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"#!/bin/sh\necho one\n")
    script.chmod(0o755)
    provider = FilesystemLocalExecutableIdentityProvider()
    request = LocalExecutableIdentityRequest(root, PurePosixPath("nested/hook.sh"))

    first = provider.resolve(request)
    script.write_bytes(b"#!/bin/sh\necho two\n")
    second = provider.resolve(request)

    assert first.relative_path == PurePosixPath("nested/hook.sh")
    assert first.digest.startswith("sha256:")
    assert first.digest != second.digest


@pytest.mark.parametrize("relative", ["../hook.sh", "/hook.sh", "nested/../hook.sh"])
def test_local_executable_rejects_noncanonical_paths(
    tmp_path: Path, relative: str
) -> None:
    with pytest.raises(IdentityProviderError) as raised:
        FilesystemLocalExecutableIdentityProvider().resolve(
            LocalExecutableIdentityRequest(tmp_path, PurePosixPath(relative))
        )
    assert raised.value.kind is ProviderFailureKind.INVALID_REQUEST


def test_local_executable_rejects_symlink_and_accepts_regular_0644(
    tmp_path: Path,
) -> None:
    root = tmp_path / "scripts"
    root.mkdir()
    target = root / "target.sh"
    target.write_text("echo ok\n")
    target.chmod(0o755)
    symlink = root / "hook.sh"
    symlink.symlink_to(target)
    provider = FilesystemLocalExecutableIdentityProvider()

    with pytest.raises(IdentityProviderError) as linked:
        provider.resolve(LocalExecutableIdentityRequest(root, PurePosixPath("hook.sh")))
    assert linked.value.kind is ProviderFailureKind.LOCAL_INPUT

    target.chmod(0o644)
    identity = provider.resolve(
        LocalExecutableIdentityRequest(root, PurePosixPath("target.sh"))
    )
    assert identity.relative_path == PurePosixPath("target.sh")


def test_local_executable_rejects_symlinked_parent(tmp_path: Path) -> None:
    root = tmp_path / "scripts"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    script = external / "hook.sh"
    script.write_text("echo no\n")
    script.chmod(0o755)
    (root / "nested").symlink_to(external, target_is_directory=True)

    with pytest.raises(IdentityProviderError) as raised:
        FilesystemLocalExecutableIdentityProvider().resolve(
            LocalExecutableIdentityRequest(root, PurePosixPath("nested/hook.sh"))
        )

    assert raised.value.kind is ProviderFailureKind.LOCAL_INPUT


@pytest.mark.parametrize("substitution", ["leaf", "ancestor"])
def test_local_executable_rejects_substitution_during_descriptor_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
) -> None:
    root = tmp_path / "scripts"
    source = root / "nested/hook.sh"
    source.parent.mkdir(parents=True)
    source.write_text("echo trusted\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hook.sh").write_text("echo substituted\n")
    real_open = os.open
    substituted = False

    def substitute_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal substituted
        if not substituted and (
            (substitution == "leaf" and path == "hook.sh")
            or (substitution == "ancestor" and path == "nested")
        ):
            substituted = True
            if substitution == "leaf":
                source.unlink()
                source.symlink_to(outside / "hook.sh")
            else:
                real_parent = root / "real-nested"
                source.parent.rename(real_parent)
                source.parent.symlink_to(real_parent, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(file_admission.os, "open", substitute_then_open)

    with pytest.raises(IdentityProviderError) as raised:
        FilesystemLocalExecutableIdentityProvider().resolve(
            LocalExecutableIdentityRequest(root, PurePosixPath("nested/hook.sh"))
        )

    assert substituted
    assert raised.value.kind is ProviderFailureKind.LOCAL_INPUT
