"""Runtime download state-transition persistence."""

from __future__ import annotations

from comfyui_docker_helper.container.runtime_files import (
    RuntimeDownloadObservedStatus,
    RuntimeFilePlanItem,
    runtime_file_state_identity_digest,
)
from comfyui_docker_helper.container.runtime_state import (
    RuntimeDownloadEntry,
    RuntimeResumeState,
    RuntimeState,
    RuntimeStateError,
    RuntimeStateStore,
)
from comfyui_docker_helper.container.transfer_core import ResumeAuthority


class RuntimeDownloadStateWriter:
    """Own and atomically persist runtime download state transitions."""

    def __init__(
        self,
        store: RuntimeStateStore,
        state: RuntimeState,
    ) -> None:
        self._store = store
        self._state = state

    def __call__(
        self,
        item: RuntimeFilePlanItem,
        status: RuntimeDownloadObservedStatus,
        *,
        error: object | None = None,
        resume_authority: ResumeAuthority | None = None,
    ) -> None:
        del error
        digest = runtime_file_state_identity_digest(item)
        try:
            entry = self._state.downloads[digest]
        except KeyError as missing:
            raise RuntimeStateError(
                f"runtime download state entry is missing for {item.relative_target}"
            ) from missing

        if status in ("failed", "exhausted"):
            resume = (
                RuntimeResumeState.from_authority(resume_authority)
                if resume_authority is not None
                else None
            )
            updated_entry = RuntimeDownloadEntry.model_validate(
                {**entry.model_dump(), "status": "pending", "resume": resume}
            )
        else:
            updated_entry = RuntimeDownloadEntry.model_validate(
                {**entry.model_dump(), "status": "completed", "resume": None}
            )

        if updated_entry == entry:
            return

        entries = dict(self._state.downloads)
        entries[digest] = updated_entry
        self._state = RuntimeState(
            schema_version=self._state.schema_version,
            run_id=self._state.run_id,
            downloads=entries,
        )
        self._store.write(self._state)
