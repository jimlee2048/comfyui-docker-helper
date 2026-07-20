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


# Atomic writes protect the state file from partial updates and foreign races.
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

    real_renameat2 = runtime_state._renameat2

    def fail_replace(*args: object, **kwargs: object) -> None:
        if kwargs["flags"] == 2:
            raise OSError("replace failed")
        real_renameat2(*args, **kwargs)

    monkeypatch.setattr(runtime_state, "_renameat2", fail_replace)

    with pytest.raises(RuntimeStateError):
        write_runtime_state(path, _state())

    assert load_runtime_state(path) == old_state
    assert not list(tmp_path.glob(".*.tmp"))


def test_state_parent_durability_failure_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    real_fsync = runtime_state.os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(runtime_state.os, "fsync", fail_directory_fsync)

    with pytest.raises(RuntimeStateError, match="failed to write runtime state"):
        write_runtime_state(path, _state())

    assert not list(tmp_path.glob(".*.tmp"))


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


def test_exchange_cleanup_name_replacement_preserves_foreign_and_owned_old(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    old_state = _state(run_id="old-run")
    new_state = _state(run_id="new-run")
    write_runtime_state(path, old_state)
    real_renameat2 = runtime_state._renameat2
    exchanged_temp: str | None = None
    injected = False
    owned_old = tmp_path / "owned-old.json"

    def replace_displaced_name(
        source_fd: int,
        source_name: str,
        target_fd: int,
        target_name: str,
        *,
        flags: int,
    ) -> None:
        nonlocal exchanged_temp, injected
        if flags == 2 and target_name == path.name:
            exchanged_temp = source_name
        if (
            flags == 1
            and source_name == exchanged_temp
            and ".cleanup-" in target_name
            and not injected
        ):
            injected = True
            real_renameat2(
                source_fd,
                source_name,
                source_fd,
                owned_old.name,
                flags=1,
            )
            (tmp_path / source_name).write_text("foreign", encoding="utf-8")
        real_renameat2(
            source_fd,
            source_name,
            target_fd,
            target_name,
            flags=flags,
        )

    monkeypatch.setattr(runtime_state, "_renameat2", replace_displaced_name)

    with pytest.raises(RuntimeStateError, match="temporary changed"):
        write_runtime_state(path, new_state)

    assert exchanged_temp is not None
    assert (tmp_path / exchanged_temp).read_text(encoding="utf-8") == "foreign"
    assert load_runtime_state(owned_old) == old_state
    assert load_runtime_state(path) == new_state


def test_precommit_temp_cleanup_name_replacement_preserves_both_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    state = _state(run_id="new-run")
    real_verify_current = RuntimeStateStore._verify_current
    verify_calls = 0

    def fail_precommit(store: RuntimeStateStore) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 2:
            raise RuntimeStateError("precommit stopped")
        real_verify_current(store)

    real_renameat2 = runtime_state._renameat2
    injected = False
    temp_name: str | None = None
    owned_temp = tmp_path / "owned-temp.json"

    def replace_temp_name(
        source_fd: int,
        source_name: str,
        target_fd: int,
        target_name: str,
        *,
        flags: int,
    ) -> None:
        nonlocal injected, temp_name
        if flags == 1 and ".cleanup-" in target_name and not injected:
            injected = True
            temp_name = source_name
            real_renameat2(
                source_fd,
                source_name,
                source_fd,
                owned_temp.name,
                flags=1,
            )
            (tmp_path / source_name).write_text("foreign", encoding="utf-8")
        real_renameat2(
            source_fd,
            source_name,
            target_fd,
            target_name,
            flags=flags,
        )

    monkeypatch.setattr(RuntimeStateStore, "_verify_current", fail_precommit)
    monkeypatch.setattr(runtime_state, "_renameat2", replace_temp_name)

    with pytest.raises(RuntimeStateError, match="precommit stopped"):
        write_runtime_state(path, state)

    assert temp_name is not None
    assert (tmp_path / temp_name).read_text(encoding="utf-8") == "foreign"
    assert load_runtime_state(owned_temp) == state
    assert not path.exists()


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
