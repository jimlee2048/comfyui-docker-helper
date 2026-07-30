"""Tests for runtime state persistence."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.container import runtime_state
from comfyui_docker_helper.container.runtime_diagnostics import (
    runtime_error_reason,
    runtime_source_host,
    short_runtime_identity,
)
from comfyui_docker_helper.container.runtime_state import (
    RUNTIME_STATE_SCHEMA_VERSION,
    RuntimeDownloadEntry,
    RuntimeResumeState,
    RuntimeState,
    RuntimeStateError,
    RuntimeStateStore,
    load_runtime_state,
    prepare_runtime_state_for_start,
    summarize_runtime_error,
    write_runtime_state,
)

DIGEST_KEY = "sha256:" + ("a" * 64)


def _entry(
    *,
    target: str = "models/checkpoints/model.safetensors",
    status: str = "pending",
    source: str = "https://example.com/model.safetensors",
    checksum: str | None = None,
    overwrite: bool = False,
    downloader: str = "httpx",
    resume: RuntimeResumeState | None = None,
) -> RuntimeDownloadEntry:
    return RuntimeDownloadEntry(
        source=source,
        target=target,
        checksum=checksum,
        overwrite=overwrite,
        downloader=downloader,
        download_mode="sync",
        status=status,
        resume=resume,
    )


def _state(
    *,
    run_id: str = "run-1",
    entries: dict[str, RuntimeDownloadEntry] | None = None,
) -> RuntimeState:
    return RuntimeState(
        schema_version=RUNTIME_STATE_SCHEMA_VERSION,
        run_id=run_id,
        downloads=entries or {},
    )


# Schema and persistence tests pin the on-disk contract used across container
# restarts and async download resumes.
def test_write_runtime_state_serializes_deterministic_json_shape(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    state = _state(entries={DIGEST_KEY: _entry()})

    write_runtime_state(path, state)

    assert path.read_text(encoding="utf-8") == (
        '{"downloads":{"'
        f"{DIGEST_KEY}"
        '":{"checksum":null,"download_mode":"sync","downloader":"httpx",'
        '"overwrite":false,"resume":null,"source":'
        '"https://example.com/model.safetensors","status":"pending","target":'
        '"models/checkpoints/model.safetensors"}},"run_id":"run-1",'
        '"schema_version":1}\n'
    )
    assert load_runtime_state(path) == state


def test_load_runtime_state_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": "run-1",
                "downloads": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeStateError):
        load_runtime_state(path)


def test_load_runtime_state_requires_serialized_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "downloads": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeStateError, match=r"remove .*state.json and restart"):
        load_runtime_state(path)


# Status and identity validation keep async queue bookkeeping strict before it is
# written to the shared runtime state file.
def test_runtime_download_entry_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _entry(status="unknown")


def test_runtime_state_rejects_extra_fields_and_invalid_digest_key() -> None:
    with pytest.raises(ValidationError):
        RuntimeState.model_validate(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "downloads": {},
                "extra": True,
            }
        )

    with pytest.raises(ValidationError):
        _state(entries={"sha256:" + ("A" * 64): _entry()})


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
        "models/.cdh-staging",
    ],
)
def test_runtime_download_entry_rejects_invalid_target_paths(target: str) -> None:
    with pytest.raises(ValidationError):
        _entry(target=target)


# Atomic writes expose only complete state and define one observable commit point.
def test_atomic_write_replaces_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_runtime_state(path, _state(run_id="old-run"))

    write_runtime_state(path, _state())

    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "run-1"


def test_crash_leftover_temp_does_not_block_next_atomic_write(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    leftover = tmp_path / f".state.json.{os.getpid()}.old.tmp"
    leftover.write_text("partial", encoding="utf-8")

    write_runtime_state(path, _state())

    assert load_runtime_state(path) == _state()
    assert leftover.read_text(encoding="utf-8") == "partial"


def test_atomic_write_preserves_old_file_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    old_state = _state(run_id="old-run")
    write_runtime_state(path, old_state)

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(runtime_state.os, "replace", fail_replace)

    with pytest.raises(RuntimeStateError):
        write_runtime_state(path, _state())

    assert load_runtime_state(path) == old_state
    assert not list(tmp_path.glob(".*.tmp"))


def test_state_file_durability_failure_preserves_old_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    old_state = _state(run_id="old-run")
    write_runtime_state(path, old_state)
    real_fsync = runtime_state.os.fsync

    def fail_file_fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("file fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(runtime_state.os, "fsync", fail_file_fsync)

    with pytest.raises(RuntimeStateError, match="failed to write runtime state"):
        write_runtime_state(path, _state(run_id="new-run"))

    assert load_runtime_state(path) == old_state


def test_state_parent_durability_failure_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    write_runtime_state(path, _state(run_id="old-run"))
    new_state = _state(run_id="new-run")
    real_fsync = runtime_state.os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(runtime_state.os, "fsync", fail_directory_fsync)

    with pytest.raises(RuntimeStateError, match="failed to write runtime state"):
        write_runtime_state(path, new_state)

    assert load_runtime_state(path) == new_state


def test_final_state_verification_failure_is_fatal_without_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    write_runtime_state(path, _state(run_id="old-run"))
    new_state = _state(run_id="new-run")
    real_open_state_leaf = runtime_state._open_state_leaf

    def fail_final_open(*_args: object, **_kwargs: object) -> None:
        raise RuntimeStateError("final state verification failed")

    monkeypatch.setattr(runtime_state, "_open_state_leaf", fail_final_open)

    with pytest.raises(RuntimeStateError, match="final state verification failed"):
        write_runtime_state(path, new_state)

    monkeypatch.setattr(runtime_state, "_open_state_leaf", real_open_state_leaf)
    assert load_runtime_state(path) == new_state


def test_store_refreshes_held_leaf_after_each_successful_write(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_runtime_state(path, _state(run_id="old-run"))
    store = RuntimeStateStore.open(path, create_parent=False)
    assert store is not None
    assert store.read() == _state(run_id="old-run")

    try:
        store.write(_state(run_id="middle-run"))
        store.write(_state(run_id="new-run"))
    finally:
        store.close()

    assert load_runtime_state(path) == _state(run_id="new-run")


# Descriptor admission rejects non-private state leaves before parsing or mutation.
@pytest.mark.parametrize("leaf_kind", ["symlink", "fifo", "directory", "hardlink"])
def test_runtime_state_rejects_unsafe_leaf_types(
    tmp_path: Path,
    leaf_kind: str,
) -> None:
    path = tmp_path / "state.json"
    if leaf_kind == "symlink":
        foreign = tmp_path / "foreign.json"
        foreign.write_text("foreign", encoding="utf-8")
        path.symlink_to(foreign)
    elif leaf_kind == "fifo":
        os.mkfifo(path)
    elif leaf_kind == "directory":
        path.mkdir()
    else:
        foreign = tmp_path / "foreign.json"
        foreign.write_text("foreign", encoding="utf-8")
        os.link(foreign, path)

    with pytest.raises(RuntimeStateError):
        load_runtime_state(path)


def test_runtime_state_rejects_wrong_owner_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    write_runtime_state(path, _state())
    actual_uid = path.stat().st_uid
    monkeypatch.setattr(runtime_state.os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(RuntimeStateError, match="unexpected owner"):
        load_runtime_state(path)


def test_runtime_state_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    (real_parent / "state.json").write_text("foreign", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(RuntimeStateError, match="parent cannot be opened safely"):
        load_runtime_state(alias / "state.json")


def test_runtime_state_store_rejects_replaced_parent_and_leaf(tmp_path: Path) -> None:
    parent = tmp_path / "runtime"
    path = parent / "state.json"
    write_runtime_state(path, _state())
    store = RuntimeStateStore.open(path, create_parent=False)
    assert store is not None
    assert store.read() == _state()

    detached = tmp_path / "detached"
    parent.rename(detached)
    parent.mkdir()
    foreign = parent / "state.json"
    foreign.write_text("foreign", encoding="utf-8")
    try:
        with pytest.raises(RuntimeStateError, match="parent changed"):
            store.write(_state(run_id="new-run"))
    finally:
        store.close()

    assert foreign.read_text(encoding="utf-8") == "foreign"
    assert load_runtime_state(detached / "state.json") == _state()


def test_runtime_state_store_rejects_replaced_leaf_without_touching_foreign(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    write_runtime_state(path, _state())
    store = RuntimeStateStore.open(path, create_parent=False)
    assert store is not None
    assert store.read() == _state()
    original = tmp_path / "original.json"
    path.rename(original)
    path.write_text("foreign", encoding="utf-8")
    try:
        with pytest.raises(RuntimeStateError, match="changed during operation"):
            store.write(_state(run_id="new-run"))
    finally:
        store.close()

    assert path.read_text(encoding="utf-8") == "foreign"
    assert load_runtime_state(original) == _state()


def test_runtime_state_temp_collision_preserves_foreign_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    token = "fixed"
    collision = tmp_path / f".state.json.{os.getpid()}.{token}.tmp"
    collision.write_text("foreign", encoding="utf-8")
    monkeypatch.setattr(runtime_state.secrets, "token_hex", lambda _: token)

    with pytest.raises(RuntimeStateError, match="failed to write runtime state"):
        write_runtime_state(path, _state())

    assert collision.read_text(encoding="utf-8") == "foreign"
    assert not path.exists()


def test_precommit_recheck_rejects_changed_leaf_without_touching_foreign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    old_state = _state(run_id="old-run")
    write_runtime_state(path, old_state)
    original = tmp_path / "original.json"
    real_fsync = runtime_state.os.fsync
    injected = False

    def replace_state_after_temp_fsync(fd: int) -> None:
        nonlocal injected
        real_fsync(fd)
        if not injected and stat.S_ISREG(os.fstat(fd).st_mode):
            injected = True
            path.rename(original)
            path.write_text("foreign", encoding="utf-8")

    monkeypatch.setattr(runtime_state.os, "fsync", replace_state_after_temp_fsync)

    with pytest.raises(RuntimeStateError, match="changed during operation"):
        write_runtime_state(path, _state(run_id="new-run"))

    assert path.read_text(encoding="utf-8") == "foreign"
    assert load_runtime_state(original) == old_state


# Startup preparation validates existing state and binds one top-level generation;
# reconciliation owns the first durable write.
def test_prepare_runtime_state_creates_missing_desired_state_without_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing" / "state.json"

    state = prepare_runtime_state_for_start(
        path,
        desired_downloads=True,
        run_id="run-1",
    )

    assert state == _state()
    assert not path.parent.exists()


def test_prepare_runtime_state_fails_corrupt_active_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeStateError):
        prepare_runtime_state_for_start(
            path,
            desired_downloads=True,
            run_id="run-1",
        )


def test_prepare_runtime_state_rejects_invalid_existing_empty_plan_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(
        RuntimeStateError,
        match=r"remove .*state.json and restart",
    ):
        prepare_runtime_state_for_start(
            path,
            desired_downloads=False,
            run_id="run-1",
        )

    assert path.read_text(encoding="utf-8") == "{not-json"

    missing_path = tmp_path / "missing" / "state.json"
    assert (
        prepare_runtime_state_for_start(
            missing_path,
            desired_downloads=False,
            run_id="run-1",
        )
        is None
    )
    assert not missing_path.parent.exists()


def test_prepare_runtime_state_rebinds_generation_without_rewriting_entries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    state = _state(
        run_id="old-run",
        entries={DIGEST_KEY: _entry()},
    )
    write_runtime_state(path, state)

    prepared = prepare_runtime_state_for_start(
        path,
        desired_downloads=True,
        run_id="run-2",
    )

    assert prepared is not None
    assert prepared.run_id == "run-2"
    assert prepared.downloads == state.downloads


# Runtime error summaries preserve authored text while making logs structurally safe.
def test_summarize_runtime_error_preserves_text_and_truncates() -> None:
    raw = (
        "failed\n"
        "url=https://example.com/file.bin?token=abc "
        "password=hunter2 api_key:abcdef Authorization=BearerValue "
        "Authorization: Bearer auth-secret bearer bearer-secret "
        "path /workspace/ComfyUI/models/file.bin "
        "staging /var/lib/cdh/runtime/staging/file.tmp "
        "\x00 done"
    )

    summarized = summarize_runtime_error(raw)
    bounded = summarize_runtime_error(raw, max_length=80)

    assert "\n" not in summarized
    assert "\x00" not in summarized
    assert "https://example.com/file.bin?token=abc" in summarized
    assert "password=hunter2" in summarized
    assert "api_key:abcdef" in summarized
    assert "Authorization: Bearer auth-secret" in summarized
    assert "/workspace/ComfyUI/models/file.bin" in summarized
    assert "/var/lib/cdh/runtime/staging/file.tmp" in summarized
    assert len(bounded) == 80
    assert bounded.endswith("...")


def test_runtime_diagnostics_source_host_and_short_identity_are_safe() -> None:
    assert (
        runtime_source_host(
            "https://user:password@example.com:8443/path/file.bin?token=secret#frag"
        )
        == "example.com"
    )
    assert runtime_source_host("not a url") == "unknown"
    assert (
        short_runtime_identity(
            "sha256:37b76480b800111122223333444455556666777788889999aaaabbbbccccdddd"
        )
        == "sha256:37b76480b800"
    )


def test_runtime_error_reason_preserves_authored_text_and_quotes_it() -> None:
    reason = runtime_error_reason(
        "failed https://user:pass@example.com/a.bin?token=url-secret "
        "SSH_PASSWORD=hunter2 password=plain token:abc123 "
        "Authorization: Bearer bearer-secret"
    )

    assert reason.startswith('"')
    assert reason.endswith('"')
    assert "https://user:pass@example.com/a.bin?token=url-secret" in reason
    assert "SSH_PASSWORD=hunter2" in reason
    assert "password=plain" in reason
    assert "token:abc123" in reason
    assert "Bearer bearer-secret" in reason
