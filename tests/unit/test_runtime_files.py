"""Runtime file planning, reconciliation, and transfer-policy tests."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

import pytest

from comfyui_docker_helper.config.runtime_models import RuntimeConfig
from comfyui_docker_helper.container.download_files import (
    DownloadCancelled,
    DownloaderSettings,
    DownloadFilesError,
    Logger,
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
    TransportRequest,
    TransportResult,
)
from comfyui_docker_helper.container.runtime_files import (
    RuntimeFileDownloadError,
    RuntimeFilePlan,
    RuntimeFilePlanError,
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
)
from comfyui_docker_helper.container.transfer_core import (
    DownloadStatus,
    VerificationStatus,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
LATER = datetime(2026, 1, 2, 4, 5, 6, tzinfo=UTC)


def _checksum(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _identities(error: RuntimeFilePlanError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


def _state(
    entries: dict[str, RuntimeDownloadEntry] | None = None,
) -> RuntimeState:
    return RuntimeState(
        schema_version=1,
        updated_at=NOW,
        run_id="run-1",
        downloads=RuntimeDownloadsState(entries=entries or {}),
    )


def _entry(
    *,
    target: str = "models/a.bin",
    status: str = "completed",
) -> RuntimeDownloadEntry:
    return RuntimeDownloadEntry(
        target=target,
        download_mode="sync",
        status=status,
        attempts=1,
        attempt_run_id="run-1",
        last_error=None,
        updated_at=NOW,
    )


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
    return build_runtime_file_plan([{"files": list(files)}], comfyui_path=root)


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
        failures: list[Exception] | None = None,
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
    ) -> TransportResult:
        self.calls.append((request, settings))
        with request.sink.open_for_write() as output:
            output.write(self.payload)
        if self.failures:
            raise self.failures.pop(0)
        return TransportResult(length=len(self.payload))

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


def test_runtime_staging_uses_desired_identity_digest(tmp_path: Path) -> None:
    item = _plan(tmp_path / "ComfyUI", _file("a.bin")).items[0]
    digest = runtime_file_identity_digest(item)

    assert runtime_file_staging_target(item) == (
        item.target.parent
        / ".cdh-staging"
        / f"cdh-{digest.removeprefix('sha256:')}.part"
    )


def test_runtime_file_merge_overrides_by_target_and_empty_list_resets() -> None:
    baked = {"files": [_file("a.bin"), _file("b.bin")]}
    mounted = {
        "files": [
            {
                **_file("a.bin"),
                "url": "https://mirror.test/a.bin",
                "checksum": _checksum(b"a"),
            }
        ]
    }

    merged = merge_runtime_file_items([baked, mounted])

    assert [item["filename"] for item in merged] == ["a.bin", "b.bin"]
    assert merged[0]["url"] == "https://mirror.test/a.bin"
    assert merged[0]["checksum"] == _checksum(b"a")
    assert merge_runtime_file_items([baked, {"files": []}]) == ()


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

    result = reconcile_runtime_file_plan(plan, _state(), now=LATER, comfyui_path=root)

    assert result.download_plan.items == plan.items
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
    digest = runtime_file_identity_digest(item)

    result = reconcile_runtime_file_plan(
        plan,
        _state({digest: _entry()}),
        now=LATER,
        comfyui_path=root,
    )

    assert result.download_plan.items == plan.items
    assert result.items[0].status == "pending"


def test_reconciliation_reuses_completed_checksum_free_regular_final(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin"))
    item = plan.items[0]
    item.target.parent.mkdir()
    item.target.write_bytes(b"existing")
    digest = runtime_file_identity_digest(item)

    result = reconcile_runtime_file_plan(
        plan,
        _state({digest: _entry()}),
        now=LATER,
        comfyui_path=root,
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
    digest = runtime_file_identity_digest(plan.items[0])

    with pytest.raises(DownloadFilesError, match="not a real directory"):
        reconcile_runtime_file_plan(
            plan,
            _state({digest: _entry()}),
            now=LATER,
            comfyui_path=root,
        )


def test_reconciliation_reschedules_completed_entry_when_final_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin"))
    digest = runtime_file_identity_digest(plan.items[0])

    result = reconcile_runtime_file_plan(
        plan,
        _state({digest: _entry()}),
        now=LATER,
        comfyui_path=root,
    )

    assert result.download_plan.items == plan.items
    assert result.state.downloads.entries[digest].status == "pending"


def test_reconciliation_reports_exact_stale_state_staging_without_deleting(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    stale_digest = f"sha256:{'a' * 64}"
    stale = root / "models" / ".cdh-staging" / f"cdh-{'a' * 64}.part"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial")

    result = reconcile_runtime_file_plan(
        RuntimeFilePlan(items=()),
        _state({stale_digest: _entry()}),
        now=LATER,
        comfyui_path=root,
    )

    assert result.stale_entry_digests == frozenset({stale_digest})
    assert result.stale_staging_candidates == (stale,)
    assert stale.read_bytes() == b"partial"


def test_stale_same_target_identity_transition_uses_stale_digest_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(root, _file("a.bin", checksum=_checksum(b"current")))
    current_staging = runtime_file_staging_target(plan.items[0])
    stale_digest = f"sha256:{'a' * 64}"

    result = reconcile_runtime_file_plan(
        plan,
        _state({stale_digest: _entry(target="models/a.bin")}),
        now=LATER,
        comfyui_path=root,
    )

    expected = root / "models" / ".cdh-staging" / f"cdh-{'a' * 64}.part"
    assert result.stale_staging_candidates == (expected,)
    assert result.stale_staging_candidates != (current_staging,)


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
    backend = FakeBackend(failures=[TransferDownloadFilesError("temporary")])

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
            TransferDownloadFilesError("temporary"),
            TransferDownloadFilesError("temporary"),
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
            TransferDownloadFilesError("temporary"),
            TransferDownloadFilesError("temporary"),
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
    backend = FakeBackend(failures=[TerminalTransferDownloadFilesError("not found")])

    if policy == "fail":
        with pytest.raises(RuntimeFileDownloadError):
            process_runtime_file_downloads(
                plan,
                config=_config(policy=policy, attempts=3),
                backends={"httpx": backend},
                log=lambda _: None,
            )
    else:
        results = process_runtime_file_downloads(
            plan,
            config=_config(policy=policy, attempts=3),
            backends={"httpx": backend},
            log=lambda _: None,
        )
        assert [result.item.filename for result in results] == ["b.bin"]

    assert len(backend.calls) == (1 if policy == "fail" else 2)


def test_runtime_continue_cannot_mask_local_target_invariant(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"))
    plan.items[0].target.mkdir(parents=True)
    backend = FakeBackend()

    with pytest.raises(DownloadFilesError, match="not a regular file"):
        process_runtime_file_downloads(
            plan,
            config=_config(policy="continue"),
            backends={"httpx": backend},
            log=lambda _: None,
        )

    assert backend.calls == []


def test_runtime_cancelled_transfer_stops_without_exhausted_failure(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"), _file("b.bin"))
    backend = FakeBackend(failures=[DownloadCancelled("cancelled")])
    statuses: list[str] = []

    results = process_runtime_file_downloads(
        plan,
        config=_config(),
        backends={"httpx": backend},
        state_observer=lambda item, status, **_: statuses.append(status),
        log=lambda _: None,
    )

    assert results == ()
    assert statuses == ["downloading"]
    assert len(backend.calls) == 1


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

        def download(self, request, settings) -> TransportResult:
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
