"""Runtime download state-transition persistence."""

from __future__ import annotations

from datetime import UTC, datetime

from comfyui_docker_helper.container.runtime_diagnostics import (
    short_runtime_identity,
)
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeDownloadObservedStatus,
    RuntimeFilePlanItem,
    runtime_file_state_identity_digest,
)
from comfyui_docker_helper.container.runtime_state import (
    RuntimeDownloadEntry,
    RuntimeDownloadsState,
    RuntimeState,
    RuntimeStateError,
    RuntimeStateStore,
    failed_runtime_download_entry,
)
from comfyui_docker_helper.container.transfer_core import ResumeAuthority


class RuntimeDownloadStateWriter:
    """Own and atomically persist runtime download state transitions."""

    def __init__(
        self,
        store: RuntimeStateStore,
        state: RuntimeState,
        *,
        log: Logger | None = None,
    ) -> None:
        self._store = store
        self._state = state
        self._log = log

    def __call__(
        self,
        item: RuntimeFilePlanItem,
        status: RuntimeDownloadObservedStatus,
        *,
        error: object | None = None,
        resume_authority: ResumeAuthority | None = None,
    ) -> None:
        digest = runtime_file_state_identity_digest(item)
        try:
            entry = self._state.downloads.entries[digest]
        except KeyError as missing:
            raise RuntimeStateError(
                f"runtime download state entry is missing for {item.relative_target}"
            ) from missing

        now = datetime.now(UTC)
        if status == "downloading":
            updated_entry = RuntimeDownloadEntry.model_validate(
                {
                    **entry.model_dump(),
                    "status": "downloading",
                    "attempts": entry.attempts + 1,
                    "attempt_run_id": self._state.run_id,
                    "last_error": None,
                    "updated_at": now,
                }
            )
        elif status in ("failed", "exhausted"):
            updated_entry = failed_runtime_download_entry(
                entry,
                status=status,
                last_error=error,
                updated_at=now,
                resume_authority=resume_authority,
            )
        else:
            updated_entry = RuntimeDownloadEntry.model_validate(
                {
                    **entry.model_dump(),
                    "status": "completed",
                    "resume": None,
                    "last_error": None,
                    "updated_at": now,
                }
            )

        entries = dict(self._state.downloads.entries)
        entries[digest] = updated_entry
        self._state = RuntimeState(
            schema_version=self._state.schema_version,
            updated_at=now,
            run_id=self._state.run_id,
            downloads=RuntimeDownloadsState(entries=entries),
        )
        self._store.write(self._state)
        if self._log is not None:
            self._log(
                "Runtime download state persisted: "
                f"mode={item.download_mode} target={item.relative_target} "
                f"status={updated_entry.status} attempts={updated_entry.attempts} "
                f"identity={short_runtime_identity(digest)}"
            )
