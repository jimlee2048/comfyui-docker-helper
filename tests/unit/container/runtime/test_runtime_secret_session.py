"""Runtime downloader Secret projection and generation-cache tests."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
import pytest

from comfyui_docker_helper.config.credentials.secrets import (
    CREDENTIAL_SECRET_MAX_BYTES,
)
from comfyui_docker_helper.config.runtime.models import RuntimeConfig
from comfyui_docker_helper.container.runtime import secret_session as subject
from comfyui_docker_helper.container.runtime.secret_session import (
    RuntimeDownloaderCredentialPolicy,
    RuntimeSecretSession,
    RuntimeSecretSessionError,
    RuntimeSecretSource,
)


class CountingEnvironment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads = 0

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def get(self, key: str, default: str | None = None) -> str | None:
        self.reads += 1
        return self.values.get(key, default)


def test_environment_secret_is_lazy_and_cached_once() -> None:
    environ = CountingEnvironment({"HF_TOKEN": "hf_token-value"})
    session = RuntimeSecretSession(
        {"hf_read": RuntimeSecretSource("env", "HF_TOKEN")},
        environ,
    )

    assert environ.reads == 0
    assert session.bearer_token("hf_read") == b"hf_token-value"
    environ.values["HF_TOKEN"] = "rotated"
    assert session.bearer_token("hf_read") == b"hf_token-value"
    assert environ.reads == 1


def test_cached_failure_raises_fresh_attempt_state() -> None:
    environ = CountingEnvironment({})
    session = RuntimeSecretSession(
        {"hf_read": RuntimeSecretSource("env", "HF_TOKEN")},
        environ,
    )

    with pytest.raises(RuntimeSecretSessionError) as first:
        session.bearer_token("hf_read")
    first.value.network_attempted = True

    with pytest.raises(RuntimeSecretSessionError) as second:
        session.bearer_token("hf_read")

    assert second.value is not first.value
    assert second.value.code == "source_unavailable"
    assert second.value.network_attempted is False
    assert environ.reads == 1


def test_unexpected_bearer_validator_failure_is_not_policy_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RuntimeSecretSession(
        {"hf_read": RuntimeSecretSource("env", "HF_TOKEN")},
        {"HF_TOKEN": "valid-token"},
    )

    validation_attempts = 0

    def fail(_: bytes) -> None:
        nonlocal validation_attempts
        validation_attempts += 1
        raise RuntimeError("programming failure")

    monkeypatch.setattr(subject, "validate_bearer_token", fail)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="programming failure"):
            session.bearer_token("hf_read")

    assert validation_attempts == 2


def test_projected_symlink_is_resolved_fresh_by_each_session(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    projection = tmp_path / "token"
    first.write_bytes(b"first-token")
    second.write_bytes(b"second-token")
    projection.symlink_to(first)
    source = {"hf_read": RuntimeSecretSource("file", os.fspath(projection))}

    assert RuntimeSecretSession(source, {}).bearer_token("hf_read") == b"first-token"
    projection.unlink()
    projection.symlink_to(second)
    assert RuntimeSecretSession(source, {}).bearer_token("hf_read") == b"second-token"


def test_runtime_secret_file_enforces_shared_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "token"
    admitted = b"a" * CREDENTIAL_SECRET_MAX_BYTES
    token.write_bytes(admitted)
    requested_sizes: list[int] = []
    real_read = subject.os.read

    def observe_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(subject.os, "read", observe_read)
    admitted_session = RuntimeSecretSession(
        {"hf_read": RuntimeSecretSource("file", os.fspath(token))},
        {},
    )

    assert admitted_session.bearer_token("hf_read") == admitted
    assert requested_sizes
    assert max(requested_sizes) <= CREDENTIAL_SECRET_MAX_BYTES + 1

    requests_before_rejection = len(requested_sizes)
    token.write_bytes(b"a" * (CREDENTIAL_SECRET_MAX_BYTES + 1))
    oversized_session = RuntimeSecretSession(
        {"hf_read": RuntimeSecretSource("file", os.fspath(token))},
        {},
    )

    with pytest.raises(RuntimeSecretSessionError) as raised:
        oversized_session.bearer_token("hf_read")

    assert raised.value.code == "source_unavailable"
    assert "a" * 100 not in str(raised.value)
    assert len(requested_sizes) == requests_before_rejection


@pytest.mark.parametrize("source_kind", ["directory", "fifo"])
def test_projected_secret_rejects_non_regular_target(
    tmp_path: Path,
    source_kind: str,
) -> None:
    target = tmp_path / "target"
    if source_kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    projection = tmp_path / "token"
    projection.symlink_to(target)
    session = RuntimeSecretSession(
        {"hf_read": RuntimeSecretSource("file", os.fspath(projection))},
        {},
    )

    with pytest.raises(RuntimeSecretSessionError) as raised:
        session.bearer_token("hf_read")

    assert raised.value.code == "source_unavailable"
    assert os.fspath(target) not in str(raised.value)


def test_runtime_policy_selects_longest_route_and_keeps_value_private() -> None:
    config = RuntimeConfig.model_validate(
        {
            "cdh": {
                "downloader": {
                    "credentials": [
                        {
                            "match": "https://example.test/models/",
                            "type": "bearer",
                            "token": {"secret": "general"},
                        },
                        {
                            "match": "https://example.test/models/private/",
                            "type": "bearer",
                            "token": {"secret": "private"},
                        },
                    ]
                }
            },
            "secrets": {
                "general": {"env": "GENERAL_TOKEN"},
                "private": {"env": "PRIVATE_TOKEN"},
            },
        }
    )
    policy = RuntimeDownloaderCredentialPolicy.from_config(
        config,
        environ={"GENERAL_TOKEN": "general-token", "PRIVATE_TOKEN": "private-token"},
    )

    assert policy.authorization_for(httpx.URL("https://example.test/public")) is None
    assert (
        policy.authorization_for(
            httpx.URL("https://example.test/models/private/model?download=1")
        )
        == b"Bearer private-token"
    )
    assert "private-token" not in repr(policy)
