"""Runtime file planning, reconciliation, and transfer-policy tests."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from comfyui_docker_helper.config.runtime_models import RuntimeConfig
from comfyui_docker_helper.container import transfer_core
from comfyui_docker_helper.container.runtime_download_state import (
    RuntimeDownloadStateWriter,
)
from comfyui_docker_helper.container.runtime_files import (
    RuntimeFileDownloadError,
    RuntimeFilePlan,
    RuntimeFilePlanError,
    RuntimeFilePlanItem,
    build_runtime_file_plan,
    canonical_runtime_file_identity_bytes,
    download_runtime_files,
    process_runtime_file_downloads,
    reconcile_runtime_file_plan,
    runtime_file_identity_digest,
    runtime_file_staging_target,
    runtime_file_state_identity_digest,
    validate_runtime_file_state_plan,
)
from comfyui_docker_helper.container.runtime_state import (
    RuntimeDownloadEntry,
    RuntimeResumeState,
    RuntimeState,
    RuntimeStateError,
)
from comfyui_docker_helper.container.transfer_core import (
    DownloaderSettings,
    DownloadFilesError,
    DownloadStatus,
    Logger,
    PreservedTransferCleanupError,
    ResumeAuthority,
    TransportCancelled,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportOutcome,
    TransportRequest,
    TransportRetryable,
    TransportSuccess,
    VerificationStatus,
)


def _checksum(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _identities(error: RuntimeFilePlanError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


def _state(
    entries: dict[str, RuntimeDownloadEntry] | None = None,
    *,
    run_id: str = "run-1",
) -> RuntimeState:
    return RuntimeState(
        schema_version=1,
        run_id=run_id,
        downloads=entries or {},
    )


def _entry(
    *,
    target: str = "models/a.bin",
    status: str = "completed",
    source: str = "https://example.test/a.bin",
    checksum: str | None = None,
    overwrite: bool = False,
    downloader: str = "httpx",
) -> RuntimeDownloadEntry:
    return RuntimeDownloadEntry(
        source=source,
        target=target,
        checksum=checksum,
        overwrite=overwrite,
        downloader=downloader,
        download_mode="sync",
        status=status,
    )


def _entry_for_item(
    item: RuntimeFilePlanItem,
    *,
    status: str = "completed",
    downloader: str = "httpx",
) -> RuntimeDownloadEntry:
    return _entry(
        source=item.url,
        target=item.relative_target,
        checksum=item.checksum,
        overwrite=item.overwrite,
        downloader=downloader,
        status=status,
    )


def _state_digest(
    item: RuntimeFilePlanItem,
    *,
    default_downloader: str = "httpx",
) -> str:
    return runtime_file_state_identity_digest(
        item,
        default_downloader=default_downloader,
    )


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


def _config(
    *,
    policy: str = "fail",
    attempts: int = 2,
    default: str = "httpx",
    resume: bool = False,
) -> RuntimeConfig:
    document = RuntimeConfig().model_dump(mode="python")
    document["cdh"]["download_failure_policy"] = policy
    document["cdh"]["download_max_attempts"] = attempts
    document["cdh"]["default_downloader"] = default
    document["cdh"]["downloader"]["aria2"]["resume_download"] = resume
    return RuntimeConfig.model_validate(document)


def _plan(root: Path, *files: dict) -> RuntimeFilePlan:
    root.mkdir(parents=True, exist_ok=True)
    return build_runtime_file_plan(files, comfyui_path=root)


def _file(
    name: str,
    *,
    downloader: str | None = None,
    overwrite: bool = False,
    checksum: str | None = None,
    mode: str | None = None,
) -> dict:
    item = {
        "url": f"https://example.test/{name}",
        "dir": "models",
        "filename": name,
        "overwrite": overwrite,
    }
    if downloader is not None:
        item["downloader"] = downloader
    if checksum is not None:
        item["checksum"] = checksum
    if mode is not None:
        item["download_mode"] = mode
    return item


class FakeBackend:
    """Write only supplied staging and expose scripted transport failures."""

    def __init__(
        self,
        payload: bytes = b"downloaded",
        *,
        failures: list[Exception | TransportOutcome] | None = None,
    ) -> None:
        self.payload = payload
        self.failures = failures or []
        self.calls: list[tuple[TransportRequest, DownloaderSettings]] = []
        self.prepare_calls: list[DownloaderSettings] = []
        self.entered = False
        self.exited = False

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        self.calls.append((request, settings))
        with request.sink.open_for_write() as output:
            output.write(self.payload)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        return TransportSuccess(
            length=len(self.payload), namespace="httpx", http_status=200
        )

    def prepare(self, settings: DownloaderSettings) -> None:
        self.prepare_calls.append(settings)

    def __enter__(self) -> FakeBackend:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.exited = True


class FakeAria2Factory:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.logs: list[Logger] = []

    def __call__(self, *, log: Logger) -> FakeBackend:
        self.logs.append(log)
        return self.backend


# Planning preserves ordered user intent and canonical checksum identity.
def test_runtime_plan_projects_order_targets_modes_and_checksum(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    uppercase = f"sha256:{'AB' * 32}"

    plan = _plan(
        root,
        _file("a.bin", checksum=uppercase),
        _file("b.bin", downloader="aria2", mode="async"),
    )

    assert [item.relative_target for item in plan.items] == [
        "models/a.bin",
        "models/b.bin",
    ]
    assert [item.download_mode for item in plan.items] == ["sync", "async"]
    assert plan.items[0].checksum == uppercase.lower()
    assert plan.items[0].target == root / "models" / "a.bin"


def test_runtime_identity_includes_checksum_but_not_execution_policy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    item = _plan(root, _file("a.bin", checksum=_checksum(b"one"))).items[0]
    identity = canonical_runtime_file_identity_bytes(item)

    assert b'"checksum":"sha256:' in identity
    assert b'"target":"models/a.bin"' in identity
    baseline = runtime_file_identity_digest(item)
    assert runtime_file_identity_digest(replace(item, overwrite=True)) == baseline
    assert (
        runtime_file_identity_digest(replace(item, download_mode="async")) == baseline
    )
    assert (
        runtime_file_identity_digest(replace(item, checksum=_checksum(b"two")))
        != baseline
    )


def test_runtime_state_identity_tracks_backend_and_overwrite_but_not_mode(
    tmp_path: Path,
) -> None:
    item = replace(
        _plan(tmp_path / "ComfyUI", _file("a.bin")).items[0],
        downloader="httpx",
    )
    baseline = runtime_file_state_identity_digest(item)

    assert (
        runtime_file_state_identity_digest(replace(item, download_mode="async"))
        == baseline
    )
    assert runtime_file_state_identity_digest(replace(item, overwrite=True)) != baseline
    assert (
        runtime_file_state_identity_digest(replace(item, downloader="aria2"))
        != baseline
    )
    assert runtime_file_identity_digest(
        replace(item, overwrite=True)
    ) == runtime_file_identity_digest(item)
    assert runtime_file_identity_digest(
        replace(item, downloader="aria2")
    ) == runtime_file_identity_digest(item)


def test_runtime_staging_uses_transfer_identity_digest(tmp_path: Path) -> None:
    item = _plan(tmp_path / "ComfyUI", _file("a.bin")).items[0]
    digest = runtime_file_identity_digest(item)

    assert runtime_file_staging_target(item) == (
        item.target.parent
        / ".cdh-staging"
        / f"cdh-{digest.removeprefix('sha256:')}.part"
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("dir", "../models", "runtime_file.parent_directory_segment"),
        ("filename", "../a.bin", "runtime_file.invalid_filename"),
        ("url", "file:///tmp/a", "runtime_file.invalid_url"),
        ("checksum", "sha256:bad", "schema.value_error"),
        ("checksum", 123, "schema.string_type"),
        ("download_mode", "later", "schema.literal_error"),
    ],
)
def test_runtime_plan_rejects_invalid_file_fields(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    item = _file("a.bin")
    item[field] = value

    with pytest.raises(RuntimeFilePlanError) as captured:
        _plan(tmp_path / "ComfyUI", item)

    assert _identities(captured.value) == [(("files", 0, field), code)]


# Reconciliation indexes state; the shared core remains the target-byte authority.
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
    control.unlink()
    control.write_bytes(b"foreign")

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
    assert "directory fsync failed" in first.cleanup_pending[0].reason
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
            raise DownloadFilesError("staging cleanup failed")

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


# Runtime policy wraps attempts/state while both backends share one transfer core.
def test_runtime_consumer_selects_backends_and_returns_typed_outcomes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    checksum = _checksum(b"downloaded")
    plan = _plan(
        root,
        _file("a.bin", downloader="httpx", checksum=checksum),
        _file("b.bin", downloader="aria2"),
    )
    httpx_backend = FakeBackend()
    aria2_backend = FakeBackend()

    results = process_runtime_file_downloads(
        plan,
        config=_config(),
        backends={"httpx": httpx_backend, "aria2": aria2_backend},
        log=lambda _: None,
    )

    assert [result.status for result in results] == [
        DownloadStatus.DOWNLOADED,
        DownloadStatus.DOWNLOADED,
    ]
    assert results[0].outcome.verification is VerificationStatus.VERIFIED
    assert results[0].outcome.observed_checksum == checksum
    assert results[1].outcome.observed_checksum is None
    assert results[0].staging_target == runtime_file_staging_target(
        plan.items[0],
    )
    assert plan.items[0].target.read_bytes() == b"downloaded"
    assert httpx_backend.calls[0][0].sink.display_path != plan.items[0].target


def test_runtime_retryable_failure_retries_then_completes(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"))
    backend = FakeBackend(
        failures=[TransportRetryable(TransportDiagnostic("httpx", "temporary"))]
    )

    result = process_runtime_file_downloads(
        plan,
        config=_config(attempts=2),
        backends={"httpx": backend},
        log=lambda _: None,
    )

    assert result[0].status is DownloadStatus.DOWNLOADED
    assert len(backend.calls) == 2


def test_runtime_continue_applies_only_after_retryable_exhaustion(
    tmp_path: Path,
) -> None:
    plan = _plan(
        tmp_path / "ComfyUI",
        _file("a.bin"),
        _file("b.bin"),
    )
    backend = FakeBackend(
        failures=[
            TransportRetryable(TransportDiagnostic("httpx", "temporary")),
            TransportRetryable(TransportDiagnostic("httpx", "temporary")),
        ]
    )

    results = process_runtime_file_downloads(
        plan,
        config=_config(policy="continue", attempts=2),
        backends={"httpx": backend},
        log=lambda _: None,
    )

    assert [result.item.filename for result in results] == ["b.bin"]
    assert len(backend.calls) == 3


def test_runtime_fail_policy_stops_after_retryable_exhaustion(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"), _file("b.bin"))
    backend = FakeBackend(
        failures=[
            TransportRetryable(TransportDiagnostic("httpx", "temporary")),
            TransportRetryable(TransportDiagnostic("httpx", "temporary")),
        ]
    )

    with pytest.raises(RuntimeFileDownloadError, match="runtime file download"):
        process_runtime_file_downloads(
            plan,
            config=_config(policy="fail", attempts=2),
            backends={"httpx": backend},
            log=lambda _: None,
        )

    assert len(backend.calls) == 2
    assert not plan.items[1].target.exists()


# Terminal item failures apply runtime policy once without spending retry budget.
@pytest.mark.parametrize("policy", ["continue", "fail"])
def test_runtime_terminal_failure_applies_policy_without_retry(
    tmp_path: Path,
    policy: str,
) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"), _file("b.bin"))
    backend = FakeBackend(
        failures=[TransportOrdinaryTerminal(TransportDiagnostic("aria2", "not found"))]
    )
    statuses: list[str] = []
    logs: list[str] = []

    if policy == "fail":
        with pytest.raises(RuntimeFileDownloadError):
            process_runtime_file_downloads(
                plan,
                config=_config(policy=policy, attempts=3),
                backends={"httpx": backend},
                state_observer=lambda _item, status, **_: statuses.append(status),
                log=logs.append,
            )
    else:
        results = process_runtime_file_downloads(
            plan,
            config=_config(policy=policy, attempts=3),
            backends={"httpx": backend},
            state_observer=lambda _item, status, **_: statuses.append(status),
            log=logs.append,
        )
        assert [result.item.filename for result in results] == ["b.bin"]

    assert len(backend.calls) == (1 if policy == "fail" else 2)
    assert statuses[0] == "failed"
    if policy == "continue":
        assert statuses[-1] == "completed"
    assert any(
        "Runtime download failed:" in line and "status=failed" in line for line in logs
    )
    assert all("status=exhausted" not in line for line in logs)


# Cleanup failure must not overwrite the exact persisted authority it could not use.
def test_skip_cleanup_failure_preserves_persisted_resume_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ComfyUI"
    initial = _plan(root, _file("a.bin", downloader="aria2")).items[0]
    staging = runtime_file_staging_target(initial)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"partial")
    control = Path(f"{staging}.aria2")
    control.write_bytes(b"control")
    authority = ResumeAuthority(
        identity_digest=runtime_file_identity_digest(initial),
        staging_device=staging.stat().st_dev,
        staging_inode=staging.stat().st_ino,
        control_device=control.stat().st_dev,
        control_inode=control.stat().st_ino,
    )
    item = replace(initial, resume_authority=authority)
    item.target.parent.mkdir(parents=True, exist_ok=True)
    item.target.write_bytes(b"existing")
    control.unlink()
    staging.unlink()
    staging.parent.rmdir()

    def fail_absence_fsync(_fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(transfer_core.os, "fsync", fail_absence_fsync)
    backend = FakeBackend()
    observed: list[str] = []

    with pytest.raises(
        PreservedTransferCleanupError,
        match="staging absence could not be made durable",
    ):
        process_runtime_file_downloads(
            RuntimeFilePlan(items=(item,)),
            config=_config(default="aria2", resume=True),
            backends={"aria2": backend},
            state_observer=lambda _item, status, **_: observed.append(status),
            log=lambda _: None,
        )

    assert observed == []
    assert backend.calls == []
    assert item.resume_authority == authority
    assert not staging.parent.exists()


def test_runtime_continue_cannot_mask_local_target_invariant(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"))
    plan.items[0].target.mkdir(parents=True)
    backend = FakeBackend()
    statuses: list[str] = []

    with pytest.raises(DownloadFilesError, match="not a regular file"):
        process_runtime_file_downloads(
            plan,
            config=_config(policy="continue"),
            backends={"httpx": backend},
            state_observer=lambda _item, status, **_: statuses.append(status),
            log=lambda _: None,
        )

    assert backend.calls == []
    assert statuses == ["failed"]


def test_runtime_cancelled_transfer_stops_without_exhausted_failure(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"), _file("b.bin"))
    backend = FakeBackend(
        failures=[TransportCancelled(TransportDiagnostic("httpx", "cancelled"))]
    )
    statuses: list[str] = []

    results = process_runtime_file_downloads(
        plan,
        config=_config(),
        backends={"httpx": backend},
        state_observer=lambda item, status, **_: statuses.append(status),
        log=lambda _: None,
    )

    assert results == ()
    assert statuses == ["failed"]
    assert len(backend.calls) == 1


def test_required_completion_state_persistence_failure_is_fatal(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"))
    backend = FakeBackend()
    observations: list[str] = []

    def fail_state_write(
        item: RuntimeFilePlanItem,
        status: str,
        *,
        error: object | None = None,
        resume_authority: ResumeAuthority | None = None,
    ) -> None:
        del item, error, resume_authority
        observations.append(status)
        raise RuntimeStateError("state write failed")

    with pytest.raises(RuntimeStateError, match="state write failed"):
        process_runtime_file_downloads(
            plan,
            config=_config(),
            backends={"httpx": backend},
            state_observer=fail_state_write,
            log=lambda _: None,
        )

    assert observations == ["completed"]
    assert len(backend.calls) == 1
    assert plan.items[0].target.read_bytes() == b"downloaded"


# The state writer persists only next-start recovery changes, not repeated
# current-run failure telemetry.
def test_runtime_state_writer_persists_only_recovery_changes(tmp_path: Path) -> None:
    class RecordingStore:
        def __init__(self) -> None:
            self.writes: list[RuntimeState] = []

        def write(self, state: RuntimeState) -> None:
            self.writes.append(state)

    item = replace(
        _plan(tmp_path / "ComfyUI", _file("a.bin", downloader="aria2")).items[0],
        downloader="aria2",
    )
    digest = _state_digest(item, default_downloader="aria2")
    store = RecordingStore()
    writer = RuntimeDownloadStateWriter(
        store,
        _state({digest: _entry_for_item(item, status="pending", downloader="aria2")}),
    )
    authority = ResumeAuthority(
        identity_digest=runtime_file_identity_digest(item),
        staging_device=1,
        staging_inode=2,
        control_device=3,
        control_inode=4,
    )

    writer(item, "failed")
    assert store.writes == []

    writer(item, "failed", resume_authority=authority)
    assert len(store.writes) == 1
    assert store.writes[-1].downloads[digest].status == "pending"
    assert store.writes[-1].downloads[digest].resume == (
        RuntimeResumeState.from_authority(authority)
    )

    writer(item, "failed", resume_authority=authority)
    assert len(store.writes) == 1

    writer(item, "failed")
    assert len(store.writes) == 2
    assert store.writes[-1].downloads[digest].status == "pending"
    assert store.writes[-1].downloads[digest].resume is None

    writer(item, "completed")
    assert len(store.writes) == 3
    assert store.writes[-1].downloads[digest].status == "completed"
    assert store.writes[-1].downloads[digest].resume is None


def test_quiescent_aria_resume_authority_round_trips_through_reconciliation(
    tmp_path: Path,
) -> None:
    class PreservingAriaBackend:
        def download(
            self,
            request: TransportRequest,
            settings: DownloaderSettings,
        ) -> TransportOutcome:
            del settings
            with request.sink.open_for_write() as output:
                output.write(b"partial")
            control = (
                Path(request.sink.aria2_directory) / f"{request.sink.aria2_name}.aria2"
            )
            control.write_bytes(b"control")
            return TransportRetryable(TransportDiagnostic("aria2", "temporary"))

    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin", downloader="aria2"))
    observed: list[tuple[str, ResumeAuthority | None]] = []

    def observe(
        item: RuntimeFilePlanItem,
        status: str,
        *,
        error: object | None = None,
        resume_authority: ResumeAuthority | None = None,
    ) -> None:
        del item, error
        observed.append((status, resume_authority))

    assert (
        process_runtime_file_downloads(
            plan,
            config=_config(
                policy="continue",
                attempts=1,
                default="aria2",
                resume=True,
            ),
            backends={"aria2": PreservingAriaBackend()},
            state_observer=observe,
            log=lambda _: None,
        )
        == ()
    )
    authority = observed[-1][1]
    assert authority is not None
    item = plan.items[0]
    digest = _state_digest(item, default_downloader="aria2")
    entry = _entry_for_item(item, status="pending", downloader="aria2")
    entry.resume = RuntimeResumeState.from_authority(authority)

    reconciled = reconcile_runtime_file_plan(
        plan,
        _state({digest: entry}),
        comfyui_path=root,
        default_downloader="aria2",
        resume_download=True,
    )

    assert reconciled.download_plan.items[0].resume_authority == authority


def test_runtime_missing_backend_is_structured_terminal_failure(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"))

    with pytest.raises(RuntimeFileDownloadError) as captured:
        process_runtime_file_downloads(
            plan,
            config=_config(),
            backends={},
            log=lambda _: None,
        )

    assert captured.value.diagnostics[0].code == "runtime_file.downloader_unavailable"


def test_download_runtime_files_constructs_aria2_only_when_required(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    httpx = FakeBackend()
    aria2 = FakeBackend()
    factory = FakeAria2Factory(aria2)

    httpx_results = download_runtime_files(
        _plan(root, _file("a.bin", downloader="httpx")),
        config=_config(),
        httpx_downloader=httpx,
        aria2_downloader_factory=factory,
        log=lambda _: None,
    )
    aria2_results = download_runtime_files(
        _plan(root, _file("b.bin", downloader="aria2")),
        config=_config(),
        httpx_downloader=httpx,
        aria2_downloader_factory=factory,
        log=lambda _: None,
    )

    assert httpx_results[0].backend == "httpx"
    assert aria2_results[0].backend == "aria2"
    assert len(factory.logs) == 1
    assert aria2.entered and aria2.exited


def test_startup_observer_runs_after_backend_prepare_before_transfer(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin", downloader="aria2"))
    events: list[str] = []

    class OrderedBackend(FakeBackend):
        def prepare(self, settings: DownloaderSettings) -> None:
            super().prepare(settings)
            events.append("prepare")

        def download(self, request, settings) -> TransportSuccess:
            events.append("download")
            return super().download(request, settings)

    backend = OrderedBackend()

    download_runtime_files(
        plan,
        config=_config(),
        aria2_downloader_factory=FakeAria2Factory(backend),
        startup_observer=lambda: events.append("startup"),
        log=lambda _: None,
    )

    assert events == ["prepare", "startup", "download"]
