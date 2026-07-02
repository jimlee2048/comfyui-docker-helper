"""Tests for host source provider boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

import comfyui_docker_helper.host.source_providers as provider_module
from comfyui_docker_helper.config import UpstreamResponseError
from comfyui_docker_helper.host.source_providers import (
    GitRemoteProvider,
    HttpRegistryProvider,
    PyPIComfyCliProvider,
)

COMMIT_1 = "1" * 40
COMMIT_2 = "2" * 40
COMMIT_A = "a" * 40


@dataclass(slots=True)
class FakeResponse:
    """Minimal httpx response double for provider tests."""

    data: Any = None
    status_error: httpx.HTTPStatusError | None = None
    json_error: ValueError | None = None

    def raise_for_status(self) -> None:
        if self.status_error is not None:
            raise self.status_error

    def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return self.data


@dataclass(slots=True)
class FakeHttpClient:
    """Minimal httpx client double for provider tests."""

    response: FakeResponse | None = None
    request_error: httpx.RequestError | None = None
    calls: list[tuple[str, Mapping[str, str] | None]] | None = None

    def __post_init__(self) -> None:
        self.calls = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> FakeResponse:
        assert self.calls is not None
        self.calls.append((url, params))
        if self.request_error is not None:
            raise self.request_error
        assert self.response is not None
        return self.response


def test_comfyui_release_listing_uses_peeled_ref_for_annotated_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Annotated tags lock the target commit from the peeled ``^{}`` ref."""
    refs = "\n".join(
        [
            f"{COMMIT_1}\trefs/tags/v0.1.0",
            f"{COMMIT_2}\trefs/tags/v0.1.0^{{}}",
            f"{COMMIT_A}\trefs/tags/v0.2.0",
        ]
    )
    monkeypatch.setattr(provider_module, "_run_git", lambda *args: refs)

    releases = GitRemoteProvider().list_releases()

    assert releases[0].version == "v0.1.0"
    assert releases[0].commit == COMMIT_2
    assert releases[1].version == "v0.2.0"
    assert releases[1].commit == COMMIT_A


def test_comfyui_release_listing_keeps_lightweight_tag_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lightweight tags have no peeled ref and lock their tag commit directly."""
    monkeypatch.setattr(
        provider_module,
        "_run_git",
        lambda *args: f"{COMMIT_A}\trefs/tags/v0.2.0\n",
    )

    releases = GitRemoteProvider().list_releases()

    assert releases[0].version == "v0.2.0"
    assert releases[0].commit == COMMIT_A


def test_git_ref_resolution_uses_peeled_ref_for_annotated_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Annotated tags resolve to their target commit, not the tag object."""
    refs = "\n".join(
        [
            f"{COMMIT_1}\trefs/tags/v0.1.0",
            f"{COMMIT_2}\trefs/tags/v0.1.0^{{}}",
        ]
    )
    monkeypatch.setattr(provider_module, "_run_git", lambda *args: refs)

    commit = GitRemoteProvider().resolve_ref("https://example.com/repo.git", "v0.1.0")

    assert commit == COMMIT_2


def test_git_ref_resolution_keeps_lightweight_tag_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lightweight tags resolve to their single returned commit."""
    monkeypatch.setattr(
        provider_module,
        "_run_git",
        lambda *args: f"{COMMIT_A}\trefs/tags/v0.2.0\n",
    )

    commit = GitRemoteProvider().resolve_ref("https://example.com/repo.git", "v0.2.0")

    assert commit == COMMIT_A


def test_pypi_provider_wraps_http_request_errors() -> None:
    """HTTP transport failures become resolver diagnostics."""
    client = FakeHttpClient(
        request_error=httpx.RequestError(
            "network down",
            request=httpx.Request("GET", "https://pypi.org"),
        )
    )

    with pytest.raises(UpstreamResponseError) as error:
        PyPIComfyCliProvider(client).list_versions()

    assert error.value.source == "PyPI comfy-cli"
    assert error.value.selector == "versions"
    assert "HTTP request failed" in error.value.reason


def test_pypi_provider_wraps_http_status_errors() -> None:
    """Non-success HTTP status failures become resolver diagnostics."""
    request = httpx.Request("GET", "https://pypi.org")
    response = httpx.Response(503, request=request)
    client = FakeHttpClient(
        response=FakeResponse(
            status_error=httpx.HTTPStatusError(
                "service unavailable",
                request=request,
                response=response,
            )
        )
    )

    with pytest.raises(UpstreamResponseError) as error:
        PyPIComfyCliProvider(client).list_versions()

    assert error.value.source == "PyPI comfy-cli"
    assert "status 503" in error.value.reason


def test_pypi_provider_wraps_json_decode_failures() -> None:
    """Invalid JSON responses become resolver diagnostics."""
    client = FakeHttpClient(response=FakeResponse(json_error=ValueError("bad json")))

    with pytest.raises(UpstreamResponseError) as error:
        PyPIComfyCliProvider(client).list_versions()

    assert error.value.source == "PyPI comfy-cli"
    assert error.value.reason == "response body was not valid JSON"


def test_pypi_provider_rejects_invalid_response_shape() -> None:
    """Missing releases table becomes a resolver diagnostic."""
    client = FakeHttpClient(response=FakeResponse(data={"not_releases": {}}))

    with pytest.raises(UpstreamResponseError) as error:
        PyPIComfyCliProvider(client).list_versions()

    assert error.value.source == "PyPI comfy-cli"
    assert "releases table" in error.value.reason


def test_registry_provider_wraps_json_decode_failures() -> None:
    """Registry JSON failures become resolver diagnostics."""
    client = FakeHttpClient(response=FakeResponse(json_error=ValueError("bad json")))

    with pytest.raises(UpstreamResponseError) as error:
        HttpRegistryProvider(client).get_install_metadata("node")

    assert error.value.source == "Comfy Registry install"
    assert error.value.reason == "response body was not valid JSON"


def test_registry_provider_rejects_invalid_install_shape() -> None:
    """Registry install responses must include a concrete version."""
    client = FakeHttpClient(response=FakeResponse(data={"id": "node"}))

    with pytest.raises(UpstreamResponseError) as error:
        HttpRegistryProvider(client).get_install_metadata("node")

    assert error.value.source == "Comfy Registry install"
    assert "version string" in error.value.reason


def test_registry_provider_rejects_invalid_versions_shape() -> None:
    """Registry version list items must include concrete versions."""
    client = FakeHttpClient(response=FakeResponse(data={"versions": [{}]}))

    with pytest.raises(UpstreamResponseError) as error:
        HttpRegistryProvider(client).list_versions("node")

    assert error.value.source == "Comfy Registry versions"
    assert "version string" in error.value.reason
