"""Runtime file state admission, reconciliation, and exact cleanup."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Literal

from comfyui_docker_helper.config.validation.urls import DownloaderName
from comfyui_docker_helper.container.runtime.files.models import (
    RuntimeFileCleanupPending,
    RuntimeFilePlan,
    RuntimeFilePlanItem,
    RuntimeFileReconciliation,
    RuntimeFileReconciliationItem,
)
from comfyui_docker_helper.container.runtime.files.planning import (
    runtime_file_identity_digest,
    runtime_file_staging_target,
    runtime_file_state_identity_digest,
)
from comfyui_docker_helper.container.runtime.state import (
    RuntimeDownloadDigestKey,
    RuntimeDownloadEntry,
    RuntimeResumeState,
    RuntimeState,
    RuntimeStateError,
    runtime_download_desired_identity_digest,
)
from comfyui_docker_helper.container.transfer.core import (
    DownloadFilesError,
    FileTransferRequest,
    ResumeAuthority,
    StagingDisposition,
    TransferIdentity,
    _admit_preserved_transfer,
    admitted_regular_final,
    confirm_indexed_transfer_artifacts_absent,
    discard_preserved_transfer,
    project_transfer_identity,
)


def reconcile_runtime_file_plan(
    plan: RuntimeFilePlan,
    state: RuntimeState,
    *,
    comfyui_path: str | Path,
    default_downloader: DownloaderName,
    resume_download: bool,
) -> RuntimeFileReconciliation:
    """Reconcile desired files and execute exact state-indexed stale cleanup."""
    root = Path(comfyui_path)
    admitted_items = tuple(
        replace(item, downloader=item.downloader or default_downloader)
        for item in plan.items
    )
    current_digests = {
        runtime_file_state_identity_digest(item): item for item in admitted_items
    }
    stale_entry_digests = frozenset(
        digest for digest in state.downloads if digest not in current_digests
    )

    state_namespaces = _validate_runtime_state_entries(root, state)
    current_namespaces = {runtime_file_identity_digest(item) for item in admitted_items}

    items: list[RuntimeFileReconciliationItem] = []
    scheduled_items: list[RuntimeFilePlanItem] = []
    entries: dict[RuntimeDownloadDigestKey, RuntimeDownloadEntry] = {}
    cleanup_pending: list[RuntimeFileCleanupPending] = []

    for digest in sorted(stale_entry_digests):
        entry = state.downloads[digest]
        pending, reason = _reconcile_stale_runtime_entry(root, entry)
        if pending is not None:
            assert reason is not None
            if state_namespaces[digest] in current_namespaces:
                raise DownloadFilesError(
                    "current runtime transfer namespace has unresolved stale cleanup"
                )
            entries[digest] = pending
            cleanup_pending.append(
                RuntimeFileCleanupPending(digest=digest, reason=reason)
            )

    for item in admitted_items:
        digest = runtime_file_state_identity_digest(item)
        previous_entry = state.downloads.get(digest)
        final_exists = admitted_regular_final(root, item.target)

        status: Literal["pending", "completed"] = (
            "completed"
            if previous_entry is not None
            and previous_entry.status == "completed"
            and final_exists
            and item.checksum is None
            else "pending"
        )
        scheduled = status == "pending"

        resume_authority: ResumeAuthority | None = None
        if scheduled:
            resume_authority = _current_resume_authority(
                root,
                item,
                previous_entry,
                resume_download=resume_download,
            )
            scheduled_items.append(
                replace(
                    item,
                    resume_authority=resume_authority,
                )
            )

        entry = _runtime_download_entry_for_reconciliation(
            item,
            previous_entry,
            status=status,
            resume_authority=resume_authority,
        )
        entries[digest] = entry
        items.append(
            RuntimeFileReconciliationItem(
                item=item,
                digest=digest,
                status=status,
                scheduled=scheduled,
                staging_target=runtime_file_staging_target(item),
                previous_entry=previous_entry,
            )
        )

    reconciled_state = RuntimeState(
        schema_version=state.schema_version,
        run_id=state.run_id,
        downloads=entries,
    )
    return RuntimeFileReconciliation(
        state=reconciled_state,
        download_plan=RuntimeFilePlan(items=tuple(scheduled_items)),
        items=tuple(items),
        stale_entry_digests=stale_entry_digests,
        cleanup_pending=tuple(cleanup_pending),
    )


def _runtime_download_entry_for_reconciliation(
    item: RuntimeFilePlanItem,
    previous_entry: RuntimeDownloadEntry | None,
    *,
    status: Literal["pending", "completed"],
    resume_authority: ResumeAuthority | None,
) -> RuntimeDownloadEntry:
    return RuntimeDownloadEntry(
        source=item.url,
        target=item.relative_target,
        checksum=item.checksum,
        overwrite=item.overwrite,
        downloader=item.downloader,
        download_mode=item.download_mode,
        status=status,
        resume=(
            RuntimeResumeState.from_authority(resume_authority)
            if resume_authority is not None
            else None
        ),
    )


def _validate_runtime_state_entries(
    root: Path,
    state: RuntimeState,
) -> dict[str, str]:
    namespaces: dict[str, str] = {}
    namespace_owners: dict[str, str] = {}
    for digest, entry in state.downloads.items():
        expected = runtime_download_desired_identity_digest(
            source=entry.source,
            target=entry.target,
            checksum=entry.checksum,
            overwrite=entry.overwrite,
            downloader=entry.downloader,
        )
        if expected != digest:
            raise RuntimeStateError(
                "runtime state is invalid; remove the state file and restart"
            )
        transfer_digest = _entry_transfer_identity(root, entry).digest
        owner = namespace_owners.get(transfer_digest)
        if owner is not None and owner != digest:
            raise RuntimeStateError(
                "runtime state is invalid; remove the state file and restart"
            )
        namespace_owners[transfer_digest] = digest
        namespaces[digest] = transfer_digest
    return namespaces


def validate_runtime_file_state_plan(
    plan: RuntimeFilePlan,
    state: RuntimeState,
    *,
    comfyui_path: str | Path,
    default_downloader: DownloaderName,
    expected_run_id: str,
) -> None:
    """Re-admit an execution plan against the complete canonical state identity."""
    root = Path(comfyui_path)
    _validate_runtime_state_entries(root, state)
    if state.run_id != expected_run_id:
        raise RuntimeStateError("runtime download state belongs to another start")
    for item in plan.items:
        admitted = replace(item, downloader=item.downloader or default_downloader)
        digest = runtime_file_state_identity_digest(admitted)
        try:
            entry = state.downloads[digest]
        except KeyError as error:
            raise RuntimeStateError(
                f"runtime download state entry is missing for {item.relative_target}"
            ) from error
        expected_resume = _entry_resume_authority(root, entry)
        if (
            entry.source != admitted.url
            or entry.target != admitted.relative_target
            or entry.checksum != admitted.checksum
            or entry.overwrite != admitted.overwrite
            or entry.downloader != admitted.downloader
            or entry.download_mode != admitted.download_mode
            or expected_resume != admitted.resume_authority
            or entry.status != "pending"
        ):
            raise RuntimeStateError(
                f"runtime download state identity differs for {item.relative_target}"
            )


def _reconcile_stale_runtime_entry(
    root: Path,
    entry: RuntimeDownloadEntry,
) -> tuple[RuntimeDownloadEntry | None, str | None]:
    target = root.joinpath(*PurePosixPath(entry.target).parts)
    transfer_digest = _entry_transfer_identity(root, entry).digest
    authority = _entry_resume_authority(root, entry)
    try:
        absent = confirm_indexed_transfer_artifacts_absent(
            root=root,
            target=target,
            identity_digest=transfer_digest,
        )
    except (DownloadFilesError, OSError):
        return (
            _cleanup_pending_runtime_entry(entry, authority),
            "staging cleanup inspection failed",
        )
    if absent:
        return None, None
    if authority is not None:
        request = FileTransferRequest(
            root=root,
            url=entry.source,
            target=target,
            overwrite=entry.overwrite,
            expected_checksum=entry.checksum,
            staging_disposition=StagingDisposition.PRESERVE,
            resume_authority=authority,
        )
        try:
            discard_preserved_transfer(request)
        except (DownloadFilesError, OSError):
            return (
                _cleanup_pending_runtime_entry(entry, authority),
                "staging cleanup failed",
            )
        return None, None

    return (
        _cleanup_pending_runtime_entry(entry, None),
        "interrupted transfer lacks exact artifact authority",
    )


def _cleanup_pending_runtime_entry(
    entry: RuntimeDownloadEntry,
    authority: ResumeAuthority | None,
) -> RuntimeDownloadEntry:
    resume = (
        RuntimeResumeState.from_authority(authority) if authority is not None else None
    )
    return RuntimeDownloadEntry.model_validate(
        {**entry.model_dump(), "status": "cleanup_pending", "resume": resume}
    )


def _current_resume_authority(
    root: Path,
    item: RuntimeFilePlanItem,
    entry: RuntimeDownloadEntry | None,
    *,
    resume_download: bool,
) -> ResumeAuthority | None:
    transfer_digest = runtime_file_identity_digest(item)
    authority = _entry_resume_authority(root, entry)
    request: FileTransferRequest | None = None
    artifact_admission: Literal["absent", "partial", "complete"] | None = None
    if authority is not None:
        request = FileTransferRequest(
            root=root,
            url=item.url,
            target=item.target,
            overwrite=item.overwrite,
            expected_checksum=item.checksum,
            staging_disposition=StagingDisposition.PRESERVE,
            resume_authority=authority,
        )
        try:
            artifact_admission = _admit_preserved_transfer(request)
        except (DownloadFilesError, OSError) as error:
            raise DownloadFilesError(
                "current runtime resume artifacts failed exact admission"
            ) from error
    may_resume = (
        authority is not None
        and artifact_admission == "complete"
        and item.downloader == "aria2"
        and resume_download
        and entry is not None
        and entry.status != "cleanup_pending"
    )
    if may_resume:
        return authority

    target = item.target
    try:
        absent = confirm_indexed_transfer_artifacts_absent(
            root=root,
            target=target,
            identity_digest=transfer_digest,
        )
    except (DownloadFilesError, OSError) as error:
        raise DownloadFilesError(
            "current runtime transfer cleanup could not be established"
        ) from error
    if absent:
        return None
    if authority is None:
        raise DownloadFilesError(
            "current runtime transfer namespace lacks exact cleanup authority"
        )
    assert request is not None
    try:
        discard_preserved_transfer(request)
    except (DownloadFilesError, OSError) as error:
        raise DownloadFilesError("current runtime transfer cleanup failed") from error
    return None


def _entry_transfer_identity(
    root: Path,
    entry: RuntimeDownloadEntry,
) -> TransferIdentity:
    target = root.joinpath(*PurePosixPath(entry.target).parts)
    return project_transfer_identity(
        root=root,
        url=entry.source,
        target=target,
        expected_checksum=entry.checksum,
    )


def _entry_resume_authority(
    root: Path,
    entry: RuntimeDownloadEntry | None,
) -> ResumeAuthority | None:
    if entry is None or entry.resume is None:
        return None
    return entry.resume.as_authority(_entry_transfer_identity(root, entry).digest)
