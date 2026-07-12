"""Focused M2-T4 tests for isolated exact identity providers."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import pytest

from comfyui_docker_helper.config.resolvers import COMFYUI_REPO_URL
from comfyui_docker_helper.host.identity_providers import (
    ComfyCliIdentityRequest,
    DirectGitIdentityRequest,
    FilesystemLocalExecutableIdentityProvider,
    GitDirectIdentityProvider,
    GitOfficialComfyUIIdentityProvider,
    HttpOciIdentityProvider,
    HttpRegistryNodeIdentityProvider,
    IdentityProviderError,
    LocalExecutableIdentityRequest,
    ManagedPythonIdentityRequest,
    OciIdentityRequest,
    OfficialComfyUIIdentityRequest,
    ProviderFailureKind,
    PyPIComfyCliIdentityProvider,
    RegistryNodeIdentityRequest,
    SelectorStability,
    UvManagedPythonIdentityProvider,
)
from comfyui_docker_helper.host.uv_runner import HostUvRunner

INDEX_DIGEST_A = f"sha256:{'a' * 64}"
INDEX_DIGEST_B = f"sha256:{'b' * 64}"
ARM64_DIGEST = f"sha256:{'d' * 64}"
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40

CONFIG_DOCUMENT = {"os": "linux", "architecture": "amd64"}
CONFIG_CONTENT = json.dumps(CONFIG_DOCUMENT, separators=(",", ":")).encode()
CONFIG_DIGEST = f"sha256:{hashlib.sha256(CONFIG_CONTENT).hexdigest()}"
CHILD_DOCUMENT = {
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {"digest": CONFIG_DIGEST},
}
CHILD_CONTENT = json.dumps(CHILD_DOCUMENT, separators=(",", ":")).encode()
AMD64_DIGEST = f"sha256:{hashlib.sha256(CHILD_CONTENT).hexdigest()}"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _index_response(
    request: httpx.Request,
    *,
    digest: str = INDEX_DIGEST_A,
    platforms: Sequence[tuple[str, str, str]] = (
        ("linux", "amd64", AMD64_DIGEST),
        ("linux", "arm64", ARM64_DIGEST),
    ),
) -> httpx.Response:
    manifests = [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": child_digest,
            "platform": {"os": os_name, "architecture": architecture},
        }
        for os_name, architecture, child_digest in platforms
    ]
    response = httpx.Response(
        200,
        request=request,
        json={
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": manifests,
            "annotations": {"test.identity": digest},
        },
    )
    response.headers["Docker-Content-Digest"] = (
        f"sha256:{hashlib.sha256(response.content).hexdigest()}"
    )
    return response


def _child_response(
    request: httpx.Request,
    *,
    content: bytes = CHILD_CONTENT,
    digest: str = AMD64_DIGEST,
) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        headers={"Docker-Content-Digest": digest},
        content=content,
    )


def _config_response(
    request: httpx.Request, *, content: bytes = CONFIG_CONTENT
) -> httpx.Response:
    return httpx.Response(200, request=request, content=content)


def _oci_index_handler(
    request: httpx.Request,
    *,
    marker: str = INDEX_DIGEST_A,
    platforms: Sequence[tuple[str, str, str]] = (
        ("linux", "amd64", AMD64_DIGEST),
        ("linux", "arm64", ARM64_DIGEST),
    ),
) -> httpx.Response:
    if "/blobs/" in request.url.path:
        return _config_response(request)
    if request.url.path.endswith(f"/manifests/{AMD64_DIGEST}"):
        return _child_response(request)
    return _index_response(request, digest=marker, platforms=platforms)


def test_oci_index_preserves_top_descriptor_and_exact_platform_binding() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _oci_index_handler(request)

    with _client(handler) as client:
        identity = HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest(
                "cuda-base", "nvidia/cuda", "13.0.3-cudnn-devel-ubuntu24.04"
            )
        )

    assert identity.role == "cuda-base"
    assert identity.repository == "nvidia/cuda"
    assert identity.descriptor_digest.startswith("sha256:")
    assert identity.descriptor_kind == "index"
    assert identity.platform == "linux/amd64"
    assert seen[0].url.host == "registry-1.docker.io"
    assert seen[0].url.path.startswith("/v2/nvidia/cuda/manifests/")
    assert "application/vnd.oci.image.index.v1+json" in seen[0].headers["Accept"]
    assert seen[1].url.path.endswith(f"/manifests/{AMD64_DIGEST}")
    assert seen[2].url.path.endswith(f"/blobs/{CONFIG_DIGEST}")


def test_oci_tag_movement_returns_current_descriptor_without_local_catalog() -> None:
    top_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal top_calls
        if (
            "/blobs/" not in request.url.path
            and "/manifests/sha256:" not in request.url.path
        ):
            top_calls += 1
        return _oci_index_handler(
            request, marker=INDEX_DIGEST_A if top_calls == 1 else INDEX_DIGEST_B
        )

    request = OciIdentityRequest("uv-tool", "ghcr.io/astral-sh/uv", "latest")
    with _client(handler) as client:
        provider = HttpOciIdentityProvider(client)
        first = provider.resolve(request)
        second = provider.resolve(request)

    assert request.stability is SelectorStability.MOVING
    assert first.descriptor_digest != second.descriptor_digest


def test_oci_bearer_challenge_retries_without_exposing_token() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "auth.example.test":
            return httpx.Response(200, request=request, json={"token": "secret-token"})
        if "Authorization" not in request.headers:
            return httpx.Response(
                401,
                request=request,
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="https://auth.example.test/token",'
                        'service="registry.test",scope="repository:owner/image:pull"'
                    )
                },
            )
        assert request.headers["Authorization"] == "Bearer secret-token"
        return _oci_index_handler(request)

    with _client(handler) as client:
        identity = HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("cuda-base", "registry.test/owner/image", "moving")
        )

    assert identity.descriptor_digest.startswith("sha256:")
    assert len(calls) == 5


def test_oci_single_manifest_checks_config_platform() -> None:
    config_content = b'{"os":"linux","architecture":"amd64"}'
    config_digest = f"sha256:{hashlib.sha256(config_content).hexdigest()}"

    def handler(request: httpx.Request) -> httpx.Response:
        if "/blobs/" in request.url.path:
            return httpx.Response(
                200,
                request=request,
                content=config_content,
            )
        response = httpx.Response(
            200,
            request=request,
            json={
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": config_digest},
            },
        )
        response.headers["Docker-Content-Digest"] = (
            f"sha256:{hashlib.sha256(response.content).hexdigest()}"
        )
        return response

    with _client(handler) as client:
        identity = HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("cuda-base", "registry.test/owner/image", "exact")
        )

    assert identity.descriptor_kind == "manifest"
    assert identity.descriptor_digest.startswith("sha256:")


def test_oci_rejects_descriptor_header_that_does_not_match_manifest_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = _index_response(request)
        response.headers["Docker-Content-Digest"] = INDEX_DIGEST_A
        return response

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("cuda-base", "registry.test/owner/image", "tag")
        )

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


def test_oci_index_rejects_child_manifest_digest_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/manifests/{AMD64_DIGEST}"):
            return _child_response(request, content=b"not the selected manifest")
        return _oci_index_handler(request)

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("cuda-base", "registry.test/owner/image", "tag")
        )

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


def test_oci_index_rejects_child_manifest_media_type_mismatch() -> None:
    child_document = {
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"digest": CONFIG_DIGEST},
    }
    child_content = json.dumps(child_document, separators=(",", ":")).encode()
    child_digest = f"sha256:{hashlib.sha256(child_content).hexdigest()}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/manifests/{child_digest}"):
            return _child_response(request, content=child_content, digest=child_digest)
        return _index_response(request, platforms=(("linux", "amd64", child_digest),))

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("cuda-base", "registry.test/owner/image", "tag")
        )

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


def test_oci_index_rejects_child_config_digest_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/blobs/" in request.url.path:
            return _config_response(request, content=b'{"os":"linux"}')
        return _oci_index_handler(request)

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("cuda-base", "registry.test/owner/image", "tag")
        )

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


def test_oci_index_rejects_config_platform_mismatch() -> None:
    config_content = b'{"os":"linux","architecture":"arm64"}'
    config_digest = f"sha256:{hashlib.sha256(config_content).hexdigest()}"
    child_document = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": config_digest},
    }
    child_content = json.dumps(child_document, separators=(",", ":")).encode()
    child_digest = f"sha256:{hashlib.sha256(child_content).hexdigest()}"

    def handler(request: httpx.Request) -> httpx.Response:
        if "/blobs/" in request.url.path:
            return _config_response(request, content=config_content)
        if request.url.path.endswith(f"/manifests/{child_digest}"):
            return _child_response(request, content=child_content, digest=child_digest)
        return _index_response(request, platforms=(("linux", "amd64", child_digest),))

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("cuda-base", "registry.test/owner/image", "tag")
        )

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


@pytest.mark.parametrize("tag", ["", "-bad", "bad\ntag"])
def test_oci_rejects_invalid_tags_before_http(tag: str) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _index_response(request)

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("cuda-base", "registry.test/owner/image", tag)
        )

    assert raised.value.kind is ProviderFailureKind.INVALID_REQUEST
    assert called is False


def test_oci_rejects_invalid_role_before_http() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _index_response(request)

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("invalid", "registry.test/owner/image", "tag")  # type: ignore[arg-type]
        )

    assert raised.value.kind is ProviderFailureKind.INVALID_REQUEST
    assert called is False


@pytest.mark.parametrize(
    ("platforms", "kind"),
    [
        (("linux", "arm64", ARM64_DIGEST), ProviderFailureKind.NOT_FOUND),
        (
            (
                ("linux", "amd64", AMD64_DIGEST),
                ("linux", "amd64", ARM64_DIGEST),
            ),
            ProviderFailureKind.INVALID_RESPONSE,
        ),
    ],
)
def test_oci_rejects_missing_or_ambiguous_platform(
    platforms: Any, kind: ProviderFailureKind
) -> None:
    normalized = platforms if isinstance(platforms[0], tuple) else (platforms,)
    with (
        _client(
            lambda request: _index_response(request, platforms=normalized)
        ) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        HttpOciIdentityProvider(client).resolve(
            OciIdentityRequest("cuda-base", "registry.test/owner/image", "tag")
        )

    assert raised.value.kind is kind


def _completed(stdout: str, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ("provider",), returncode, stdout, "secret stderr"
    )


@pytest.mark.parametrize("version", ["3.13.14", "3.12.13"])
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
    calls: list[tuple[Sequence[str], Mapping[str, object]]] = []

    def runner(
        args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return _completed(json.dumps(rows))

    provider = UvManagedPythonIdentityProvider(
        HostUvRunner(Path("/owned/uv")), runner=runner
    )
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
    assert calls[0][0][0] == "/owned/uv"
    assert "--no-config" in calls[0][0]
    assert "--all-platforms" in calls[0][0]


def test_managed_python_rejects_missing_and_duplicate_catalog_rows() -> None:
    request = ManagedPythonIdentityRequest("3.13.14", INDEX_DIGEST_A)
    provider = UvManagedPythonIdentityProvider(
        HostUvRunner(Path("/owned/uv")), runner=lambda *args, **kwargs: _completed("[]")
    )
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
    duplicate = UvManagedPythonIdentityProvider(
        HostUvRunner(Path("/owned/uv")),
        runner=lambda *args, **kwargs: _completed(json.dumps([row, row])),
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
    provider = UvManagedPythonIdentityProvider(
        HostUvRunner(Path("/owned/uv")),
        runner=lambda *args, **kwargs: _completed(json.dumps([row])),
    )

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(ManagedPythonIdentityRequest("3.13.14", INDEX_DIGEST_A))

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


def test_managed_python_rejects_invalid_catalog_descriptor_before_uv() -> None:
    called = False

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return _completed("[]")

    provider = UvManagedPythonIdentityProvider(
        HostUvRunner(Path("/owned/uv")), runner=runner
    )
    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(ManagedPythonIdentityRequest("3.13.14", "sha256:bad"))

    assert raised.value.kind is ProviderFailureKind.INVALID_REQUEST
    assert called is False


def _git_runner(
    output: str, calls: list[tuple[Sequence[str], Mapping[str, object]]]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def runner(
        args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return _completed(output)

    return runner


def test_official_comfyui_resolves_peeled_release_commit_and_release_identity() -> None:
    calls: list[tuple[Sequence[str], Mapping[str, object]]] = []
    provider = GitOfficialComfyUIIdentityProvider(
        runner=_git_runner(
            f"{COMMIT_A}\trefs/tags/v0.4.0\n{COMMIT_B}\trefs/tags/v0.4.0^{{}}\n",
            calls,
        )
    )
    request = OfficialComfyUIIdentityRequest(COMFYUI_REPO_URL, "refs/tags/v0.4.0")

    identity = provider.resolve(request)

    assert request.stability is SelectorStability.EXACT
    assert identity.repository == COMFYUI_REPO_URL
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

    releases = provider.list_releases(COMFYUI_REPO_URL)

    assert [item.formal_release for item in releases] == ["0.4.0", "0.5.0"]
    assert releases[0].commit == "3" * 40
    assert calls[0][0][1:4] == ("ls-remote", "--tags", "--end-of-options")


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
    request = DirectGitIdentityRequest("https://example.test/node.git", "main")
    provider = GitDirectIdentityProvider(
        runner=_git_runner(f"{COMMIT_A}\trefs/heads/main\n", [])
    )

    identity = provider.resolve(request)

    assert request.stability is SelectorStability.MOVING
    assert identity.type == "git"
    assert identity.url == request.url
    assert identity.commit == COMMIT_A


def test_direct_git_full_commit_request_is_classified_exact() -> None:
    request = DirectGitIdentityRequest("https://example.test/node.git", COMMIT_A)
    assert request.stability is SelectorStability.EXACT


def test_pypi_comfy_cli_confirms_exact_published_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pypi/comfy-cli/1.4.1/json"
        return httpx.Response(
            200,
            request=request,
            json={
                "info": {"version": "1.4.1"},
                "urls": [{"filename": "comfy_cli-1.4.1-py3-none-any.whl"}],
            },
        )

    with _client(handler) as client:
        request = ComfyCliIdentityRequest("v1.4.1")
        identity = PyPIComfyCliIdentityProvider(client).resolve(request)

    assert request.stability is SelectorStability.EXACT
    assert identity.package == "comfy-cli"
    assert identity.version == "1.4.1"


def test_pypi_comfy_cli_catalog_returns_sorted_published_identities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "releases": {
                    "2.0.0": [],
                    "1.1.0": [{"filename": "comfy_cli-1.1.0.whl"}],
                    "1.0.0rc1": [{"filename": "comfy_cli-1.0.0rc1.whl"}],
                }
            },
        )

    with _client(handler) as client:
        identities = PyPIComfyCliIdentityProvider(client).list_versions()

    assert [identity.version for identity in identities] == [
        "1.0.0rc1",
        "1.1.0",
    ]


def test_pypi_comfy_cli_exact_release_without_artifacts_is_not_published() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"info": {"version": "2.0.0"}, "urls": []},
        )

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        PyPIComfyCliIdentityProvider(client).resolve(ComfyCliIdentityRequest("2.0.0"))

    assert raised.value.kind is ProviderFailureKind.NOT_FOUND


@pytest.mark.parametrize(
    "document",
    [
        {"releases": {"not-a-version": [{"filename": "bad.whl"}]}},
        {"releases": {"1.0.0": "not-a-files-list"}},
    ],
)
def test_pypi_comfy_cli_maps_malformed_catalog_to_invalid_response(
    document: dict[str, object],
) -> None:
    with (
        _client(
            lambda request: httpx.Response(200, request=request, json=document)
        ) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        PyPIComfyCliIdentityProvider(client).list_versions()

    assert raised.value.kind is ProviderFailureKind.INVALID_RESPONSE


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
    assert not hasattr(identity, "download_url")


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
def test_http_provider_status_errors_are_short_stable_and_secret_free(
    status: int, kind: ProviderFailureKind
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, text="token=super-secret")

    with (
        _client(handler) as client,
        pytest.raises(IdentityProviderError) as raised,
    ):
        PyPIComfyCliIdentityProvider(
            client, base_url="https://user:password@example.test"
        ).resolve(ComfyCliIdentityRequest("1.0.0"))

    assert raised.value.kind is kind
    rendered = str(raised.value)
    assert "super-secret" not in rendered
    assert "password" not in rendered
    assert "1.0.0" not in rendered


def test_http_provider_maps_network_and_invalid_payload() -> None:
    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("token=super-secret", request=request)

    with (
        _client(network_failure) as client,
        pytest.raises(IdentityProviderError) as network,
    ):
        PyPIComfyCliIdentityProvider(client).resolve(ComfyCliIdentityRequest("1.0.0"))
    assert network.value.kind is ProviderFailureKind.NETWORK
    assert "super-secret" not in str(network.value)

    def invalid_payload(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text="not json secret")

    with (
        _client(invalid_payload) as client,
        pytest.raises(IdentityProviderError) as invalid,
    ):
        PyPIComfyCliIdentityProvider(client).resolve(ComfyCliIdentityRequest("1.0.0"))
    assert invalid.value.kind is ProviderFailureKind.INVALID_RESPONSE
    assert "secret" not in str(invalid.value)


def test_git_provider_errors_do_not_reproduce_url_or_stderr_secrets() -> None:
    request = DirectGitIdentityRequest(
        "https://user:password@example.test/node.git?token=secret", "main"
    )
    provider = GitDirectIdentityProvider(
        runner=lambda *args, **kwargs: _completed("", returncode=128)
    )

    with pytest.raises(IdentityProviderError) as raised:
        provider.resolve(request)

    assert raised.value.kind is ProviderFailureKind.NETWORK
    assert "password" not in str(raised.value)
    assert "secret" not in str(raised.value)


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

    assert request.stability is SelectorStability.MOVING
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


def test_local_executable_rejects_symlink_and_non_executable(
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
    with pytest.raises(IdentityProviderError) as not_executable:
        provider.resolve(
            LocalExecutableIdentityRequest(root, PurePosixPath("target.sh"))
        )
    assert not_executable.value.kind is ProviderFailureKind.LOCAL_INPUT


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
