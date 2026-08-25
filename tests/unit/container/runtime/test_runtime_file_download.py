"""Runtime file download policy, backend, and state-observer tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import TracebackType

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

from comfyui_docker_helper.container.runtime.events import (
    RuntimeDownloadAttemptStarted,
    RuntimeDownloadFailed,
    RuntimeDownloadItemCompleted,
    RuntimeDownloadItemProgress,
    RuntimeDownloadItemRetryScheduled,
    RuntimeDownloadItemVerificationStarted,
)
from comfyui_docker_helper.container.runtime.files.download import (
    download_runtime_files,
    process_runtime_file_downloads,
)
from comfyui_docker_helper.container.runtime.files.models import (
    RuntimeFileDownloadError,
    RuntimeFilePlan,
    RuntimeFilePlanItem,
)
from comfyui_docker_helper.container.runtime.files.planning import (
    runtime_file_identity_digest,
    runtime_file_staging_target,
)
from comfyui_docker_helper.container.runtime.files.reconciliation import (
    reconcile_runtime_file_plan,
)
from comfyui_docker_helper.container.runtime.state import (
    RuntimeResumeState,
    RuntimeState,
    RuntimeStateError,
)
from comfyui_docker_helper.container.runtime_download_state import (
    RuntimeDownloadStateWriter,
)
from comfyui_docker_helper.container.transfer import core as transfer_core
from comfyui_docker_helper.container.transfer.core import (
    DownloaderSettings,
    DownloadFilesError,
    DownloadStatus,
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
from comfyui_docker_helper.container.transfer.credentials import (
    DownloaderCredentialError,
)
from comfyui_docker_helper.container.transfer.events import DownloadTransferProgress


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

    def __call__(self) -> FakeBackend:
        return self.backend


def test_runtime_consumer_selects_backends_and_returns_typed_outcomes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    checksum = _checksum(b"downloaded")
    plan = _plan(
        root,
        _file("a.bin", downloader="httpx", checksum=checksum),
        _file("b.bin"),
    )
    httpx_backend = FakeBackend()
    aria2_backend = FakeBackend()

    results = process_runtime_file_downloads(
        plan,
        config=_config(default="aria2"),
        backends={"httpx": httpx_backend, "aria2": aria2_backend},
        event_sink=RecordingRuntimeEventSink(),
    )

    assert [result.status for result in results] == [
        DownloadStatus.DOWNLOADED,
        DownloadStatus.DOWNLOADED,
    ]
    assert [result.backend for result in results] == ["httpx", "aria2"]
    assert results[0].outcome.verification is VerificationStatus.VERIFIED
    assert results[0].outcome.observed_checksum == checksum
    assert results[1].outcome.observed_checksum is None
    assert results[0].staging_target == runtime_file_staging_target(
        plan.items[0],
    )
    assert plan.items[0].target.read_bytes() == b"downloaded"
    assert httpx_backend.calls[0][0].sink.display_path != plan.items[0].target


def test_runtime_verified_existing_target_skips_credential_and_backend(
    tmp_path: Path,
) -> None:
    content = b"already complete"
    plan = _plan(
        tmp_path / "ComfyUI",
        _file("a.bin", checksum=_checksum(content)),
    )
    item = plan.items[0]
    item.target.parent.mkdir(parents=True)
    item.target.write_bytes(content)
    backend = FakeBackend()
    recorder = RecordingRuntimeEventSink()

    class CredentialMustRemainLazy:
        def authorization_for(self, _url: object) -> bytes | None:
            pytest.fail("a skipped transfer must not acquire its credential")

    results = process_runtime_file_downloads(
        plan,
        config=_config(policy="fail"),
        backends={"httpx": backend},
        credential_policy=CredentialMustRemainLazy(),
        event_sink=recorder,
    )

    assert results[0].status is DownloadStatus.SKIPPED
    assert backend.calls == []
    completion = next(
        event
        for event in recorder.events
        if isinstance(event, RuntimeDownloadItemCompleted)
    )
    assert completion.attempts == 0
    assert not any(
        isinstance(event, RuntimeDownloadItemVerificationStarted)
        for event in recorder.events
    )


def test_runtime_retryable_failure_retries_then_completes(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"))
    backend = FakeBackend(
        failures=[TransportRetryable(TransportDiagnostic("httpx", "temporary"))]
    )

    recorder = RecordingRuntimeEventSink()

    result = process_runtime_file_downloads(
        plan,
        config=_config(attempts=2),
        backends={"httpx": backend},
        event_sink=recorder,
    )

    assert result[0].status is DownloadStatus.DOWNLOADED
    assert len(backend.calls) == 2
    assert [type(event) for event in recorder.events] == [
        RuntimeDownloadAttemptStarted,
        RuntimeDownloadItemRetryScheduled,
        RuntimeDownloadAttemptStarted,
        RuntimeDownloadItemVerificationStarted,
        RuntimeDownloadItemCompleted,
    ]
    assert all("https://" not in repr(event) for event in recorder.events)


def test_runtime_transport_progress_is_projected_to_background_scope(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"))
    progress = DownloadTransferProgress(
        transferred_bytes=4,
        total_bytes=10,
        stored_bytes=4,
        reported_rate=2,
    )

    class ProgressBackend(FakeBackend):
        def download(
            self,
            request: TransportRequest,
            settings: DownloaderSettings,
        ) -> TransportOutcome:
            assert request.progress_sink is not None
            request.progress_sink.emit(progress)
            return super().download(request, settings)

    operation_order: list[tuple[str, object]] = []

    class OrderedRecorder(RecordingRuntimeEventSink):
        def emit(self, event: object, /) -> None:
            operation_order.append(("emit", event))
            super().emit(event)

        def close_progress(self, scope: object) -> None:
            operation_order.append(("close", scope))
            super().close_progress(scope)

    recorder = OrderedRecorder()

    result = process_runtime_file_downloads(
        plan,
        config=_config(attempts=2),
        backends={"httpx": ProgressBackend()},
        event_sink=recorder,
    )

    assert result[0].status is DownloadStatus.DOWNLOADED
    assert len(recorder.progress_events) == 1
    scope, event = recorder.progress_events[0]
    assert event == RuntimeDownloadItemProgress(
        index=1,
        total=1,
        target="models/a.bin",
        mode="sync",
        attempt=1,
        max_attempts=2,
        progress=progress,
    )
    assert recorder.closed_progress_scopes == [scope]
    verification = next(
        event
        for operation, event in operation_order
        if operation == "emit"
        and isinstance(event, RuntimeDownloadItemVerificationStarted)
    )
    assert verification == RuntimeDownloadItemVerificationStarted(
        index=1,
        total=1,
        target="models/a.bin",
    )
    assert operation_order.index(("close", scope)) < operation_order.index(
        ("emit", verification)
    )


def test_runtime_verification_failure_never_claims_the_file_is_ready(
    tmp_path: Path,
) -> None:
    plan = _plan(
        tmp_path / "ComfyUI",
        _file("a.bin", checksum=_checksum(b"expected")),
    )
    recorder = RecordingRuntimeEventSink()

    results = process_runtime_file_downloads(
        plan,
        config=_config(policy="continue", attempts=1),
        backends={"httpx": FakeBackend(payload=b"unexpected")},
        event_sink=recorder,
    )

    assert results == ()
    assert any(
        isinstance(event, RuntimeDownloadItemVerificationStarted)
        for event in recorder.events
    )
    assert any(isinstance(event, RuntimeDownloadFailed) for event in recorder.events)
    assert not any(
        isinstance(event, RuntimeDownloadItemCompleted) for event in recorder.events
    )


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
        event_sink=RecordingRuntimeEventSink(),
    )

    assert [result.item.filename for result in results] == ["b.bin"]
    assert len(backend.calls) == 3


def test_runtime_fail_policy_stops_after_retryable_exhaustion(
    tmp_path: Path,
) -> None:
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
            event_sink=RecordingRuntimeEventSink(),
        )

    assert len(backend.calls) == 2
    assert not plan.items[1].target.exists()


@pytest.mark.parametrize(
    ("network_attempted", "expected_attempts"),
    [(False, 0), (True, 1)],
)
def test_runtime_credential_failure_is_not_retried_and_preserves_attempt_fact(
    tmp_path: Path,
    network_attempted: bool,
    expected_attempts: int,
) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"), _file("b.bin"))
    calls = 0

    class InitialCredentialPolicy:
        def __init__(self) -> None:
            self.calls = 0

        def authorization_for(self, _url: object) -> bytes | None:
            self.calls += 1
            if self.calls == 1:
                raise DownloaderCredentialError("credential unavailable")
            return None

    class CredentialBackend:
        def download(
            self,
            request: TransportRequest,
            settings: DownloaderSettings,
        ) -> TransportOutcome:
            nonlocal calls
            del request, settings
            calls += 1
            if network_attempted and calls == 1:
                raise DownloaderCredentialError(
                    "credential unavailable",
                    network_attempted=True,
                )
            return TransportSuccess(length=0, namespace="httpx", http_status=200)

    recorder = RecordingRuntimeEventSink()

    results = process_runtime_file_downloads(
        plan,
        config=_config(policy="continue", attempts=3),
        backends={"httpx": CredentialBackend()},
        credential_policy=(None if network_attempted else InitialCredentialPolicy()),
        event_sink=recorder,
    )

    assert [result.item.filename for result in results] == ["b.bin"]
    assert calls == (2 if network_attempted else 1)
    failure = next(
        event for event in recorder.events if isinstance(event, RuntimeDownloadFailed)
    )
    assert failure.attempts == expected_attempts


# Terminal item failures apply runtime policy once without spending retry budget.
@pytest.mark.parametrize("policy", ["continue", "fail"])
def test_runtime_terminal_failure_applies_policy_without_retry(
    tmp_path: Path,
    policy: str,
) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"), _file("b.bin"))
    backend = FakeBackend(
        failures=[
            TransportOrdinaryTerminal(
                TransportDiagnostic("aria2", "credential-url-raw-reason")
            )
        ]
    )
    statuses: list[str] = []
    recorder = RecordingRuntimeEventSink()
    failure_text = ""

    if policy == "fail":
        with pytest.raises(RuntimeFileDownloadError) as raised:
            process_runtime_file_downloads(
                plan,
                config=_config(policy=policy, attempts=3),
                backends={"httpx": backend},
                state_observer=lambda _item, status, **_: statuses.append(status),
                event_sink=recorder,
            )
        failure_text = str(raised.value)
    else:
        results = process_runtime_file_downloads(
            plan,
            config=_config(policy=policy, attempts=3),
            backends={"httpx": backend},
            state_observer=lambda _item, status, **_: statuses.append(status),
            event_sink=recorder,
        )
        assert [result.item.filename for result in results] == ["b.bin"]

    assert len(backend.calls) == (1 if policy == "fail" else 2)
    assert statuses[0] == "failed"
    if policy == "continue":
        assert statuses[-1] == "completed"
    failure = next(
        event for event in recorder.events if isinstance(event, RuntimeDownloadFailed)
    )
    assert (
        failure.target,
        failure.mode,
        failure.policy,
        failure.attempts,
        failure.max_attempts,
    ) == ("models/a.bin", "sync", policy, 1, 3)
    assert "credential-url-raw-reason" not in repr(recorder.events)
    assert "credential-url-raw-reason" not in failure_text


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
    ) as raised:
        process_runtime_file_downloads(
            RuntimeFilePlan(items=(item,)),
            config=_config(default="aria2", resume=True),
            backends={"aria2": backend},
            state_observer=lambda _item, status, **_: observed.append(status),
            event_sink=RecordingRuntimeEventSink(),
        )

    assert "fsync failed" not in str(raised.value)
    assert isinstance(raised.value.__cause__, DownloadFilesError)
    assert isinstance(raised.value.__cause__.__cause__, OSError)
    assert str(raised.value.__cause__.__cause__) == "fsync failed"
    assert observed == []
    assert backend.calls == []
    assert item.resume_authority == authority
    assert not staging.parent.exists()


def test_runtime_continue_cannot_mask_local_target_invariant(
    tmp_path: Path,
) -> None:
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
            event_sink=RecordingRuntimeEventSink(),
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
    recorder = RecordingRuntimeEventSink()

    results = process_runtime_file_downloads(
        plan,
        config=_config(),
        backends={"httpx": backend},
        state_observer=lambda item, status, **_: statuses.append(status),
        event_sink=recorder,
    )

    assert results == ()
    assert statuses == ["failed"]
    assert len(backend.calls) == 1
    assert len(recorder.closed_progress_scopes) == 1
    assert any(
        isinstance(event, RuntimeDownloadAttemptStarted) for event in recorder.events
    )
    assert not any(
        isinstance(event, (RuntimeDownloadFailed, RuntimeDownloadItemCompleted))
        for event in recorder.events
    )


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
            event_sink=RecordingRuntimeEventSink(),
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
            event_sink=RecordingRuntimeEventSink(),
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


def test_runtime_missing_backend_is_structured_terminal_failure(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path / "ComfyUI", _file("a.bin"))

    with pytest.raises(RuntimeFileDownloadError) as captured:
        process_runtime_file_downloads(
            plan,
            config=_config(),
            backends={},
            event_sink=RecordingRuntimeEventSink(),
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
        event_sink=RecordingRuntimeEventSink(),
    )
    aria2_results = download_runtime_files(
        _plan(root, _file("b.bin", downloader="aria2")),
        config=_config(),
        httpx_downloader=httpx,
        aria2_downloader_factory=factory,
        event_sink=RecordingRuntimeEventSink(),
    )

    assert httpx_results[0].backend == "httpx"
    assert aria2_results[0].backend == "aria2"
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
        event_sink=RecordingRuntimeEventSink(),
    )

    assert events == ["prepare", "startup", "download"]
