"""Runtime state persistence for container startup coordination."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from comfyui_docker_helper.config.models import ConfigModel
from comfyui_docker_helper.config.value_validation import (
    has_control_characters,
    replace_control_characters,
)

RUNTIME_STATE_PATH = Path("/var/lib/cdh/runtime/state.json")
RUNTIME_STATE_SCHEMA_VERSION = 1

_DIGEST_KEY_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ERROR_DOWNLOAD_STATUSES = frozenset({"failed", "exhausted"})

type RuntimeDownloadDigestKey = Annotated[
    str, Field(pattern=_DIGEST_KEY_PATTERN.pattern)
]


class RuntimeStateError(ValueError):
    """Runtime state file cannot be loaded, validated, or persisted."""


class RuntimeDownloadEntry(ConfigModel):
    """Persisted state for one runtime file download target."""

    target: str
    download_mode: Literal["sync", "async"]
    status: Literal[
        "pending",
        "downloading",
        "completed",
        "failed",
        "exhausted",
        "skipped",
    ]
    attempts: int = Field(ge=0)
    attempt_run_id: str = Field(min_length=1)
    last_error: str | None = None
    updated_at: datetime

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        if not value:
            raise ValueError("target must be a non-empty relative POSIX path")
        if "\\" in value:
            raise ValueError("target must use POSIX separators")
        if has_control_characters(value):
            raise ValueError("target must not contain control characters")

        raw_parts = value.split("/")
        if any(part in ("", ".", "..") for part in raw_parts):
            raise ValueError("target must not contain empty, dot, or dotdot segments")

        path = PurePosixPath(value)
        if path.is_absolute():
            raise ValueError("target must be a relative POSIX path")
        return path.as_posix()

    @field_validator("attempt_run_id")
    @classmethod
    def _validate_attempt_run_id(cls, value: str) -> str:
        if not value:
            raise ValueError("attempt_run_id must be non-empty")
        return value

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "updated_at")

    @model_validator(mode="after")
    def _normalize_last_error(self) -> Self:
        if self.status not in _ERROR_DOWNLOAD_STATUSES:
            self.last_error = None
        elif self.last_error is not None:
            self.last_error = summarize_runtime_error(self.last_error)
        return self


class RuntimeDownloadsState(ConfigModel):
    """Persisted runtime file download entries keyed by source digest."""

    entries: dict[RuntimeDownloadDigestKey, RuntimeDownloadEntry] = Field(
        default_factory=dict
    )


class RuntimeState(ConfigModel):
    """Top-level persisted runtime state."""

    schema_version: Literal[1] = RUNTIME_STATE_SCHEMA_VERSION
    updated_at: datetime
    run_id: str = Field(min_length=1)
    downloads: RuntimeDownloadsState = Field(default_factory=RuntimeDownloadsState)

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, value: str) -> str:
        if not value:
            raise ValueError("run_id must be non-empty")
        return value

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "updated_at")


def load_runtime_state(path: Path = RUNTIME_STATE_PATH) -> RuntimeState:
    """Load and validate the runtime state file."""
    try:
        payload = path.read_text(encoding="utf-8")
        json.loads(payload)
        return RuntimeState.model_validate_json(payload)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise RuntimeStateError(f"failed to read runtime state: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeStateError(f"runtime state is not valid JSON: {path}") from error
    except ValidationError as error:
        raise RuntimeStateError(f"runtime state is invalid: {path}") from error


def write_runtime_state(path: Path, state: RuntimeState) -> None:
    """Atomically write runtime state as deterministic JSON."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeStateError(
            f"failed to create runtime state parent: {path}"
        ) from error

    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = _runtime_state_json(state)

    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_parent(path.parent)
    except OSError as error:
        _remove_temp(temp_path)
        raise RuntimeStateError(f"failed to write runtime state: {path}") from error


def prepare_runtime_state_for_start(
    path: Path = RUNTIME_STATE_PATH,
    *,
    active_downloads: bool,
    run_id: str,
    now: datetime,
) -> RuntimeState | None:
    """Load or create runtime state for a container start."""
    if not active_downloads:
        return None

    _validate_aware_datetime(now, "now")
    if not run_id:
        raise RuntimeStateError("run_id must be non-empty")

    try:
        state = load_runtime_state(path)
    except FileNotFoundError:
        state = RuntimeState(
            schema_version=RUNTIME_STATE_SCHEMA_VERSION,
            updated_at=now,
            run_id=run_id,
            downloads=RuntimeDownloadsState(),
        )

    entries = {
        digest_key: _entry_for_run(entry, run_id=run_id, now=now)
        for digest_key, entry in state.downloads.entries.items()
    }
    prepared = RuntimeState(
        schema_version=RUNTIME_STATE_SCHEMA_VERSION,
        updated_at=now,
        run_id=run_id,
        downloads=RuntimeDownloadsState(entries=entries),
    )
    write_runtime_state(path, prepared)
    return prepared


def summarize_runtime_error(value: object, *, max_length: int = 512) -> str:
    """Return bounded, single-line runtime error text without classifying it."""
    text = "" if value is None else str(value)
    text = replace_control_characters(text)
    text = " ".join(text.split())

    if max_length < 3:
        return text[:max_length]
    if len(text) > max_length:
        return f"{text[: max_length - 3]}..."
    return text


def failed_runtime_download_entry(
    entry: RuntimeDownloadEntry,
    *,
    status: Literal["failed", "exhausted"],
    last_error: object,
    updated_at: datetime,
) -> RuntimeDownloadEntry:
    """Return a failed/exhausted entry with a bounded, single-line error."""
    return RuntimeDownloadEntry.model_validate(
        {
            **entry.model_dump(),
            "status": status,
            "last_error": summarize_runtime_error(last_error),
            "updated_at": updated_at,
        }
    )


def _runtime_state_json(state: RuntimeState) -> str:
    normalized = RuntimeState.model_validate(state.model_dump())
    data = normalized.model_dump(mode="json")
    return (
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    )


def _entry_for_run(
    entry: RuntimeDownloadEntry, *, run_id: str, now: datetime
) -> RuntimeDownloadEntry:
    if entry.attempt_run_id == run_id:
        return entry
    return entry.model_copy(
        update={"attempts": 0, "attempt_run_id": run_id, "updated_at": now}
    )


def _validate_aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _fsync_parent(parent: Path) -> None:
    try:
        directory_fd = os.open(parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _remove_temp(temp_path: Path) -> None:
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
