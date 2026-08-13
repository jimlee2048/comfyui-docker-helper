"""Runtime downloader Secret projection and generation-cache tests."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
import pytest

from comfyui_docker_helper.config.runtime_models import RuntimeConfig
from comfyui_docker_helper.container import runtime_secret_session as subject
from comfyui_docker_helper.container.runtime_secret_session import (
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
    session = RuntimeSecretSession(
        {"hf_read": RuntimeSecretSource("env", "HF_TOKEN")},
        {},
    )

    with pytest.raises(RuntimeSecretSessionError) as first:
        session.bearer_token("hf_read")
    first.value.network_attempted = True

    with pytest.raises(RuntimeSecretSessionError) as second:
        session.bearer_token("hf_read")

    assert second.value is not first.value
    assert second.value.code == "source_unavailable"
    assert second.value.network_attempted is False


def test_unexpected_bearer_validator_failure_is_not_policy_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RuntimeSecretSession(
        {"hf_read": RuntimeSecretSource("env", "HF_TOKEN")},
        {"HF_TOKEN": "valid-token"},
    )

    def fail(_: bytes) -> None:
        raise RuntimeError("programming failure")

    monkeypatch.setattr(subject, "validate_bearer_token", fail)

    with pytest.raises(RuntimeError, match="programming failure"):
        session.bearer_token("hf_read")


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


def test_runtime_secret_file_enforces_shared_bounded_read(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_bytes(b"a" * 65_526)
    session = RuntimeSecretSession(
        {"hf_read": RuntimeSecretSource("file", os.fspath(token))},
        {},
    )

    with pytest.raises(RuntimeSecretSessionError) as raised:
        session.bearer_token("hf_read")

    assert raised.value.code == "source_unavailable"
    assert "a" * 100 not in str(raised.value)


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
