"""Tests for runtime state persistence."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.container import runtime_state
from comfyui_docker_helper.container.runtime_state import (
    RUNTIME_STATE_SCHEMA_VERSION,
    RuntimeDownloadEntry,
    RuntimeDownloadsState,
    RuntimeState,
    RuntimeStateError,
    load_runtime_state,
    prepare_runtime_state_for_start,
    sanitize_last_error,
    write_runtime_state,
)

DIGEST_KEY = "sha256:" + ("a" * 64)
OTHER_DIGEST_KEY = "sha256:" + ("b" * 64)
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _entry(
    *,
    target: str = "models/checkpoints/model.safetensors",
    status: str = "pending",
    attempts: int = 0,
    attempt_run_id: str = "run-1",
    updated_at: datetime = NOW,
    last_error: str | None = None,
) -> RuntimeDownloadEntry:
    return RuntimeDownloadEntry(
        target=target,
        download_mode="sync",
        status=status,
        attempts=attempts,
        attempt_run_id=attempt_run_id,
        last_error=last_error,
        updated_at=updated_at,
    )


def _state(
    *,
    run_id: str = "run-1",
    entries: dict[str, RuntimeDownloadEntry] | None = None,
    updated_at: datetime = NOW,
) -> RuntimeState:
    return RuntimeState(
        schema_version=RUNTIME_STATE_SCHEMA_VERSION,
        updated_at=updated_at,
        run_id=run_id,
        downloads=RuntimeDownloadsState(entries=entries or {}),
    )


def test_write_runtime_state_serializes_deterministic_json_shape(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    state = _state(entries={DIGEST_KEY: _entry()})

    write_runtime_state(path, state)

    assert path.read_text(encoding="utf-8") == (
        '{"downloads":{"entries":{"'
        f"{DIGEST_KEY}"
        '":{"attempt_run_id":"run-1","attempts":0,"download_mode":"sync",'
        '"last_error":null,"status":"pending","target":'
        '"models/checkpoints/model.safetensors",'
        '"updated_at":"2026-01-02T03:04:05Z"}}},"run_id":"run-1",'
        '"schema_version":1,"updated_at":"2026-01-02T03:04:05Z"}\n'
    )
    assert load_runtime_state(path) == state


def test_load_runtime_state_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "updated_at": "2026-01-02T03:04:05Z",
                "run_id": "run-1",
                "downloads": {"entries": {}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeStateError):
        load_runtime_state(path)


def test_runtime_download_entry_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _entry(status="unknown")


def test_runtime_state_rejects_extra_fields_and_invalid_digest_key() -> None:
    with pytest.raises(ValidationError):
        RuntimeState.model_validate(
            {
                "schema_version": 1,
                "updated_at": "2026-01-02T03:04:05Z",
                "run_id": "run-1",
                "downloads": {"entries": {}},
                "extra": True,
            }
        )

    with pytest.raises(ValidationError):
        RuntimeDownloadsState(entries={"sha256:" + ("A" * 64): _entry()})


@pytest.mark.parametrize(
    "target",
    [
        "",
        ".",
        "..",
        "../model.bin",
        "models/../model.bin",
        "models//file.bin",
        "models/./file.bin",
        "models/",
        "/models/model.bin",
        "a\\b",
    ],
)
def test_runtime_download_entry_rejects_invalid_target_paths(target: str) -> None:
    with pytest.raises(ValidationError):
        _entry(target=target)


@pytest.mark.parametrize("status", ["pending", "downloading", "completed", "skipped"])
def test_runtime_download_entry_clears_last_error_for_non_error_statuses(
    status: str,
) -> None:
    entry = _entry(
        status=status,
        last_error=(
            "https://example.com/model.safetensors?token=secret "
            "password=hunter2 /workspace/ComfyUI/model.safetensors"
        ),
    )

    assert entry.last_error is None


@pytest.mark.parametrize("status", ["failed", "exhausted"])
def test_runtime_download_entry_sanitizes_last_error_for_error_statuses(
    status: str,
) -> None:
    raw_error = (
        "failed\n"
        "https://example.com/model.safetensors?token=secret "
        "password=hunter2 Authorization: Bearer auth-secret bearer bearer-secret "
        "/workspace/ComfyUI/models/model.safetensors "
        f"{'x' * 600}"
    )

    entry = _entry(status=status, last_error=raw_error)

    assert entry.last_error is not None
    assert "\n" not in entry.last_error
    assert "https://example.com" not in entry.last_error
    assert "hunter2" not in entry.last_error
    assert "auth-secret" not in entry.last_error
    assert "bearer-secret" not in entry.last_error
    assert "/workspace/ComfyUI" not in entry.last_error
    assert len(entry.last_error) == 512
    assert entry.last_error.endswith("...")


def test_failed_runtime_download_entry_accepts_exception_last_error() -> None:
    entry = runtime_state.failed_runtime_download_entry(
        _entry(),
        status="failed",
        last_error=RuntimeError(
            "https://example.com/model.safetensors?token=secret "
            "password=hunter2 /absolute/path/model.safetensors"
        ),
        updated_at=NOW,
    )

    assert isinstance(entry, RuntimeDownloadEntry)
    assert entry.status == "failed"
    assert entry.last_error is not None
    assert "https://example.com" not in entry.last_error
    assert "token=secret" not in entry.last_error
    assert "hunter2" not in entry.last_error
    assert "/absolute/path" not in entry.last_error
    assert "[REDACTED_URL]" in entry.last_error
    assert "[REDACTED_PATH]" in entry.last_error


def test_atomic_write_replaces_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("old\n", encoding="utf-8")

    write_runtime_state(path, _state())

    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "run-1"


def test_write_runtime_state_renormalizes_mutated_last_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _state(entries={DIGEST_KEY: _entry(status="failed")})
    state.downloads.entries[DIGEST_KEY].last_error = (
        "https://example.com/model.safetensors?token=secret "
        "password=hunter2 /workspace/ComfyUI/model.safetensors"
    )

    write_runtime_state(path, state)

    payload = path.read_text(encoding="utf-8")
    assert "https://example.com" not in payload
    assert "token=secret" not in payload
    assert "hunter2" not in payload
    assert "/workspace/ComfyUI" not in payload
    assert "[REDACTED_URL]" in payload
    assert "[REDACTED_PATH]" in payload


def test_atomic_write_preserves_old_file_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    path.write_text("old\n", encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(runtime_state.os, "replace", fail_replace)

    with pytest.raises(RuntimeStateError):
        write_runtime_state(path, _state())

    assert path.read_text(encoding="utf-8") == "old\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_prepare_runtime_state_creates_missing_active_state_and_writes_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing" / "state.json"

    state = prepare_runtime_state_for_start(
        path,
        active_downloads=True,
        run_id="run-1",
        now=NOW,
    )

    assert state == _state()
    assert load_runtime_state(path) == state


def test_prepare_runtime_state_fails_corrupt_active_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeStateError):
        prepare_runtime_state_for_start(
            path,
            active_downloads=True,
            run_id="run-1",
            now=NOW,
        )


def test_prepare_runtime_state_ignores_invalid_inactive_state_without_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    assert (
        prepare_runtime_state_for_start(
            path,
            active_downloads=False,
            run_id="run-1",
            now=NOW,
        )
        is None
    )

    assert path.read_text(encoding="utf-8") == "{not-json"

    missing_path = tmp_path / "missing" / "state.json"
    assert (
        prepare_runtime_state_for_start(
            missing_path,
            active_downloads=False,
            run_id="run-1",
            now=NOW,
        )
        is None
    )
    assert not missing_path.parent.exists()


def test_prepare_runtime_state_resets_attempts_for_new_run_and_preserves_same_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    old_updated_at = datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
    state = _state(
        run_id="old-run",
        entries={
            DIGEST_KEY: _entry(
                attempts=3,
                attempt_run_id="old-run",
                updated_at=old_updated_at,
            ),
            OTHER_DIGEST_KEY: _entry(
                attempts=2,
                attempt_run_id="run-2",
                updated_at=old_updated_at,
            ),
        },
    )
    write_runtime_state(path, state)

    prepared = prepare_runtime_state_for_start(
        path,
        active_downloads=True,
        run_id="run-2",
        now=NOW,
    )

    assert prepared is not None
    assert prepared.run_id == "run-2"
    assert prepared.downloads.entries[DIGEST_KEY].attempts == 0
    assert prepared.downloads.entries[DIGEST_KEY].attempt_run_id == "run-2"
    assert prepared.downloads.entries[DIGEST_KEY].updated_at == NOW
    assert prepared.downloads.entries[OTHER_DIGEST_KEY].attempts == 2
    assert prepared.downloads.entries[OTHER_DIGEST_KEY].attempt_run_id == "run-2"
    assert prepared.downloads.entries[OTHER_DIGEST_KEY].updated_at == old_updated_at


def test_sanitize_last_error_redacts_and_truncates() -> None:
    raw = (
        "failed\n"
        "url=https://example.com/file.bin?token=abc "
        "password=hunter2 api_key:abcdef Authorization=BearerValue "
        "Authorization: Bearer auth-secret bearer bearer-secret "
        "path /workspace/ComfyUI/models/file.bin "
        "staging /var/lib/cdh/runtime/staging/file.tmp "
        "\x00 done"
    )

    sanitized = sanitize_last_error(raw, max_length=80)

    assert "\n" not in sanitized
    assert "\x00" not in sanitized
    assert "https://example.com" not in sanitized
    assert "hunter2" not in sanitized
    assert "abcdef" not in sanitized
    assert "auth-secret" not in sanitized
    assert "bearer-secret" not in sanitized
    assert "/workspace/ComfyUI" not in sanitized
    assert "/var/lib/cdh/runtime/staging" not in sanitized
    assert len(sanitized) == 80
    assert sanitized.endswith("...")


def test_state_json_does_not_serialize_source_urls_credentials_or_absolute_paths(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    state = _state(
        entries={
            DIGEST_KEY: _entry(
                status="failed",
                target="models/checkpoints/model.safetensors",
                last_error=(
                    "https://example.com/model.safetensors?token=secret "
                    "password=hunter2 Authorization: Bearer auth-secret "
                    "bearer bearer-secret /workspace/ComfyUI/models/model.safetensors "
                    "/var/lib/cdh/runtime/staging/model.tmp backend=httpx "
                    "failure_policy=continue"
                ),
            )
        }
    )

    write_runtime_state(path, state)

    payload = path.read_text(encoding="utf-8")
    assert "https://example.com" not in payload
    assert "hunter2" not in payload
    assert "token=secret" not in payload
    assert "auth-secret" not in payload
    assert "bearer-secret" not in payload
    assert "backend" not in payload
    assert "failure_policy" not in payload
    assert "/workspace/ComfyUI" not in payload
    assert "/var/lib/cdh/runtime/staging" not in payload
    assert "models/checkpoints/model.safetensors" in payload
    assert os.linesep
