"""Build-mounted downloader credential policy tests."""

from __future__ import annotations

import httpx
import pytest

from comfyui_docker_helper.config.build_plan import DownloaderCredentialRoutePlan
from comfyui_docker_helper.container import downloader_credentials as subject
from comfyui_docker_helper.container.downloader_credentials import (
    DownloaderCredentialError,
    MountedDownloaderCredentialPolicy,
)
from comfyui_docker_helper.file_admission import AdmittedRegularFile


def _route(match: str, secret_id: str) -> DownloaderCredentialRoutePlan:
    secret = secret_id.removeprefix("cdh-downloader-credential-")
    return DownloaderCredentialRoutePlan(
        match=match,
        type="bearer",
        token={"secret": secret},
        secret_id=secret_id,
    )


def test_mounted_policy_selects_longest_route_and_caches_each_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []
    values = {
        "/run/secrets/cdh-downloader-credential-base": b"base-token",
        "/run/secrets/cdh-downloader-credential-private": b"private-token",
    }

    def read(path, *, max_bytes):
        del max_bytes
        reads.append(str(path))
        return AdmittedRegularFile(values[str(path)], 0o400)

    monkeypatch.setattr(subject, "read_bounded_regular_absolute_file", read)
    policy = MountedDownloaderCredentialPolicy.from_routes(
        (
            _route(
                "https://example.test/",
                "cdh-downloader-credential-base",
            ),
            _route(
                "https://example.test/private",
                "cdh-downloader-credential-private",
            ),
        )
    )

    assert policy.authorization_for(httpx.URL("https://example.test/public")) == (
        b"Bearer base-token"
    )
    assert (
        policy.authorization_for(
            httpx.URL("https://example.test/private/file?signature=kept")
        )
        == b"Bearer private-token"
    )
    assert (
        policy.authorization_for(httpx.URL("https://example.test/private/again"))
        == b"Bearer private-token"
    )
    assert reads == [
        "/run/secrets/cdh-downloader-credential-base",
        "/run/secrets/cdh-downloader-credential-private",
    ]


def test_mounted_policy_caches_content_safe_invalid_value_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0

    def read(path, *, max_bytes):
        nonlocal reads
        del path, max_bytes
        reads += 1
        return AdmittedRegularFile(b"secret marker with spaces", 0o400)

    monkeypatch.setattr(subject, "read_bounded_regular_absolute_file", read)
    policy = MountedDownloaderCredentialPolicy.from_routes(
        (
            _route(
                "https://example.test/private",
                "cdh-downloader-credential-private",
            ),
        )
    )

    for _ in range(2):
        with pytest.raises(DownloaderCredentialError) as caught:
            policy.authorization_for(httpx.URL("https://example.test/private/file"))
        assert "secret marker" not in str(caught.value)
    assert reads == 1
