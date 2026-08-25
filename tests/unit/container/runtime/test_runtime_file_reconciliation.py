"""Runtime file state reconciliation and cleanup tests."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.runtime_event_support import RecordingRuntimeEventSink
from tests.unit.container.runtime.runtime_file_support import (
    checksum as _checksum,
)
from tests.unit.container.runtime.runtime_file_support import (
    runtime_config as _config,
)
from tests.unit.container.runtime.runtime_file_support import (
    runtime_entry_for_item as _entry_for_item,
)
from tests.unit.container.runtime.runtime_file_support import runtime_file as _file
from tests.unit.container.runtime.runtime_file_support import (
    runtime_file_plan as _plan,
)
from tests.unit.container.runtime.runtime_file_support import runtime_state as _state
from tests.unit.container.runtime.runtime_file_support import (
    runtime_state_digest as _state_digest,
)

from comfyui_docker_helper.container.runtime.files.download import (
    process_runtime_file_downloads,
)
from comfyui_docker_helper.container.runtime.files.models import (
    RuntimeFilePlan,
    RuntimeFilePlanItem,
)
from comfyui_docker_helper.container.runtime.files.planning import (
    runtime_file_staging_target,
)
from comfyui_docker_helper.container.runtime.files.reconciliation import (
    reconcile_runtime_file_plan,
    validate_runtime_file_state_plan,
)
from comfyui_docker_helper.container.runtime.state import (
    RuntimeDownloadEntry,
    RuntimeResumeState,
    RuntimeStateError,
)
from comfyui_docker_helper.container.transfer import core as transfer_core
from comfyui_docker_helper.container.transfer.core import DownloadFilesError


def _resume_entry_for_item(
    item: RuntimeFilePlanItem,
    *,
    status: str = "pending",
) -> tuple[RuntimeDownloadEntry, Path, Path]:
    staging = runtime_file_staging_target(item)
    control = Path(f"{staging}.aria2")
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"partial")
    control.write_bytes(b"control")
    entry = _entry_for_item(item, status=status, downloader="aria2")
    entry.resume = RuntimeResumeState(
        staging_device=staging.stat().st_dev,
        staging_inode=staging.stat().st_ino,
        control_device=control.stat().st_dev,
        control_inode=control.stat().st_ino,
    )
    return entry, staging, control


def test_reconciliation_schedules_unproven_existing_target_for_core(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin"))
    plan.items[0].target.parent.mkdir()
    plan.items[0].target.write_bytes(b"existing")

    result = reconcile_runtime_file_plan(
        plan,
        _state(),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )

    assert result.download_plan.items == (replace(plan.items[0], downloader="httpx"),)
    assert result.items[0].status == "pending"
    assert result.items[0].scheduled is True


def test_reconciliation_reschedules_completed_checksum_for_live_verification(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin", checksum=_checksum(b"existing")))
    item = plan.items[0]
    item.target.parent.mkdir()
    item.target.write_bytes(b"existing")
    digest = _state_digest(item)

    result = reconcile_runtime_file_plan(
        plan,
        _state({digest: _entry_for_item(item)}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )

    assert result.download_plan.items == (replace(item, downloader="httpx"),)
    assert result.items[0].status == "pending"


def test_reconciliation_reuses_completed_checksum_free_regular_final(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin"))
    item = plan.items[0]
    item.target.parent.mkdir()
    item.target.write_bytes(b"existing")
    digest = _state_digest(item)

    result = reconcile_runtime_file_plan(
        plan,
        _state({digest: _entry_for_item(item)}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )

    assert result.download_plan.items == ()
    assert result.items[0].status == "completed"

    class CredentialMustRemainLazy:
        def authorization_for(self, _url: object) -> bytes | None:
            pytest.fail("completed target must not acquire its credential")

    assert (
        process_runtime_file_downloads(
            result.download_plan,
            config=_config(),
            backends={},
            credential_policy=CredentialMustRemainLazy(),
            event_sink=RecordingRuntimeEventSink(),
        )
        == ()
    )


# Completed state cannot admit a final through a symlinked parent.
def test_reconciliation_rejects_completed_final_through_symlinked_parent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "a.bin").write_bytes(b"outside")
    (root / "models").symlink_to(outside, target_is_directory=True)
    plan = _plan(root, _file("a.bin"))
    digest = _state_digest(plan.items[0])

    with pytest.raises(DownloadFilesError, match="not a real directory"):
        reconcile_runtime_file_plan(
            plan,
            _state({digest: _entry_for_item(plan.items[0])}),
            comfyui_path=root,
            default_downloader="httpx",
            resume_download=False,
        )


def test_reconciliation_reschedules_completed_entry_when_final_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin"))
    digest = _state_digest(plan.items[0])

    result = reconcile_runtime_file_plan(
        plan,
        _state({digest: _entry_for_item(plan.items[0])}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )

    assert result.download_plan.items == (replace(plan.items[0], downloader="httpx"),)
    assert result.state.downloads[digest].status == "pending"


def test_reconciliation_retains_unowned_stale_artifact_as_cleanup_pending(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    stale_plan = _plan(root, _file("a.bin"))
    stale_item = stale_plan.items[0]
    stale_digest = _state_digest(stale_item)
    stale = runtime_file_staging_target(stale_item)
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial")

    result = reconcile_runtime_file_plan(
        RuntimeFilePlan(items=()),
        _state({stale_digest: _entry_for_item(stale_item, status="pending")}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )

    assert result.stale_entry_digests == frozenset({stale_digest})
    assert [pending.digest for pending in result.cleanup_pending] == [stale_digest]
    assert result.cleanup_pending[0].reason == (
        "interrupted transfer lacks exact artifact authority"
    )
    assert result.state.downloads[stale_digest].status == "cleanup_pending"
    assert stale.read_bytes() == b"partial"


def test_invalid_state_identity_fails_before_stale_artifact_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    stale_plan = _plan(root, _file("a.bin"))
    stale_item = stale_plan.items[0]
    actual_digest = _state_digest(stale_item)
    mismatched_digest = f"sha256:{'f' * 64}"
    artifact = (
        stale_item.target.parent
        / ".cdh-staging"
        / f"cdh-{mismatched_digest.removeprefix('sha256:')}.part"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"do not touch")

    with pytest.raises(RuntimeStateError, match="remove the state file and restart"):
        reconcile_runtime_file_plan(
            RuntimeFilePlan(items=()),
            _state({mismatched_digest: _entry_for_item(stale_item)}),
            comfyui_path=root,
            default_downloader="httpx",
            resume_download=False,
        )

    assert actual_digest != mismatched_digest
    assert artifact.read_bytes() == b"do not touch"


def test_reconciliation_exactly_cleans_authorized_stale_resume_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    stale_plan = _plan(root, _file("a.bin", downloader="aria2"))
    stale_item = stale_plan.items[0]
    stale_digest = _state_digest(stale_item)
    staging = runtime_file_staging_target(stale_item)
    control = Path(f"{staging}.aria2")
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"partial")
    control.write_bytes(b"control")
    staging_stat = staging.stat()
    control_stat = control.stat()
    entry = _entry_for_item(stale_item, status="pending", downloader="aria2")
    entry.resume = RuntimeResumeState(
        staging_device=staging_stat.st_dev,
        staging_inode=staging_stat.st_ino,
        control_device=control_stat.st_dev,
        control_inode=control_stat.st_ino,
    )

    result = reconcile_runtime_file_plan(
        RuntimeFilePlan(items=()),
        _state({stale_digest: entry}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )

    assert result.state.downloads == {}
    assert result.cleanup_pending == ()
    assert not staging.exists()
    assert not control.exists()


def test_changed_identity_preserves_final_and_drops_clean_old_bookkeeping(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    old_plan = _plan(root, _file("a.bin", checksum=_checksum(b"old")))
    new_plan = _plan(root, _file("a.bin", checksum=_checksum(b"current")))
    old_item = old_plan.items[0]
    old_digest = _state_digest(old_item)
    new_item = new_plan.items[0]
    new_item.target.parent.mkdir(exist_ok=True)
    new_item.target.write_bytes(b"old final")

    result = reconcile_runtime_file_plan(
        new_plan,
        _state({old_digest: _entry_for_item(old_item)}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )

    assert old_digest not in result.state.downloads
    assert result.download_plan.items == (replace(new_item, downloader="httpx"),)
    assert new_item.target.read_bytes() == b"old final"


# Reconciliation cleans only exact old authority before assigning a shared
# transfer namespace to a changed desired identity.
def test_changed_overwrite_cleans_old_resume_and_preserves_final(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    old_item = _plan(root, _file("a.bin", downloader="aria2")).items[0]
    new_plan = _plan(root, _file("a.bin", downloader="aria2", overwrite=True))
    entry, staging, control = _resume_entry_for_item(old_item)
    old_digest = _state_digest(old_item)
    old_item.target.parent.mkdir(exist_ok=True)
    old_item.target.write_bytes(b"old final")

    result = reconcile_runtime_file_plan(
        new_plan,
        _state({old_digest: entry}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=True,
    )

    assert old_digest not in result.state.downloads
    assert not staging.exists()
    assert not control.exists()
    assert old_item.target.read_bytes() == b"old final"
    assert result.download_plan.items[0].resume_authority is None


def test_duplicate_serialized_transfer_namespace_is_invalid(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    item = _plan(root, _file("a.bin")).items[0]
    first = _entry_for_item(item)
    second = first.model_copy(update={"overwrite": True})
    entries = {
        _state_digest(item): first,
        _state_digest(replace(item, overwrite=True)): second,
    }

    with pytest.raises(RuntimeStateError, match="remove the state file and restart"):
        reconcile_runtime_file_plan(
            RuntimeFilePlan(items=()),
            _state(entries),
            comfyui_path=root,
            default_downloader="httpx",
            resume_download=False,
        )


def test_async_secondary_admission_rejects_non_digest_identity_mismatch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    item = replace(
        _plan(root, _file("a.bin", mode="async")).items[0],
        downloader="httpx",
    )
    digest = _state_digest(item)
    entry = _entry_for_item(item)
    assert entry.download_mode == "sync"

    with pytest.raises(RuntimeStateError, match="state identity differs"):
        validate_runtime_file_state_plan(
            RuntimeFilePlan(items=(item,)),
            _state({digest: entry}),
            comfyui_path=root,
            default_downloader="httpx",
            expected_run_id="run-1",
        )


@pytest.mark.parametrize(
    ("status", "state_run_id"),
    [
        ("completed", "run-1"),
        ("cleanup_pending", "run-1"),
        ("pending", "other-run"),
    ],
)
def test_async_secondary_admission_binds_current_pending_generation(
    tmp_path: Path,
    status: str,
    state_run_id: str,
) -> None:
    root = tmp_path / "ComfyUI"
    item = replace(
        _plan(root, _file("a.bin", mode="async")).items[0],
        downloader="httpx",
    )
    digest = _state_digest(item)
    base = _entry_for_item(item, status="pending")
    entry = RuntimeDownloadEntry.model_validate(
        {
            **base.model_dump(),
            "download_mode": "async",
            "status": status,
        }
    )

    with pytest.raises(RuntimeStateError):
        validate_runtime_file_state_plan(
            RuntimeFilePlan(items=(item,)),
            _state({digest: entry}, run_id=state_run_id),
            comfyui_path=root,
            default_downloader="httpx",
            expected_run_id="run-1",
        )


# Current resume policy must establish a clean namespace before clean scheduling.
def test_disabling_resume_cleans_exact_current_authority(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin", downloader="aria2"))
    item = plan.items[0]
    entry, staging, control = _resume_entry_for_item(item)
    digest = _state_digest(item)

    result = reconcile_runtime_file_plan(
        plan,
        _state({digest: entry}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )

    assert result.download_plan.items[0].resume_authority is None
    assert result.state.downloads[digest].resume is None
    assert not staging.exists()
    assert not control.exists()


def test_disabling_resume_cleanup_failure_retains_old_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin", downloader="aria2"))
    item = plan.items[0]
    entry, _, _ = _resume_entry_for_item(item)
    digest = _state_digest(item)
    state = _state({digest: entry})
    real_fsync = transfer_core.os.fsync

    def fail_staging_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(transfer_core.os, "fsync", fail_staging_directory_fsync)

    with pytest.raises(
        DownloadFilesError, match="current runtime transfer cleanup failed"
    ):
        reconcile_runtime_file_plan(
            plan,
            state,
            comfyui_path=root,
            default_downloader="httpx",
            resume_download=False,
        )

    assert state.downloads[digest].resume == entry.resume


def test_current_partial_resume_authority_converges_to_clean_schedule(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin", downloader="aria2"))
    item = plan.items[0]
    entry, staging, control = _resume_entry_for_item(item)
    control.unlink()
    digest = _state_digest(item)

    result = reconcile_runtime_file_plan(
        plan,
        _state({digest: entry}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=True,
    )

    assert result.download_plan.items[0].resume_authority is None
    assert result.state.downloads[digest].resume is None
    assert not staging.exists()


def test_current_resume_inode_mismatch_is_fatal_without_state_change(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin", downloader="aria2"))
    item = plan.items[0]
    entry, staging, control = _resume_entry_for_item(item)
    digest = _state_digest(item)
    state = _state({digest: entry})
    replacement = control.with_name(f"{control.name}.foreign")
    replacement.write_bytes(b"foreign")
    os.replace(replacement, control)

    with pytest.raises(DownloadFilesError, match="failed exact admission"):
        reconcile_runtime_file_plan(
            plan,
            state,
            comfyui_path=root,
            default_downloader="httpx",
            resume_download=True,
        )

    assert state.downloads[digest].resume == entry.resume
    assert staging.read_bytes() == b"partial"
    assert control.read_bytes() == b"foreign"


@pytest.mark.parametrize(
    ("status", "with_authority", "resume_download"),
    [("cleanup_pending", True, True), ("pending", False, False)],
)
def test_current_interrupted_state_reconciles_before_clean_schedule(
    tmp_path: Path,
    status: str,
    with_authority: bool,
    resume_download: bool,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin", downloader="aria2"))
    item = plan.items[0]
    if with_authority:
        entry, staging, control = _resume_entry_for_item(item, status=status)
    else:
        entry = _entry_for_item(item, status=status, downloader="aria2")
        staging = control = None
    digest = _state_digest(item)

    result = reconcile_runtime_file_plan(
        plan,
        _state({digest: entry}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=resume_download,
    )

    assert result.download_plan.items[0].resume_authority is None
    assert result.state.downloads[digest].status == "pending"
    if staging is not None and control is not None:
        assert not staging.exists()
        assert not control.exists()


def test_current_unowned_artifact_fails_before_schedule(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin"))
    item = plan.items[0]
    staging = runtime_file_staging_target(item)
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"foreign")

    with pytest.raises(DownloadFilesError, match="lacks exact cleanup authority"):
        reconcile_runtime_file_plan(
            plan,
            _state(),
            comfyui_path=root,
            default_downloader="httpx",
            resume_download=False,
        )

    assert staging.read_bytes() == b"foreign"


# Unrelated stale failures remain retryable bookkeeping and never block safe work.
def test_stale_cleanup_fsync_failure_retries_from_durable_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    stale_item = _plan(root, _file("old.bin", downloader="aria2")).items[0]
    entry, staging, control = _resume_entry_for_item(stale_item)
    digest = _state_digest(stale_item)
    real_fsync = transfer_core.os.fsync

    with monkeypatch.context() as patch:

        def fail_directory_fsync(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory fsync failed")
            real_fsync(fd)

        patch.setattr(transfer_core.os, "fsync", fail_directory_fsync)
        first = reconcile_runtime_file_plan(
            RuntimeFilePlan(items=()),
            _state({digest: entry}),
            comfyui_path=root,
            default_downloader="httpx",
            resume_download=False,
        )

    assert [pending.digest for pending in first.cleanup_pending] == [digest]
    assert first.cleanup_pending[0].reason == "staging cleanup failed"
    assert "directory fsync failed" not in first.cleanup_pending[0].reason
    assert first.state.downloads[digest].resume == entry.resume
    assert not staging.exists()
    assert not control.exists()

    second = reconcile_runtime_file_plan(
        RuntimeFilePlan(items=()),
        first.state,
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )
    assert second.state.downloads == {}


def test_partial_stale_authority_and_unsafe_leaf_remain_cleanup_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    partial_item = _plan(root, _file("partial.bin", downloader="aria2")).items[0]
    partial_entry, staging, control = _resume_entry_for_item(partial_item)
    unsafe_item = _plan(root, _file("unsafe.bin")).items[0]
    unsafe_staging = runtime_file_staging_target(unsafe_item)
    unsafe_staging.parent.mkdir(parents=True, exist_ok=True)
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    unsafe_staging.symlink_to(foreign)
    entries = {
        _state_digest(partial_item): partial_entry,
        _state_digest(unsafe_item): _entry_for_item(
            unsafe_item,
            status="pending",
        ),
    }

    real_unlink_owned_leaf = transfer_core._unlink_owned_leaf

    with monkeypatch.context() as patch:

        def leave_staging_after_control(leaf: Any) -> None:
            if leaf.name.endswith(".aria2"):
                real_unlink_owned_leaf(leaf)
                return
            raise DownloadFilesError("raw-staging-cleanup-sentinel")

        patch.setattr(
            transfer_core,
            "_unlink_owned_leaf",
            leave_staging_after_control,
        )
        result = reconcile_runtime_file_plan(
            RuntimeFilePlan(items=()),
            _state(entries),
            comfyui_path=root,
            default_downloader="httpx",
            resume_download=False,
        )

    assert {pending.digest for pending in result.cleanup_pending} == set(entries)
    assert any(
        pending.reason == "staging cleanup failed" for pending in result.cleanup_pending
    )
    assert all(
        "raw-staging-cleanup-sentinel" not in pending.reason
        for pending in result.cleanup_pending
    )
    assert staging.read_bytes() == b"partial"
    assert not control.exists()
    assert unsafe_staging.is_symlink()
    assert foreign.read_bytes() == b"foreign"

    partial_digest = _state_digest(partial_item)
    retry = reconcile_runtime_file_plan(
        RuntimeFilePlan(items=()),
        _state({partial_digest: result.state.downloads[partial_digest]}),
        comfyui_path=root,
        default_downloader="httpx",
        resume_download=False,
    )
    assert retry.state.downloads == {}
    assert not staging.exists()


def test_unresolved_stale_owner_cannot_share_current_transfer_namespace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    stale_item = _plan(root, _file("a.bin", overwrite=False)).items[0]
    current_plan = _plan(root, _file("a.bin", overwrite=True))
    staging = runtime_file_staging_target(stale_item)
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"foreign")

    with pytest.raises(DownloadFilesError, match="unresolved stale cleanup"):
        reconcile_runtime_file_plan(
            current_plan,
            _state(
                {
                    _state_digest(stale_item): _entry_for_item(
                        stale_item,
                        status="pending",
                    )
                }
            ),
            comfyui_path=root,
            default_downloader="httpx",
            resume_download=False,
        )

    assert staging.read_bytes() == b"foreign"
