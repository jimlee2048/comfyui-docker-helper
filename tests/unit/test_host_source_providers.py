"""Tests for host source provider boundaries."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import comfyui_docker_helper.host.source_providers as provider_module
from comfyui_docker_helper.config import UpstreamResponseError
from comfyui_docker_helper.host.source_providers import (
    GitRemoteProvider,
    HttpRegistryProvider,
    PyPIComfyCliProvider,
    create_default_source_resolvers,
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
    """Minimal httpx client double that records requested URLs and params."""

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


class FakeClosableHttpClient:
    """Minimal close-tracking client for resolver owner tests."""

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def test_comfyui_release_listing_uses_peeled_ref_for_annotated_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release listing locks annotated tags to the peeled target commit."""
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


def test_default_source_resolver_owner_closes_http_client_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default live resolver owner closes its shared HTTP client explicitly."""
    client = FakeClosableHttpClient()
    monkeypatch.setattr(provider_module.httpx, "Client", lambda **kwargs: client)

    owner = create_default_source_resolvers()

    assert owner.resolvers.comfy_cli.client is client
    with owner as resolvers:
        assert resolvers is owner.resolvers
    owner.close()

    assert client.close_calls == 1


def test_comfyui_release_listing_keeps_lightweight_tag_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release listing keeps lightweight tags at their single returned commit."""
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
    """Explicit tag refs prefer peeled annotated-tag targets over tag objects."""
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
    """Explicit lightweight tags resolve to their single returned commit."""
    monkeypatch.setattr(
        provider_module,
        "_run_git",
        lambda *args: f"{COMMIT_A}\trefs/tags/v0.2.0\n",
    )

    commit = GitRemoteProvider().resolve_ref("https://example.com/repo.git", "v0.2.0")

    assert commit == COMMIT_A


def test_run_git_disables_interactive_prompts_and_sets_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git subprocesses run with non-interactive credential settings."""
    calls: list[dict[str, Any]] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            ("git", "ls-remote"),
            0,
            stdout=f"{COMMIT_A}\tHEAD\n",
            stderr="",
        )

    monkeypatch.setattr(provider_module.subprocess, "run", fake_run)

    output = provider_module._run_git("git", "ls-remote", "https://example.com")

    assert output == f"{COMMIT_A}\tHEAD\n"
    assert calls[0]["timeout"] == provider_module._GIT_TIMEOUT_SECONDS
    assert calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert calls[0]["env"]["GIT_ASKPASS"] == ""
    assert calls[0]["env"]["GCM_INTERACTIVE"] == "never"
    assert calls[0]["env"]["SSH_ASKPASS"] == ""


def test_git_remote_url_cannot_be_interpreted_as_an_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leading-option repository value stays positional in a real Git process."""
    repository = tmp_path / "HEAD"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", str(repository)],
        check=True,
    )
    marker = tmp_path / "upload-pack-ran"
    helper = tmp_path / "upload-pack-probe"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        "os.execvp('git-upload-pack', ['git-upload-pack', *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    malicious_url = f"--upload-pack={helper}"
    monkeypatch.chdir(tmp_path)

    unsafe = subprocess.run(
        ["git", "ls-remote", malicious_url, "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert unsafe.returncode == 0
    assert marker.is_file(), "fixture must reproduce option interpretation"
    marker.unlink()

    with pytest.raises(UpstreamResponseError):
        GitRemoteProvider().resolve_default_branch_head(malicious_url)

    assert not marker.exists()


def test_git_provider_resolves_valid_local_repository_with_real_git(
    tmp_path: Path,
) -> None:
    """The option boundary preserves valid URL/ref resolution behavior."""
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test User"],
        check=True,
    )
    (repository / "tracked").write_text("content\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", "tracked"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "initial"],
        check=True,
    )
    expected = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    provider = GitRemoteProvider()

    assert provider.resolve_default_branch_head(str(repository)) == expected
    assert provider.resolve_ref(str(repository), "HEAD") == expected


def test_git_provider_places_dynamic_url_and_ref_after_option_terminator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both dynamic ls-remote operands follow Git's explicit option boundary."""
    calls: list[tuple[str, ...]] = []

    def fake_run_git(*args: str) -> str:
        calls.append(args)
        return f"{COMMIT_A}\trefs/heads/--upload-pack=probe\n"

    monkeypatch.setattr(provider_module, "_run_git", fake_run_git)

    GitRemoteProvider().resolve_ref(
        "--upload-pack=repository-probe",
        "--upload-pack=ref-probe",
    )

    assert calls == [
        (
            "git",
            "ls-remote",
            "--end-of-options",
            "--upload-pack=repository-probe",
            "--upload-pack=ref-probe",
        )
    ]


def test_run_git_wraps_timeout_as_upstream_response_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git timeouts become user-readable resolver diagnostics."""

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(("git", "ls-remote"), timeout=30)

    monkeypatch.setattr(provider_module.subprocess, "run", fake_run)

    with pytest.raises(UpstreamResponseError) as error:
        provider_module._run_git("git", "ls-remote", "https://example.com")

    assert error.value.source == "git"
    assert "timed out" in error.value.reason


def test_git_head_resolution_rejects_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD resolution reports empty ls-remote output instead of indexing it."""
    monkeypatch.setattr(provider_module, "_run_git", lambda *args: "")

    with pytest.raises(UpstreamResponseError) as nightly_error:
        GitRemoteProvider().get_nightly_commit()
    with pytest.raises(UpstreamResponseError) as default_error:
        GitRemoteProvider().resolve_default_branch_head("https://example.com/repo.git")

    assert nightly_error.value.source == "git ls-remote"
    assert "no remote ref matched" in nightly_error.value.reason
    assert default_error.value.source == "git ls-remote"
    assert "no remote ref matched" in default_error.value.reason


def test_git_shorthand_ref_rejects_branch_tag_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shorthand branch/tag collision requires a full ref selector."""
    refs = "\n".join(
        [
            f"{COMMIT_A}\trefs/heads/release",
            f"{COMMIT_1}\trefs/tags/release",
            f"{COMMIT_2}\trefs/tags/release^{{}}",
        ]
    )
    monkeypatch.setattr(provider_module, "_run_git", lambda *args: refs)

    with pytest.raises(UpstreamResponseError) as error:
        GitRemoteProvider().resolve_ref("https://example.com/repo.git", "release")

    assert error.value.source == "git ls-remote"
    assert "ambiguous remote ref" in error.value.reason
    assert "refs/heads/name" in error.value.reason


def test_git_full_tag_ref_keeps_annotated_tag_peeled_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full tag refs can disambiguate and still use annotated tag peeled commits."""
    refs = "\n".join(
        [
            f"{COMMIT_1}\trefs/tags/release",
            f"{COMMIT_2}\trefs/tags/release^{{}}",
        ]
    )
    monkeypatch.setattr(provider_module, "_run_git", lambda *args: refs)

    commit = GitRemoteProvider().resolve_ref(
        "https://example.com/repo.git",
        "refs/tags/release",
    )

    assert commit == COMMIT_2


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


def test_registry_provider_uses_api_base_url_by_default() -> None:
    """Registry install URLs default to the public API host, not the web UI."""
    client = FakeHttpClient(
        response=FakeResponse(data={"id": "node", "version": "1.0.0"})
    )

    HttpRegistryProvider(client).get_install_metadata("node")

    assert client.calls == [
        ("https://api.comfy.org/nodes/node/install", {}),
    ]


def test_registry_provider_prefers_install_node_id_over_uuid_id() -> None:
    """Install metadata prefers slug ``node_id`` over registry UUID ``id``."""
    client = FakeHttpClient(
        response=FakeResponse(
            data={
                "id": "0baa100d-0000-4000-8000-000000000000",
                "node_id": "comfyui-custom-scripts",
                "version": "1.2.5",
            }
        )
    )

    metadata = HttpRegistryProvider(client).get_install_metadata(
        "comfyui-custom-scripts"
    )

    assert metadata.node_id == "comfyui-custom-scripts"
    assert metadata.version == "1.2.5"


def test_registry_provider_prefers_version_node_id_over_uuid_id() -> None:
    """Version listing prefers slug ``node_id`` over registry UUID ``id``."""
    client = FakeHttpClient(
        response=FakeResponse(
            data={
                "versions": [
                    {
                        "id": "0baa100d-0000-4000-8000-000000000000",
                        "node_id": "comfyui-custom-scripts",
                        "version": "1.2.5",
                    }
                ]
            }
        )
    )

    versions = HttpRegistryProvider(client).list_versions("comfyui-custom-scripts")

    assert versions[0].node_id == "comfyui-custom-scripts"
    assert versions[0].version == "1.2.5"


def test_registry_provider_accepts_explicit_boolean_fields() -> None:
    """Registry boolean fields preserve concrete bool response values."""
    client = FakeHttpClient(
        response=FakeResponse(
            data={
                "id": "node",
                "version": "1.0.0",
                "active": False,
                "installable": False,
                "deprecated": True,
            }
        )
    )

    metadata = HttpRegistryProvider(client).get_install_metadata("node")

    assert metadata.active is False
    assert metadata.installable is False
    assert metadata.deprecated is True


@pytest.mark.parametrize("value", ["false", 0, [], {}])
@pytest.mark.parametrize("field", ["active", "installable", "deprecated"])
def test_registry_install_rejects_malformed_boolean_fields(
    field: str,
    value: object,
) -> None:
    """Registry install bool fields must be booleans, not truthy-cast values."""
    client = FakeHttpClient(
        response=FakeResponse(data={"id": "node", "version": "1.0.0", field: value})
    )

    with pytest.raises(UpstreamResponseError) as error:
        HttpRegistryProvider(client).get_install_metadata("node")

    assert error.value.source == "Comfy Registry install"
    assert field in error.value.reason
    assert "boolean" in error.value.reason


@pytest.mark.parametrize("value", ["false", 0, [], {}])
@pytest.mark.parametrize("field", ["active", "installable", "deprecated"])
def test_registry_versions_reject_malformed_boolean_fields(
    field: str,
    value: object,
) -> None:
    """Registry version bool fields must be booleans, not truthy-cast values."""
    client = FakeHttpClient(
        response=FakeResponse(
            data={"versions": [{"id": "node", "version": "1.0.0", field: value}]}
        )
    )

    with pytest.raises(UpstreamResponseError) as error:
        HttpRegistryProvider(client).list_versions("node")

    assert error.value.source == "Comfy Registry versions"
    assert field in error.value.reason
    assert "boolean" in error.value.reason


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
