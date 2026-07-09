"""Tests for internal runtime file download planning."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

import httpx
import pytest

from comfyui_docker_helper.config.runtime_projection import RuntimeConfig
from comfyui_docker_helper.container.download_files import (
    DownloadCancelled,
    DownloaderSettings,
    DownloadFilesError,
    FileDownloadItem,
    HttpxDownloader,
    Logger,
    TransferDownloadFilesError,
)
from comfyui_docker_helper.container.runtime_files import (
    RuntimeFileDownloadError,
    RuntimeFilePlan,
    RuntimeFilePlanError,
    RuntimeFilePlanItem,
    build_runtime_file_plan,
    canonical_runtime_file_identity_bytes,
    download_runtime_files,
    merge_runtime_file_items,
    process_runtime_file_downloads,
    reconcile_runtime_file_plan,
    runtime_file_identity_digest,
    runtime_file_staging_target,
)
from comfyui_docker_helper.container.runtime_state import (
    RuntimeDownloadEntry,
    RuntimeDownloadsState,
    RuntimeState,
    write_runtime_state,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
LATER = datetime(2026, 1, 2, 4, 5, 6, tzinfo=UTC)
STALE_CLEANUP_NOW = 2_000_000.0


def _staging_target(item: RuntimeFilePlanItem) -> Path:
    return runtime_file_staging_target(item, runtime_file_identity_digest(item))


def _touch_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime), follow_symlinks=False)


def _identities(error: RuntimeFilePlanError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


class FakeDownloadBackend:
    def __init__(self, payload: bytes = b"downloaded") -> None:
        self.payload = payload
        self.calls: list[tuple[FileDownloadItem, DownloaderSettings]] = []

    def download(
        self,
        item: FileDownloadItem,
        settings: DownloaderSettings,
    ) -> None:
        self.calls.append((item, settings))
        item.target.write_bytes(self.payload)


class FakeManagedDownloadBackend(FakeDownloadBackend):
    def __init__(self, payload: bytes = b"downloaded") -> None:
        super().__init__(payload=payload)
        self.entered = False
        self.exited = False
        self.prepare_calls: list[DownloaderSettings] = []

    def prepare(self, settings: DownloaderSettings) -> None:
        self.prepare_calls.append(settings)

    def __enter__(self) -> FakeManagedDownloadBackend:
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


class FakeCancellableDownloadBackend(FakeDownloadBackend):
    def __init__(self, payload: bytes = b"downloaded") -> None:
        super().__init__(payload=payload)
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeCancellableManagedDownloadBackend(FakeManagedDownloadBackend):
    def __init__(self, payload: bytes = b"downloaded") -> None:
        super().__init__(payload=payload)
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeAria2Factory:
    def __init__(self, backend: FakeManagedDownloadBackend) -> None:
        self.backend = backend
        self.calls: list[Logger] = []

    def __call__(self, *, log: Logger) -> FakeManagedDownloadBackend:
        self.calls.append(log)
        return self.backend


def _runtime_state(
    *,
    entries: dict[str, RuntimeDownloadEntry] | None = None,
    run_id: str = "run-1",
    updated_at: datetime = NOW,
) -> RuntimeState:
    return RuntimeState(
        schema_version=1,
        updated_at=updated_at,
        run_id=run_id,
        downloads=RuntimeDownloadsState(entries=entries or {}),
    )


def _runtime_entry(
    *,
    target: str = "models/checkpoints/a.bin",
    status: str = "pending",
    attempts: int = 0,
    attempt_run_id: str = "run-1",
    download_mode: str = "sync",
    updated_at: datetime = NOW,
    last_error: str | None = None,
) -> RuntimeDownloadEntry:
    return RuntimeDownloadEntry(
        target=target,
        download_mode=download_mode,
        status=status,
        attempts=attempts,
        attempt_run_id=attempt_run_id,
        last_error=last_error,
        updated_at=updated_at,
    )


# Staging tests protect atomic replacement, skip/overwrite decisions, and cleanup
# so interrupted downloads cannot expose partial or unrelated files as complete.
def test_runtime_file_plan_derives_targets_keys_and_first_seen_order(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()

    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                    },
                    {
                        "url": "https://example.com/b.bin",
                        "dir": "models/loras",
                        "filename": "b.bin",
                        "download_mode": "sync",
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    assert [item.relative_target for item in plan.items] == [
        "models/checkpoints/a.bin",
        "models/loras/b.bin",
    ]
    assert [item.target for item in plan.items] == [
        comfyui / "models" / "checkpoints" / "a.bin",
        comfyui / "models" / "loras" / "b.bin",
    ]
    assert [item.download_mode for item in plan.items] == ["sync", "sync"]
    assert [item.action for item in plan.items] == ["download", "download"]


def test_runtime_file_identity_uses_exact_canonical_bytes_and_digest_vectors(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    item = plan.items[0]

    assert canonical_runtime_file_identity_bytes(item) == (
        b'{"schema_version":1,"source":"https://example.com/a.bin",'
        b'"source_type":"url","target":"models/checkpoints/a.bin"}'
    )
    assert runtime_file_identity_digest(item) == (
        "sha256:37b76480b800c01b144b9e94323a269e43922d3caeb00bb2f3fd15dd62ed1960"
    )
    assert (
        runtime_file_identity_digest(
            replace(item, url="https://EXAMPLE.com/a.bin?b=2&a=1#frag")
        )
        == "sha256:2c623e23733041c71ed64736fecf65a3cec7008afb8e04093fecd6b346646395"
    )
    assert (
        runtime_file_identity_digest(replace(item, url="https://example.com/a2.bin"))
        == "sha256:59feb9dc7065c3de93aa5e3dc5093b4ad90117542f99def28f5a7a10198bf9fc"
    )
    assert (
        runtime_file_identity_digest(
            replace(
                item,
                directory="models/loras",
                relative_target="models/loras/a.bin",
                target=comfyui / "models" / "loras" / "a.bin",
            )
        )
        == "sha256:194706c20260303bf7bf1c9b47409cd1a45aef3522c008ee1780c64c1b5f5c4f"
    )


def test_runtime_file_identity_ignores_non_identity_execution_fields(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    item = build_runtime_file_plan(
        [
            {
                "cdh": {
                    "download_max_attempts": 9,
                    "download_failure_policy": "continue",
                },
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                        "overwrite": False,
                        "downloader": "httpx",
                    }
                ],
            }
        ],
        comfyui_path=comfyui,
    ).items[0]

    assert {
        runtime_file_identity_digest(
            replace(
                item,
                downloader=downloader,
                download_mode=download_mode,
                overwrite=overwrite,
                action=action,
            )
        )
        for downloader in (None, "httpx", "aria2")
        for download_mode in ("sync", "async")
        for overwrite in (False, True)
        for action in ("download", "skip_existing", "overwrite_existing")
    } == {runtime_file_identity_digest(item)}


def test_reconcile_schedules_missing_non_overwrite_file_and_writes_state(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    reconciliation = reconcile_runtime_file_plan(
        plan,
        _runtime_state(),
        now=LATER,
        comfyui_path=comfyui,
    )
    state_path = tmp_path / "state.json"
    write_runtime_state(state_path, reconciliation.state)

    digest = runtime_file_identity_digest(plan.items[0])
    assert [item.digest for item in reconciliation.items] == [digest]
    assert [item.status for item in reconciliation.items] == ["pending"]
    assert [item.scheduled for item in reconciliation.items] == [True]
    assert reconciliation.download_plan.items == plan.items
    assert reconciliation.items[0].staging_target == runtime_file_staging_target(
        plan.items[0],
        digest,
    )
    assert reconciliation.state.downloads.entries[digest] == _runtime_entry(
        status="pending",
        updated_at=LATER,
    )
    assert state_path.exists()


def test_reconcile_runtime_file_plan_skips_existing_non_overwrite_file(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    target_parent = comfyui / "models" / "checkpoints"
    target_parent.mkdir(parents=True)
    (target_parent / "a.bin").write_bytes(b"already-there")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    reconciliation = reconcile_runtime_file_plan(
        plan,
        _runtime_state(),
        now=LATER,
        comfyui_path=comfyui,
    )
    digest = runtime_file_identity_digest(plan.items[0])

    assert reconciliation.download_plan.items == ()
    assert reconciliation.items[0].status == "skipped"
    assert reconciliation.items[0].scheduled is False
    assert reconciliation.state.downloads.entries[digest].status == "skipped"


def test_reconcile_scheduled_removed_non_overwrite_file_downloads(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    target_parent = comfyui / "models" / "checkpoints"
    target_parent.mkdir(parents=True)
    final_target = target_parent / "a.bin"
    final_target.write_bytes(b"removed-after-plan")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    assert plan.items[0].action == "skip_existing"
    final_target.unlink()

    reconciliation = reconcile_runtime_file_plan(
        plan,
        _runtime_state(),
        now=LATER,
        comfyui_path=comfyui,
    )

    assert reconciliation.items[0].status == "pending"
    assert reconciliation.items[0].scheduled is True
    assert reconciliation.download_plan.items[0].action == "download"

    backend = FakeDownloadBackend(payload=b"downloaded-after-removal")
    process_runtime_file_downloads(
        reconciliation.download_plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        backends={"httpx": backend},
        log=lambda message: None,
    )

    assert len(backend.calls) == 1
    assert final_target.read_bytes() == b"downloaded-after-removal"


def test_reconcile_completes_overwrite_when_current_completed_and_final_exists(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    target_parent = comfyui / "models" / "checkpoints"
    target_parent.mkdir(parents=True)
    (target_parent / "a.bin").write_bytes(b"complete")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                        "overwrite": True,
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    digest = runtime_file_identity_digest(plan.items[0])
    previous_entry = _runtime_entry(
        status="completed",
        attempts=3,
        attempt_run_id="previous-run",
        last_error="ignored",
    )

    reconciliation = reconcile_runtime_file_plan(
        plan,
        _runtime_state(entries={digest: previous_entry}),
        now=LATER,
        comfyui_path=comfyui,
    )

    assert reconciliation.download_plan.items == ()
    assert reconciliation.items[0].previous_entry == previous_entry
    assert reconciliation.items[0].status == "completed"
    assert reconciliation.state.downloads.entries[digest] == _runtime_entry(
        status="completed",
        attempts=3,
        attempt_run_id="previous-run",
        updated_at=LATER,
    )


@pytest.mark.parametrize("previous_status", [None, "pending", "failed", "completed"])
def test_reconcile_runtime_file_plan_schedules_overwrite_unless_completed_with_final(
    tmp_path: Path,
    previous_status: str | None,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                        "overwrite": True,
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    digest = runtime_file_identity_digest(plan.items[0])
    entries = (
        {}
        if previous_status is None
        else {
            digest: _runtime_entry(
                status=previous_status,
                attempts=2,
                attempt_run_id="same-digest-run",
                last_error="old error",
            )
        }
    )

    reconciliation = reconcile_runtime_file_plan(
        plan,
        _runtime_state(entries=entries),
        now=LATER,
        comfyui_path=comfyui,
    )

    assert reconciliation.download_plan.items[0] == replace(
        plan.items[0],
        action="overwrite_existing",
    )
    assert reconciliation.items[0].status == "pending"
    entry = reconciliation.state.downloads.entries[digest]
    assert entry.status == "pending"
    assert entry.last_error is None
    assert entry.attempts == (0 if previous_status is None else 2)
    assert entry.attempt_run_id == (
        "run-1" if previous_status is None else "same-digest-run"
    )


def test_reconcile_runtime_file_plan_reports_stale_entries_and_staging_without_deleting(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    stale_digest = "sha256:" + ("a" * 64)
    stale_staging = (
        comfyui
        / "models"
        / "old"
        / ".cdh-staging"
        / f"cdh-{stale_digest.removeprefix('sha256:')}.part"
    )
    stale_staging.parent.mkdir(parents=True)
    stale_staging.write_bytes(b"partial")
    (comfyui / "models" / "checkpoints").mkdir(parents=True)
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    reconciliation = reconcile_runtime_file_plan(
        plan,
        _runtime_state(
            entries={
                stale_digest: _runtime_entry(
                    target="models/old/removed.bin",
                    status="downloading",
                )
            }
        ),
        now=LATER,
        comfyui_path=comfyui,
    )

    assert reconciliation.stale_entry_digests == frozenset({stale_digest})
    assert reconciliation.stale_staging_candidates == (stale_staging,)
    assert stale_staging.read_bytes() == b"partial"
    assert stale_digest not in reconciliation.state.downloads.entries


def test_reconcile_empty_plan_reports_absolute_stale_candidate_without_deleting(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    stale_digest = "sha256:" + ("b" * 64)
    stale_staging = (
        comfyui
        / "models"
        / "reset"
        / ".cdh-staging"
        / f"cdh-{stale_digest.removeprefix('sha256:')}.part"
    )
    stale_staging.parent.mkdir(parents=True)
    stale_staging.write_bytes(b"old-partial")

    reconciliation = reconcile_runtime_file_plan(
        RuntimeFilePlan(items=()),
        _runtime_state(
            entries={
                stale_digest: _runtime_entry(
                    target="models/reset/removed.bin",
                    status="pending",
                )
            }
        ),
        now=LATER,
        comfyui_path=comfyui,
    )

    assert reconciliation.stale_entry_digests == frozenset({stale_digest})
    assert reconciliation.download_plan.items == ()
    assert reconciliation.items == ()
    assert reconciliation.state.downloads.entries == {}
    assert reconciliation.stale_staging_candidates == (stale_staging,)
    assert reconciliation.stale_staging_candidates[0].is_absolute()
    assert stale_staging.read_bytes() == b"old-partial"


def test_reconcile_removed_config_entry_reports_absolute_stale_candidate(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    stale_digest = "sha256:" + ("c" * 64)
    stale_staging = (
        comfyui
        / "models"
        / "old"
        / ".cdh-staging"
        / f"cdh-{stale_digest.removeprefix('sha256:')}.part"
    )
    stale_staging.parent.mkdir(parents=True)
    stale_staging.write_bytes(b"old-partial")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/current.bin",
                        "dir": "models/current",
                        "filename": "current.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    reconciliation = reconcile_runtime_file_plan(
        plan,
        _runtime_state(
            entries={
                stale_digest: _runtime_entry(
                    target="models/old/removed.bin",
                    status="downloading",
                )
            }
        ),
        now=LATER,
        comfyui_path=comfyui,
    )

    assert reconciliation.stale_entry_digests == frozenset({stale_digest})
    assert reconciliation.stale_staging_candidates == (stale_staging,)
    assert reconciliation.stale_staging_candidates[0].is_absolute()
    assert stale_staging.read_bytes() == b"old-partial"
    assert stale_digest not in reconciliation.state.downloads.entries


# Runtime file async mode and merge behavior stays paired with runtime_config:
# config validates user-authored files.N, this module preserves execution intent.
def test_reconcile_runtime_file_plan_accepts_internal_async_items(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    item = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    ).items[0]
    async_plan = replace(item, download_mode="async")

    reconciliation = reconcile_runtime_file_plan(
        replace(build_runtime_file_plan([], comfyui_path=comfyui), items=(async_plan,)),
        _runtime_state(),
        now=LATER,
        comfyui_path=comfyui,
    )

    digest = runtime_file_identity_digest(async_plan)
    assert reconciliation.download_plan.items == (async_plan,)
    assert reconciliation.state.downloads.entries[digest].download_mode == "async"


def test_build_runtime_file_plan_accepts_public_async_download_mode(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()

    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                        "download_mode": "async",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    assert plan.items[0].download_mode == "async"


def test_build_runtime_file_plan_applies_default_mode_with_per_file_precedence(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()

    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/default.bin",
                        "dir": "models",
                        "filename": "default.bin",
                    },
                    {
                        "url": "https://example.com/sync.bin",
                        "dir": "models",
                        "filename": "sync.bin",
                        "download_mode": "sync",
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
        default_download_mode="async",
    )

    assert [item.download_mode for item in plan.items] == ["async", "sync"]


def test_runtime_file_same_key_merge_and_reset_behavior() -> None:
    merged = merge_runtime_file_items(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": False,
                    },
                    {
                        "url": "https://example.com/b.bin",
                        "dir": "models",
                        "filename": "b.bin",
                    },
                ]
            },
            {
                "files": [
                    {
                        "url": "https://example.com/a2.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": True,
                    },
                    {
                        "url": "https://example.com/c.bin",
                        "dir": "models",
                        "filename": "c.bin",
                    },
                ]
            },
        ]
    )

    assert [
        (item["filename"], item["url"], item.get("overwrite")) for item in merged
    ] == [
        ("a.bin", "https://example.com/a2.bin", True),
        ("b.bin", "https://example.com/b.bin", None),
        ("c.bin", "https://example.com/c.bin", None),
    ]

    assert merge_runtime_file_items(
        [
            {"files": list(merged)},
            {"files": []},
            {
                "files": [
                    {
                        "url": "https://example.com/d.bin",
                        "dir": "models",
                        "filename": "d.bin",
                    }
                ]
            },
        ]
    ) == (
        {
            "url": "https://example.com/d.bin",
            "dir": "models",
            "filename": "d.bin",
        },
    )


def test_runtime_file_merge_accepts_full_runtime_layer_documents(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()

    plan = build_runtime_file_plan(
        [
            {
                "comfyui": {"listen": "127.0.0.1", "port": 8190},
                "cdh": {"default_downloader": "httpx"},
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ],
            },
            {
                "comfyui": {"extra_args": ["--cpu"]},
                "cdh": {"default_download_mode": "sync"},
                "files": [
                    {
                        "url": "https://example.com/a2.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": True,
                    },
                    {
                        "url": "https://example.com/b.bin",
                        "dir": "models",
                        "filename": "b.bin",
                    },
                ],
            },
        ],
        comfyui_path=comfyui,
    )

    assert [
        (item.relative_target, item.url, item.overwrite) for item in plan.items
    ] == [
        ("models/a.bin", "https://example.com/a2.bin", True),
        ("models/b.bin", "https://example.com/b.bin", False),
    ]


def test_runtime_file_merge_ignores_full_runtime_keys_but_validates_files() -> None:
    with pytest.raises(RuntimeFilePlanError) as error:
        merge_runtime_file_items(
            [
                {
                    "comfyui": {"listen": "127.0.0.1"},
                    "cdh": {"default_downloader": "httpx"},
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": "a.bin",
                            "download_mode": "parallel",
                        }
                    ],
                }
            ]
        )

    assert _identities(error.value) == [
        (("files", 0, "download_mode"), "schema.literal_error")
    ]


# Authored-index diagnostics must report the file position from the source layer
# that contributed the surviving item, not the merged output position.
def test_runtime_file_plan_invalid_mounted_after_baked_reports_authored_index(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()

    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/baked.bin",
                            "dir": "models",
                            "filename": "baked.bin",
                        }
                    ]
                },
                {
                    "files": [
                        {
                            "url": "ftp://example.com/mounted.bin",
                            "dir": "models",
                            "filename": "mounted.bin",
                        }
                    ]
                },
            ],
            comfyui_path=comfyui,
        )

    assert _identities(error.value) == [
        (("files", 0, "url"), "runtime_file.invalid_url")
    ]


def test_runtime_file_plan_multiple_invalid_items_keep_authored_indexes(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()

    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "/models",
                            "filename": "a.bin",
                        },
                        {
                            "url": "https://example.com/b.bin",
                            "dir": "models",
                            "filename": "nested/b.bin",
                        },
                    ]
                }
            ],
            comfyui_path=comfyui,
        )

    assert _identities(error.value) == [
        (("files", 0, "dir"), "runtime_file.absolute_directory"),
        (("files", 1, "filename"), "runtime_file.invalid_filename"),
    ]


def test_runtime_file_plan_override_target_error_uses_override_source_index(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    target = comfyui / "models" / "overridden.bin"
    target.mkdir(parents=True)

    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/first.bin",
                            "dir": "models",
                            "filename": "first.bin",
                        },
                        {
                            "url": "https://example.com/baked.bin",
                            "dir": "models",
                            "filename": "overridden.bin",
                        },
                    ]
                },
                {
                    "files": [
                        {
                            "url": "https://example.com/mounted.bin",
                            "dir": "models",
                            "filename": "overridden.bin",
                        }
                    ]
                },
            ],
            comfyui_path=comfyui,
        )

    assert _identities(error.value) == [
        (("files", 0, "target"), "runtime_file.non_regular_target")
    ]


def test_runtime_file_download_selects_explicit_backend_before_default(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/httpx.bin",
                        "dir": "models",
                        "filename": "httpx.bin",
                        "downloader": "httpx",
                    },
                    {
                        "url": "https://example.com/default.bin",
                        "dir": "models",
                        "filename": "default.bin",
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    config = RuntimeConfig.model_validate({"cdh": {"default_downloader": "aria2"}})
    httpx_backend = FakeDownloadBackend()
    aria2_backend = FakeDownloadBackend()

    results = process_runtime_file_downloads(
        plan,
        config=config,
        backends={"httpx": httpx_backend, "aria2": aria2_backend},
        log=lambda message: None,
    )

    assert [result.backend for result in results] == ["httpx", "aria2"]
    assert [call[0].url for call in httpx_backend.calls] == [
        "https://example.com/httpx.bin"
    ]
    assert [call[0].url for call in aria2_backend.calls] == [
        "https://example.com/default.bin"
    ]
    assert httpx_backend.calls[0][0].target == _staging_target(plan.items[0])
    assert aria2_backend.calls[0][0].target == _staging_target(plan.items[1])
    assert httpx_backend.calls[0][0].target != results[0].item.target
    assert aria2_backend.calls[0][0].target != results[1].item.target


def test_runtime_file_cancel_before_first_attempt_leaves_backend_and_state_untouched(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FakeCancellableDownloadBackend()
    observed_states: list[str] = []
    observed_backends: list[object] = []

    results = process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        backends={"httpx": backend},
        log=lambda message: None,
        state_observer=lambda item, status, error=None: observed_states.append(status),
        cancel_requested=lambda: True,
        backend_observer=observed_backends.append,
    )

    assert results == ()
    assert backend.calls == []
    assert observed_states == []
    assert observed_backends == []
    assert not (comfyui / "models" / "a.bin").exists()


def test_runtime_file_cancel_after_downloading_begins_does_not_exhaust(
    tmp_path: Path,
) -> None:
    class CancellingBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.write_bytes(b"partial")
            raise DownloadCancelled("cancelled in test")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = CancellingBackend()
    observed_states: list[str] = []

    results = process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        backends={"httpx": backend},
        log=lambda message: None,
        state_observer=lambda item, status, error=None: observed_states.append(status),
    )

    assert results == ()
    assert observed_states == ["downloading"]
    assert not (comfyui / "models" / "a.bin").exists()
    assert not _staging_target(plan.items[0]).exists()


def test_runtime_file_httpx_style_cancel_error_does_not_fail_or_exhaust(
    tmp_path: Path,
) -> None:
    class HttpxInterruptedBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            nonlocal cancel_requested
            self.calls.append((item, settings))
            item.target.write_bytes(b"partial")
            cancel_requested = True
            raise TransferDownloadFilesError("transport closed during cancellation")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    cancel_requested = False
    backend = HttpxInterruptedBackend()
    observed_states: list[str] = []

    results = process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate(
            {
                "cdh": {
                    "default_downloader": "httpx",
                    "download_max_attempts": 2,
                }
            }
        ),
        backends={"httpx": backend},
        log=lambda message: None,
        state_observer=lambda item, status, error=None: observed_states.append(status),
        cancel_requested=lambda: cancel_requested,
    )

    assert results == ()
    assert observed_states == ["downloading"]
    assert [call[0].filename for call in backend.calls] == ["a.bin"]
    assert not (comfyui / "models" / "a.bin").exists()
    assert not _staging_target(plan.items[0]).exists()


def test_runtime_file_aria2_style_cancel_error_does_not_exhaust(
    tmp_path: Path,
) -> None:
    class Aria2InterruptedBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            nonlocal cancel_requested
            self.calls.append((item, settings))
            item.target.write_bytes(b"partial")
            cancel_requested = True
            raise DownloadFilesError("aria2 RPC disconnected during cancellation")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "downloader": "aria2",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    cancel_requested = False
    backend = Aria2InterruptedBackend()
    observed_states: list[str] = []

    results = process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate(
            {
                "cdh": {
                    "download_max_attempts": 2,
                    "download_failure_policy": "fail",
                }
            }
        ),
        backends={"aria2": backend},
        log=lambda message: None,
        state_observer=lambda item, status, error=None: observed_states.append(status),
        cancel_requested=lambda: cancel_requested,
    )

    assert results == ()
    assert observed_states == ["downloading"]
    assert [call[0].filename for call in backend.calls] == ["a.bin"]
    assert not (comfyui / "models" / "a.bin").exists()
    assert not _staging_target(plan.items[0]).exists()


def test_runtime_file_cancel_after_transfer_before_final_placement_cleans_staging(
    tmp_path: Path,
) -> None:
    class CancelAfterTransferBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            nonlocal cancel_after_transfer
            super().download(item, settings)
            cancel_after_transfer = True

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    cancel_after_transfer = False
    backend = CancelAfterTransferBackend(payload=b"downloaded")
    observed_states: list[str] = []

    def state_observer(
        item: RuntimeFilePlanItem,
        status: str,
        *,
        error: object | None = None,
    ) -> None:
        del item, error
        observed_states.append(status)

    results = process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        backends={"httpx": backend},
        log=lambda message: None,
        state_observer=state_observer,
        cancel_requested=lambda: cancel_after_transfer,
    )

    assert results == ()
    assert observed_states == ["downloading"]
    assert not (comfyui / "models" / "a.bin").exists()
    assert not _staging_target(plan.items[0]).exists()


def test_download_runtime_files_observes_cancellable_httpx_backend(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "downloader": "httpx",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FakeCancellableDownloadBackend()
    observed_backends: list[object] = []

    download_runtime_files(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        httpx_downloader=backend,
        log=lambda message: None,
        backend_observer=observed_backends.append,
    )

    assert observed_backends == [backend]


def test_download_runtime_files_observes_cancellable_aria2_backend(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "downloader": "aria2",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    httpx_backend = FakeCancellableDownloadBackend()
    aria2_backend = FakeCancellableManagedDownloadBackend()
    factory = FakeAria2Factory(aria2_backend)
    observed_backends: list[object] = []

    download_runtime_files(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        httpx_downloader=httpx_backend,
        aria2_downloader_factory=factory,
        log=lambda message: None,
        backend_observer=observed_backends.append,
    )

    assert observed_backends == [httpx_backend, aria2_backend]


def test_runtime_file_download_uses_runtime_downloader_settings(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "downloader": "aria2",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    config = RuntimeConfig.model_validate(
        {
            "cdh": {
                "default_downloader": "httpx",
                "downloader": {
                    "aria2": {
                        "rpc_port": 6811,
                        "split": 4,
                        "max_connection_per_server": 5,
                        "min_split_size": "2M",
                        "resume_download": False,
                    },
                    "httpx": {"timeout": 12.5, "retries": 1},
                },
            }
        }
    )
    backend = FakeDownloadBackend()

    process_runtime_file_downloads(
        plan,
        config=config,
        backends={"aria2": backend},
        log=lambda message: None,
    )

    settings = backend.calls[0][1]
    assert settings.default == "httpx"
    assert settings.aria2.rpc_port == 6811
    assert settings.aria2.split == 4
    assert settings.aria2.max_connection_per_server == 5
    assert settings.aria2.min_split_size == "2M"
    assert settings.aria2.resume_download is False
    assert settings.httpx.timeout == 12.5
    assert settings.httpx.retries == 0


def test_runtime_file_download_retries_then_succeeds(tmp_path: Path) -> None:
    class FlakyBackend(FakeDownloadBackend):
        def __init__(self) -> None:
            super().__init__(payload=b"eventual")
            self.second_attempt_staging_bytes: bytes | None = None

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            if len(self.calls) == 1:
                item.target.write_bytes(b"partial")
                raise TransferDownloadFilesError("temporary transfer failure")
            self.second_attempt_staging_bytes = (
                item.target.read_bytes() if item.target.exists() else None
            )
            item.target.write_bytes(self.payload)

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FlakyBackend()
    messages: list[str] = []

    results = process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate(
            {
                "cdh": {
                    "default_downloader": "httpx",
                    "download_max_attempts": 2,
                    "download_failure_policy": "fail",
                    "downloader": {"httpx": {"retries": 5}},
                }
            }
        ),
        backends={"httpx": backend},
        log=messages.append,
    )

    assert len(backend.calls) == 2
    assert backend.second_attempt_staging_bytes is None
    assert {call[0].overwrite for call in backend.calls} == {False}
    assert {call[1].httpx.retries for call in backend.calls} == {0}
    assert (comfyui / "models" / "a.bin").read_bytes() == b"eventual"
    assert results[0].status.value == "downloaded"
    assert any(
        "Retrying runtime file download after attempt 1/2" in message
        for message in messages
    )
    assert any(
        "Runtime download attempt: mode=sync target=models/a.bin "
        "backend=httpx attempt=1/2 status=downloading "
        "source_host=example.com identity=sha256:" in message
        for message in messages
    )
    assert any(
        "Runtime download attempt failed: mode=sync target=models/a.bin "
        "backend=httpx attempt=1/2 status=failed" in message
        for message in messages
    )
    assert any(
        "Runtime download completed: mode=sync target=models/a.bin "
        "backend=httpx attempts=2 status=completed" in message
        for message in messages
    )


@pytest.mark.parametrize(
    ("resume_download", "expected_overwrite"),
    [(True, False), (False, True)],
)
def test_runtime_file_aria2_staging_overwrite_tracks_resume_setting(
    tmp_path: Path,
    resume_download: bool,
    expected_overwrite: bool,
) -> None:
    class FlakyAria2Backend(FakeDownloadBackend):
        def __init__(self) -> None:
            super().__init__()
            self.first_attempt_staging_exists: bool | None = None
            self.first_attempt_control_exists: bool | None = None
            self.second_attempt_staging_exists: bool | None = None
            self.second_attempt_control_exists: bool | None = None
            self.second_attempt_staging_bytes: bytes | None = None

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            if len(self.calls) == 1:
                self.first_attempt_staging_exists = item.target.exists()
                self.first_attempt_control_exists = Path(
                    f"{item.target}.aria2"
                ).exists()
                item.target.write_bytes(b"partial")
                Path(f"{item.target}.aria2").write_bytes(b"control")
                raise TransferDownloadFilesError("temporary transfer failure")
            control_path = Path(f"{item.target}.aria2")
            self.second_attempt_staging_exists = item.target.exists()
            self.second_attempt_control_exists = control_path.exists()
            self.second_attempt_staging_bytes = (
                item.target.read_bytes() if item.target.exists() else None
            )
            item.target.write_bytes(self.payload)

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "downloader": "aria2",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FlakyAria2Backend()
    staging_target = _staging_target(plan.items[0])
    staging_target.parent.mkdir(parents=True)
    staging_target.write_bytes(b"restart-partial")
    Path(f"{staging_target}.aria2").write_bytes(b"restart-control")

    process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate(
            {
                "cdh": {
                    "download_max_attempts": 2,
                    "downloader": {
                        "aria2": {"resume_download": resume_download},
                    },
                }
            }
        ),
        backends={"aria2": backend},
        log=lambda message: None,
    )

    assert [call[0].overwrite for call in backend.calls] == [
        expected_overwrite,
        expected_overwrite,
    ]
    assert [call[1].aria2.resume_download for call in backend.calls] == [
        resume_download,
        resume_download,
    ]
    assert backend.first_attempt_staging_exists is resume_download
    assert backend.first_attempt_control_exists is resume_download
    assert backend.second_attempt_staging_exists is resume_download
    assert backend.second_attempt_control_exists is resume_download
    assert backend.second_attempt_staging_bytes == (
        b"partial" if resume_download else None
    )


@pytest.mark.parametrize(
    "invalid_resume_state",
    ["symlink-part", "orphan-sidecar", "unreadable-part"],
)
def test_runtime_file_aria2_resume_cleans_invalid_current_staging_before_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_resume_state: str,
) -> None:
    class ObservingAria2Backend(FakeDownloadBackend):
        def __init__(self) -> None:
            super().__init__(payload=b"downloaded")
            self.first_attempt_target_exists: bool | None = None
            self.first_attempt_target_is_symlink: bool | None = None
            self.first_attempt_control_exists: bool | None = None

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            self.first_attempt_target_exists = item.target.exists()
            self.first_attempt_target_is_symlink = item.target.is_symlink()
            self.first_attempt_control_exists = Path(f"{item.target}.aria2").exists()
            item.target.write_bytes(self.payload)

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "downloader": "aria2",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    staging_target = _staging_target(plan.items[0])
    staging_target.parent.mkdir(parents=True)
    outside_target = tmp_path / "outside.bin"
    outside_target.write_bytes(b"outside")
    if invalid_resume_state == "symlink-part":
        staging_target.symlink_to(outside_target)
    elif invalid_resume_state == "orphan-sidecar":
        Path(f"{staging_target}.aria2").write_bytes(b"orphan-control")
    else:
        staging_target.write_bytes(b"unreadable-partial")
        original_open = Path.open

        def open_with_unreadable_staging(
            self: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            mode = str(args[0] if args else kwargs.get("mode", "r"))
            if self == staging_target and "r" in mode:
                raise OSError("staging file cannot be read in test")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_with_unreadable_staging)
    backend = ObservingAria2Backend()

    process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate(
            {"cdh": {"downloader": {"aria2": {"resume_download": True}}}}
        ),
        backends={"aria2": backend},
        log=lambda message: None,
    )

    assert backend.first_attempt_target_exists is False
    assert backend.first_attempt_target_is_symlink is False
    assert backend.first_attempt_control_exists is False
    assert outside_target.read_bytes() == b"outside"
    assert (comfyui / "models" / "a.bin").read_bytes() == b"downloaded"


def test_runtime_file_download_exhausted_fail_stops_later_files(
    tmp_path: Path,
) -> None:
    class FailingBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.write_bytes(b"partial")
            raise TransferDownloadFilesError("transfer failed")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    },
                    {
                        "url": "https://example.com/b.bin",
                        "dir": "models",
                        "filename": "b.bin",
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FailingBackend()
    messages: list[str] = []

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {
                    "cdh": {
                        "default_downloader": "httpx",
                        "download_max_attempts": 2,
                        "download_failure_policy": "fail",
                    }
                }
            ),
            backends={"httpx": backend},
            log=messages.append,
        )

    assert len(backend.calls) == 2
    assert [call[0].url for call in backend.calls] == [
        "https://example.com/a.bin",
        "https://example.com/a.bin",
    ]
    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "target"), "runtime_file.download_failed")
    ]
    assert not (comfyui / "models" / "a.bin").exists()
    assert not backend.calls[0][0].target.exists()
    assert not (comfyui / "models" / "b.bin").exists()
    assert any(
        "WARNING: Runtime download exhausted: mode=sync target=models/a.bin "
        "backend=httpx attempts=2/2 policy=fail status=exhausted" in message
        for message in messages
    )


def test_runtime_file_download_exhausted_continue_omits_failed_and_continues(
    tmp_path: Path,
) -> None:
    class FirstFileFailingBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            if item.filename == "a.bin":
                item.target.write_bytes(b"partial")
                raise TransferDownloadFilesError("transfer failed")
            item.target.write_bytes(b"later")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    },
                    {
                        "url": "https://example.com/b.bin",
                        "dir": "models",
                        "filename": "b.bin",
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FirstFileFailingBackend()
    messages: list[str] = []

    results = process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate(
            {
                "cdh": {
                    "default_downloader": "httpx",
                    "download_max_attempts": 2,
                    "download_failure_policy": "continue",
                }
            }
        ),
        backends={"httpx": backend},
        log=messages.append,
    )

    assert [result.item.filename for result in results] == ["b.bin"]
    assert [call[0].url for call in backend.calls] == [
        "https://example.com/a.bin",
        "https://example.com/a.bin",
        "https://example.com/b.bin",
    ]
    failed_staging = _staging_target(plan.items[0])
    assert not (comfyui / "models" / "a.bin").exists()
    assert not failed_staging.exists()
    assert (comfyui / "models" / "b.bin").read_bytes() == b"later"
    assert any(
        "WARNING: runtime file download failed after 2 attempt(s), continuing"
        in message
        for message in messages
    )
    assert any(
        "WARNING: Runtime download exhausted: mode=sync target=models/a.bin "
        "backend=httpx attempts=2/2 policy=continue status=exhausted" in message
        for message in messages
    )


def test_runtime_file_download_diagnostics_do_not_leak_url_or_secret_values(
    tmp_path: Path,
) -> None:
    class SecretFailingBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.write_bytes(b"partial")
            raise TransferDownloadFilesError(
                "failed https://example.com/a.bin?token=raw-token "
                "password=hunter2 Authorization: Bearer bearer-secret"
            )

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": (
                            "https://user:source-password@example.com/a.bin"
                            "?token=source-token#fragment"
                        ),
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    messages: list[str] = []

    with pytest.raises(RuntimeFileDownloadError):
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {
                    "cdh": {
                        "default_downloader": "httpx",
                        "download_max_attempts": 1,
                        "download_failure_policy": "fail",
                    }
                }
            ),
            backends={"httpx": SecretFailingBackend()},
            log=messages.append,
        )

    output = "\n".join(messages)
    assert "source_host=example.com" in output
    assert "https://user:source-password@example.com" not in output
    assert "source-password" not in output
    assert "source-token" not in output
    assert "fragment" not in output
    assert "raw-token" not in output
    assert "hunter2" not in output
    assert "bearer-secret" not in output


def test_runtime_file_download_continue_policy_keeps_plain_download_error_fatal(
    tmp_path: Path,
) -> None:
    class FailingBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.write_bytes(b"partial")
            raise DownloadFilesError("local setup failed")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FailingBackend()

    with pytest.raises(DownloadFilesError, match="local setup failed"):
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {
                    "cdh": {
                        "default_downloader": "httpx",
                        "download_max_attempts": 2,
                        "download_failure_policy": "continue",
                    }
                }
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert len(backend.calls) == 1
    assert not (comfyui / "models" / "a.bin").exists()
    assert not backend.calls[0][0].target.exists()


def test_runtime_file_httpx_download_places_staged_bytes_at_final_target(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/a.bin"
        return httpx.Response(200, content=b"runtime-bytes")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    results = download_runtime_files(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        httpx_downloader=HttpxDownloader(
            transport=httpx.MockTransport(handler),
            sleep=lambda seconds: None,
        ),
        log=lambda message: None,
    )

    final_target = comfyui / "models" / "a.bin"
    staging_target = _staging_target(plan.items[0])
    assert results[0].staging_target == staging_target
    assert final_target.read_bytes() == b"runtime-bytes"
    assert not staging_target.exists()
    assert not staging_target.parent.exists()


def test_runtime_file_aria2_factory_is_used_only_when_needed(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    httpx_plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/httpx.bin",
                        "dir": "models",
                        "filename": "httpx.bin",
                        "downloader": "httpx",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    aria2_plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/aria2.bin",
                        "dir": "models",
                        "filename": "aria2.bin",
                        "downloader": "aria2",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    httpx_backend = FakeDownloadBackend()
    aria2_backend = FakeManagedDownloadBackend()
    factory = FakeAria2Factory(aria2_backend)
    config = RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}})
    skipped_target = comfyui / "models" / "skipped.bin"
    skipped_target.parent.mkdir(parents=True)
    skipped_target.write_bytes(b"already present")
    skipped_aria2_plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/skipped.bin",
                        "dir": "models",
                        "filename": "skipped.bin",
                        "downloader": "aria2",
                        "overwrite": False,
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    download_runtime_files(
        httpx_plan,
        config=config,
        httpx_downloader=httpx_backend,
        aria2_downloader_factory=factory,
        log=lambda message: None,
    )
    assert factory.calls == []

    download_runtime_files(
        skipped_aria2_plan,
        config=config,
        httpx_downloader=httpx_backend,
        aria2_downloader_factory=factory,
        log=lambda message: None,
    )
    assert factory.calls == []

    results = download_runtime_files(
        aria2_plan,
        config=config,
        httpx_downloader=httpx_backend,
        aria2_downloader_factory=factory,
        log=lambda message: None,
    )

    assert len(factory.calls) == 1
    assert aria2_backend.entered is True
    assert aria2_backend.exited is True
    assert aria2_backend.prepare_calls == []
    assert aria2_backend.calls[0][0].target == _staging_target(aria2_plan.items[0])
    assert results[0].staging_target == aria2_backend.calls[0][0].target


def test_download_runtime_files_startup_observer_runs_before_transfer(
    tmp_path: Path,
) -> None:
    class OrderingBackend(FakeManagedDownloadBackend):
        def __enter__(self) -> OrderingBackend:
            events.append("enter")
            return super().__enter__()

        def prepare(self, settings: DownloaderSettings) -> None:
            del settings
            events.append("prepare")

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            del settings
            events.append("download")
            item.target.write_bytes(b"downloaded")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "downloader": "aria2",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    events: list[str] = []
    backend = OrderingBackend()

    class OrderingFactory(FakeAria2Factory):
        def __call__(self, *, log: Logger) -> FakeManagedDownloadBackend:
            events.append("factory")
            return super().__call__(log=log)

    factory = OrderingFactory(backend)

    download_runtime_files(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        aria2_downloader_factory=factory,
        log=lambda message: None,
        startup_observer=lambda: events.append("startup"),
    )

    assert events == ["factory", "enter", "prepare", "startup", "download"]


def test_runtime_file_download_reports_unavailable_backend(tmp_path: Path) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "aria2"}}
            ),
            backends={"httpx": FakeDownloadBackend()},
            log=lambda message: None,
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "downloader"), "runtime_file.downloader_unavailable")
    ]


def test_runtime_file_download_places_successful_transfer_at_final_target(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FakeDownloadBackend(payload=b"final-bytes")

    results = process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        backends={"httpx": backend},
        log=lambda message: None,
    )

    final_target = comfyui / "models" / "a.bin"
    assert final_target.read_bytes() == b"final-bytes"
    assert results[0].staging_target == _staging_target(plan.items[0])
    assert backend.calls[0][0].target == results[0].staging_target
    assert not results[0].staging_target.exists()


def test_runtime_file_download_skips_existing_without_backend_call(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    final_target = comfyui / "models" / "a.bin"
    final_target.parent.mkdir(parents=True)
    final_target.write_bytes(b"existing")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": False,
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FakeDownloadBackend(payload=b"new")

    results = process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        backends={"httpx": backend},
        log=lambda message: None,
    )

    assert final_target.read_bytes() == b"existing"
    assert backend.calls == []
    assert results[0].status.value == "skipped"


def test_runtime_file_download_overwrite_true_replaces_existing_after_success(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    final_target = comfyui / "models" / "a.bin"
    final_target.parent.mkdir(parents=True)
    final_target.write_bytes(b"old")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": True,
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        backends={"httpx": FakeDownloadBackend(payload=b"new")},
        log=lambda message: None,
    )

    assert final_target.read_bytes() == b"new"


def test_runtime_file_download_failure_keeps_final_absent_and_cleans_partial(
    tmp_path: Path,
) -> None:
    class FailingBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.write_bytes(b"partial")
            raise RuntimeError("backend failed")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FailingBackend()

    with pytest.raises(RuntimeError, match="backend failed"):
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "httpx"}}
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    final_target = comfyui / "models" / "a.bin"
    assert not final_target.exists()
    assert not backend.calls[0][0].target.exists()


def test_runtime_file_download_failure_keeps_existing_final_unchanged(
    tmp_path: Path,
) -> None:
    class FailingBackend(FakeDownloadBackend):
        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.write_bytes(b"partial")
            raise RuntimeError("backend failed")

    comfyui = tmp_path / "ComfyUI"
    final_target = comfyui / "models" / "a.bin"
    final_target.parent.mkdir(parents=True)
    final_target.write_bytes(b"old")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": True,
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FailingBackend()

    with pytest.raises(RuntimeError, match="backend failed"):
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "httpx"}}
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert final_target.read_bytes() == b"old"
    assert not backend.calls[0][0].target.exists()


def test_runtime_file_download_rejects_racing_final_when_overwrite_false(
    tmp_path: Path,
) -> None:
    class RacingBackend(FakeDownloadBackend):
        def __init__(self, final_target: Path) -> None:
            super().__init__(payload=b"new")
            self._final_target = final_target

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            super().download(item, settings)
            self._final_target.write_bytes(b"raced")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    final_target = comfyui / "models" / "a.bin"
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": False,
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = RacingBackend(final_target)

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "httpx"}}
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "target"), "runtime_file.final_target_exists")
    ]
    assert final_target.read_bytes() == b"raced"
    assert not backend.calls[0][0].target.exists()


# Containment race tests cover symlink swaps between planning, staging mkdir,
# backend completion, and final replacement; keep these with the positive case.
def test_runtime_file_download_rejects_parent_symlink_inserted_after_planning(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    outside = tmp_path / "outside"
    comfyui.mkdir()
    outside.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    (comfyui / "models").symlink_to(outside, target_is_directory=True)
    backend = FakeDownloadBackend(payload=b"new")

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "httpx"}}
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "target"), "runtime_file.symlink_escape")
    ]
    assert backend.calls == []
    assert not (outside / "a.bin").exists()
    assert not (outside / ".cdh-staging").exists()


def test_runtime_file_download_cleans_staging_parent_after_mkdir_symlink_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    outside = tmp_path / "outside"
    comfyui.mkdir()
    outside.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    staging_parent = _staging_target(plan.items[0]).parent
    original_mkdir = Path.mkdir

    def swap_parent_before_staging_mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if self == staging_parent:
            (comfyui / "models").symlink_to(outside, target_is_directory=True)
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", swap_parent_before_staging_mkdir)
    backend = FakeDownloadBackend(payload=b"new")

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "httpx"}}
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "target"), "runtime_file.symlink_escape")
    ]
    assert backend.calls == []
    assert not (outside / "a.bin").exists()
    assert not (outside / ".cdh-staging").exists()


def test_runtime_file_download_rejects_parent_symlink_swap_before_final_replace(
    tmp_path: Path,
) -> None:
    class SwappingBackend(FakeDownloadBackend):
        def __init__(self, outside_parent: Path) -> None:
            super().__init__(payload=b"new")
            self._outside_parent = outside_parent

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.write_bytes(self.payload)
            outside_staging = self._outside_parent / ".cdh-staging"
            outside_staging.mkdir()
            (outside_staging / item.target.name).write_bytes(self.payload)
            item.target.unlink()
            item.target.parent.rmdir()
            item.target.parent.parent.rmdir()
            item.target.parent.parent.symlink_to(
                self._outside_parent,
                target_is_directory=True,
            )

    comfyui = tmp_path / "ComfyUI"
    outside = tmp_path / "outside"
    comfyui.mkdir()
    outside.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = SwappingBackend(outside)

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "httpx"}}
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "target"), "runtime_file.symlink_escape")
    ]
    assert not (outside / "a.bin").exists()
    assert not (comfyui / "models" / "a.bin").exists()


def test_runtime_file_download_overwrites_existing_file_under_real_parent(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    final_target = comfyui / "models" / "checkpoints" / "a.bin"
    final_target.parent.mkdir(parents=True)
    final_target.write_bytes(b"old")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                        "overwrite": True,
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        backends={"httpx": FakeDownloadBackend(payload=b"new")},
        log=lambda message: None,
    )

    assert final_target.read_bytes() == b"new"


def test_runtime_file_download_continue_policy_keeps_atomic_place_error_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FakeDownloadBackend(payload=b"new")
    original_replace = Path.replace

    def failing_replace(self: Path, target: Path) -> Path:
        if self == _staging_target(plan.items[0]):
            raise OSError("replace failed in test")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {
                    "cdh": {
                        "default_downloader": "httpx",
                        "download_failure_policy": "continue",
                    }
                }
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "target"), "runtime_file.final_replace_failed")
    ]
    assert not (comfyui / "models" / "a.bin").exists()
    assert not backend.calls[0][0].target.exists()


def test_runtime_file_download_post_transfer_runtime_error_observes_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "download_mode": "async",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FakeDownloadBackend(payload=b"new")
    messages: list[str] = []
    observed_states: list[str] = []
    original_replace = Path.replace

    def failing_replace(self: Path, target: Path) -> Path:
        if self == _staging_target(plan.items[0]):
            raise OSError("replace failed in test")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {
                    "cdh": {
                        "default_downloader": "httpx",
                        "download_failure_policy": "fail",
                    }
                }
            ),
            backends={"httpx": backend},
            log=messages.append,
            state_observer=lambda item, status, error=None: observed_states.append(
                status
            ),
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "target"), "runtime_file.final_replace_failed")
    ]
    assert observed_states == ["downloading", "exhausted"]
    assert not any(
        "Async runtime download queue stopping: reason=download_exhausted" in message
        for message in messages
    )
    assert not (comfyui / "models" / "a.bin").exists()
    assert not backend.calls[0][0].target.exists()


def test_runtime_file_download_cleans_only_stale_fixed_pattern_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    staging_dir = comfyui / "models" / ".cdh-staging"
    staging_dir.mkdir(parents=True)
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    current_control = Path(f"{_staging_target(plan.items[0])}.aria2")
    old_digest = "a" * 64
    fresh_digest = "b" * 64
    unreadable_digest = "c" * 64
    stale = staging_dir / f"cdh-{old_digest}.part"
    stale_tmp = staging_dir / f"cdh-{old_digest}.part.tmp"
    stale_control = staging_dir / f"cdh-{old_digest}.part.aria2"
    fresh = staging_dir / f"cdh-{fresh_digest}.part"
    unreadable_mtime = staging_dir / f"cdh-{unreadable_digest}.part"
    symlink_artifact = staging_dir / f"cdh-{'d' * 64}.part"
    user_file = staging_dir / "user-file.txt"
    unrecognized_staging_artifact = staging_dir / "old.bin.cdh-download"
    outside = comfyui / "models" / f"cdh-{'e' * 64}.part"
    for path in (
        current_control,
        stale,
        stale_tmp,
        stale_control,
        fresh,
        unreadable_mtime,
        user_file,
        unrecognized_staging_artifact,
        outside,
    ):
        path.write_bytes(b"keep-or-clean")
    symlink_artifact.symlink_to(outside)
    old_mtime = STALE_CLEANUP_NOW - (25 * 60 * 60)
    fresh_mtime = STALE_CLEANUP_NOW - (23 * 60 * 60)
    for path in (
        current_control,
        stale,
        stale_tmp,
        stale_control,
        unreadable_mtime,
        symlink_artifact,
        user_file,
        unrecognized_staging_artifact,
        outside,
    ):
        _touch_mtime(path, old_mtime)
    _touch_mtime(fresh, fresh_mtime)

    original_lstat = Path.lstat

    def lstat_with_unreadable_mtime(self: Path) -> os.stat_result:
        if self == unreadable_mtime:
            raise OSError("mtime unavailable in test")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", lstat_with_unreadable_mtime)

    process_runtime_file_downloads(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "aria2"}}),
        backends={"aria2": FakeDownloadBackend(payload=b"new")},
        log=lambda message: None,
        staging_cleanup_clock=lambda: STALE_CLEANUP_NOW,
    )

    assert not stale.exists()
    assert not stale_tmp.exists()
    assert not stale_control.exists()
    assert not current_control.exists()
    assert fresh.read_bytes() == b"keep-or-clean"
    assert unreadable_mtime.name in {path.name for path in staging_dir.iterdir()}
    assert symlink_artifact.is_symlink()
    assert user_file.read_bytes() == b"keep-or-clean"
    assert unrecognized_staging_artifact.read_bytes() == b"keep-or-clean"
    assert outside.read_bytes() == b"keep-or-clean"
    assert (comfyui / "models" / "a.bin").read_bytes() == b"new"


# Verifies sync cleanup preserves active async resumable staging artifacts.
def test_runtime_file_sync_cleanup_protects_current_async_staging(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/sync.bin",
                        "dir": "models",
                        "filename": "sync.bin",
                        "download_mode": "sync",
                    },
                    {
                        "url": "https://example.com/async.bin",
                        "dir": "models",
                        "filename": "async.bin",
                        "download_mode": "async",
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    sync_item, async_item = plan.items
    sync_plan = RuntimeFilePlan(items=(sync_item,))
    async_staging = _staging_target(async_item)
    async_control = Path(f"{async_staging}.aria2")
    stale_staging = async_staging.parent / f"cdh-{'a' * 64}.part"
    async_staging.parent.mkdir(parents=True)
    async_staging.write_bytes(b"async-partial")
    async_control.write_bytes(b"async-control")
    stale_staging.write_bytes(b"stale")
    old_mtime = STALE_CLEANUP_NOW - (25 * 60 * 60)
    for path in (async_staging, async_control, stale_staging):
        _touch_mtime(path, old_mtime)
    backend = FakeDownloadBackend(payload=b"sync")

    process_runtime_file_downloads(
        sync_plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        backends={"httpx": backend},
        log=lambda message: None,
        staging_cleanup_clock=lambda: STALE_CLEANUP_NOW,
        extra_protected_staging_targets=(async_staging,),
    )

    assert [call[0].target for call in backend.calls] == [_staging_target(sync_item)]
    assert (comfyui / "models" / "sync.bin").read_bytes() == b"sync"
    assert not stale_staging.exists()
    assert async_staging.read_bytes() == b"async-partial"
    assert async_control.read_bytes() == b"async-control"
    assert not (comfyui / "models" / "async.bin").exists()


def test_runtime_file_download_rejects_symlinked_staging_parent(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    outside = tmp_path / "outside"
    models = comfyui / "models"
    models.mkdir(parents=True)
    outside.mkdir()
    (models / ".cdh-staging").symlink_to(outside, target_is_directory=True)
    outside_artifact = outside / f"cdh-{'f' * 64}.part"
    outside_artifact.write_bytes(b"keep")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = FakeDownloadBackend(payload=b"new")

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "httpx"}}
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "target"), "runtime_file.staging_parent_invalid")
    ]
    assert backend.calls == []
    assert outside_artifact.read_bytes() == b"keep"
    assert not (models / "a.bin").exists()


def test_runtime_file_download_rejects_symlinked_staging_file(
    tmp_path: Path,
) -> None:
    class SymlinkBackend(FakeDownloadBackend):
        def __init__(self, outside_target: Path) -> None:
            super().__init__()
            self._outside_target = outside_target

        def download(
            self,
            item: FileDownloadItem,
            settings: DownloaderSettings,
        ) -> None:
            self.calls.append((item, settings))
            item.target.symlink_to(self._outside_target)

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    backend = SymlinkBackend(outside)

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "httpx"}}
            ),
            backends={"httpx": backend},
            log=lambda message: None,
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "target"), "runtime_file.non_regular_staging")
    ]
    assert outside.read_bytes() == b"outside"
    assert not (comfyui / "models" / "a.bin").exists()
    assert not backend.calls[0][0].target.exists()


@pytest.mark.parametrize(
    ("directory", "code"),
    [
        ("/models", "runtime_file.absolute_directory"),
        ("", "runtime_file.empty_directory_segment"),
        ("models//checkpoints", "runtime_file.empty_directory_segment"),
        ("models/.", "runtime_file.current_directory_segment"),
        ("models/../checkpoints", "runtime_file.parent_directory_segment"),
        ("models/", "runtime_file.trailing_slash"),
    ],
)
# Unsafe path validation keeps runtime downloads confined to the ComfyUI tree.
def test_runtime_file_plan_rejects_unsafe_directories(
    tmp_path: Path,
    directory: str,
    code: str,
) -> None:
    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": directory,
                            "filename": "a.bin",
                        }
                    ]
                }
            ],
            comfyui_path=tmp_path / "ComfyUI",
        )

    assert _identities(error.value) == [(("files", 0, "dir"), code)]


@pytest.mark.parametrize(
    "filename", ["", ".", "..", "nested/name.bin", "bad\\name.bin"]
)
def test_runtime_file_plan_rejects_unsafe_filenames(
    tmp_path: Path,
    filename: str,
) -> None:
    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": filename,
                        }
                    ]
                }
            ],
            comfyui_path=tmp_path / "ComfyUI",
        )

    assert _identities(error.value) == [
        (("files", 0, "filename"), "runtime_file.invalid_filename")
    ]


def test_runtime_file_plan_rejects_non_http_url(tmp_path: Path) -> None:
    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "file:///tmp/a.bin",
                            "dir": "models",
                            "filename": "a.bin",
                        }
                    ]
                }
            ],
            comfyui_path=tmp_path / "ComfyUI",
        )

    assert _identities(error.value) == [
        (("files", 0, "url"), "runtime_file.invalid_url")
    ]


def test_runtime_file_plan_records_existing_regular_target_behavior(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    target = comfyui / "models" / "a.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/skip.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": False,
                    },
                    {
                        "url": "https://example.com/overwrite.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": True,
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    assert [item.action for item in plan.items] == [
        "overwrite_existing",
    ]
    assert plan.items[0].overwrite is True


def test_runtime_file_plan_existing_regular_target_without_overwrite_skips(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    target = comfyui / "models" / "a.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/skip.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    assert plan.items[0].action == "skip_existing"


def test_runtime_file_plan_rejects_non_regular_existing_target(tmp_path: Path) -> None:
    comfyui = tmp_path / "ComfyUI"
    target = comfyui / "models" / "a.bin"
    target.mkdir(parents=True)

    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": "a.bin",
                        }
                    ]
                }
            ],
            comfyui_path=comfyui,
        )

    assert _identities(error.value) == [
        (("files", 0, "target"), "runtime_file.non_regular_target")
    ]


def test_runtime_file_plan_rejects_symlink_escape(tmp_path: Path) -> None:
    comfyui = tmp_path / "ComfyUI"
    outside = tmp_path / "outside"
    comfyui.mkdir()
    outside.mkdir()
    (comfyui / "models").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": "a.bin",
                        }
                    ]
                }
            ],
            comfyui_path=comfyui,
        )

    assert _identities(error.value) == [
        (("files", 0, "target"), "runtime_file.symlink_escape")
    ]


def test_runtime_file_plan_rejects_unsupported_download_mode(tmp_path: Path) -> None:
    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": "a.bin",
                            "download_mode": "parallel",
                        }
                    ]
                }
            ],
            comfyui_path=tmp_path / "ComfyUI",
        )

    assert _identities(error.value) == [
        (("files", 0, "download_mode"), "schema.literal_error")
    ]
