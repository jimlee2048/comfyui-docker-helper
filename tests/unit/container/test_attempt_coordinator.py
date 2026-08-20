"""Total-attempt, delay, cancellation, and resume policy tests."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from comfyui_docker_helper.container import transfer_core
from comfyui_docker_helper.container.attempt_coordinator import (
    AttemptCancelled,
    AttemptExhausted,
    AttemptOrdinaryTerminal,
    AttemptSucceeded,
    coordinate_transfer_attempts,
)
from comfyui_docker_helper.container.download_events import (
    DownloadAttemptStarted,
    DownloadEvent,
    DownloadPlacementCompleted,
    DownloadPlacementStarted,
    DownloadRetryReason,
    DownloadRetryScheduled,
    DownloadTransferProgress,
    DownloadVerificationCompleted,
    DownloadVerificationStarted,
)
from comfyui_docker_helper.container.transfer_core import (
    Aria2DownloadSettings,
    DownloaderSettings,
    DownloadFilesError,
    FileTransferRequest,
    HttpxDownloadSettings,
    StagingDisposition,
    TransportCancelled,
    TransportDiagnostic,
    TransportOrdinaryTerminal,
    TransportOutcome,
    TransportRequest,
    TransportResumeRejected,
    TransportRetryable,
    TransportSuccess,
    transfer_staging_target,
)


class ScriptedBackend:
    """Return one semantic outcome per real adapter call."""

    def __init__(
        self,
        outcomes: list[TransportOutcome],
        *,
        writer: Callable[[TransportRequest, int], int] | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.writer = writer or self._clean_writer
        self.calls = 0
        self.resume_allowed: list[bool] = []

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        del settings
        index = self.calls
        self.calls += 1
        self.resume_allowed.append(request.sink.resume_allowed)
        length = self.writer(request, index)
        outcome = self.outcomes[index]
        if (
            isinstance(
                outcome,
                (TransportRetryable, TransportCancelled, TransportResumeRejected),
            )
            and outcome.diagnostic.namespace == "aria2"
        ):
            control = Path(f"{request.sink.display_path}.aria2")
            temp = Path(f"{control}__temp")
            temp.write_bytes(f"aria2-control-{index}".encode())
            os.replace(temp, control)
        if isinstance(outcome, TransportSuccess):
            return TransportSuccess(
                length=length,
                namespace=outcome.namespace,
                http_status=outcome.http_status,
            )
        return outcome

    @staticmethod
    def _clean_writer(request: TransportRequest, index: int) -> int:
        content = f"attempt-{index + 1}".encode()
        with request.sink.open_for_write() as output:
            output.write(content)
        return len(content)


class RecordingEventSink:
    """Record typed download events without adding presentation behavior."""

    def __init__(self) -> None:
        self.events: list[DownloadEvent] = []

    def emit(self, event: DownloadEvent, /) -> None:
        self.events.append(event)


def _settings(*, resume: bool = False) -> DownloaderSettings:
    return DownloaderSettings(
        default="aria2" if resume else "httpx",
        aria2=Aria2DownloadSettings(
            rpc_port=6800,
            split=16,
            max_connection_per_server=16,
            min_split_size="1M",
            resume_download=resume,
        ),
        httpx=HttpxDownloadSettings(timeout=60),
    )


def _request(root: Path) -> FileTransferRequest:
    root.mkdir()
    return FileTransferRequest(
        root=root,
        url="https://example.test/model.bin",
        target=root / "models" / "model.bin",
        overwrite=True,
        expected_checksum=None,
        staging_disposition=StagingDisposition.CLEAN,
    )


def _retryable(*, retry_after: float | None = None) -> TransportRetryable:
    return TransportRetryable(
        TransportDiagnostic("httpx", "temporary failure"),
        http_status=503 if retry_after is not None else None,
        retry_after_seconds=retry_after,
        reason=DownloadRetryReason.TEMPORARY_SERVER,
    )


def _success() -> TransportSuccess:
    return TransportSuccess(length=0, namespace="httpx", http_status=200)


# Attempt budgets count transport dispatches, stop on terminal outcomes, and
# bound retry delays without retrying local failures or completed targets.
def test_total_budget_counts_only_backend_calls_and_uses_one_two_backoff(
    tmp_path: Path,
) -> None:
    backend = ScriptedBackend([_retryable(), _retryable(), _success()])
    waits: list[float] = []
    events = RecordingEventSink()

    result = coordinate_transfer_attempts(
        _request(tmp_path / "ComfyUI"),
        backend_name="httpx",
        backend=backend,
        settings=_settings(),
        max_attempts=3,
        wait=lambda delay, _: waits.append(delay) or False,
        event_sink=events,
    )

    assert isinstance(result, AttemptSucceeded)
    assert result.attempts == backend.calls == 3
    assert waits == [1.0, 2.0]
    assert events.events == [
        DownloadAttemptStarted(1),
        DownloadRetryScheduled(
            failed_attempt=1,
            next_attempt=2,
            delay_seconds=1.0,
            reason=DownloadRetryReason.TEMPORARY_SERVER,
        ),
        DownloadAttemptStarted(2),
        DownloadRetryScheduled(
            failed_attempt=2,
            next_attempt=3,
            delay_seconds=2.0,
            reason=DownloadRetryReason.TEMPORARY_SERVER,
        ),
        DownloadAttemptStarted(3),
        DownloadVerificationStarted(),
        DownloadVerificationCompleted(),
        DownloadPlacementStarted(),
        DownloadPlacementCompleted(),
    ]
    assert result.outcome.target.read_bytes() == b"attempt-3"


def test_event_sink_is_projected_as_narrow_adapter_progress_sink(
    tmp_path: Path,
) -> None:
    events = RecordingEventSink()

    class ProgressBackend:
        def download(self, request: TransportRequest, settings: DownloaderSettings):
            del settings
            assert request.progress_sink is events
            with request.sink.open_for_write() as output:
                output.write(b"data")
            request.progress_sink.emit(
                DownloadTransferProgress(
                    transferred_bytes=4,
                    total_bytes=4,
                    stored_bytes=4,
                    reported_rate=None,
                )
            )
            return TransportSuccess(length=4, namespace="httpx", http_status=200)

    result = coordinate_transfer_attempts(
        _request(tmp_path / "ComfyUI"),
        backend_name="httpx",
        backend=ProgressBackend(),
        settings=_settings(),
        max_attempts=1,
        event_sink=events,
    )

    assert isinstance(result, AttemptSucceeded)
    assert events.events == [
        DownloadAttemptStarted(1),
        DownloadTransferProgress(
            transferred_bytes=4,
            total_bytes=4,
            stored_bytes=4,
            reported_rate=None,
        ),
        DownloadVerificationStarted(),
        DownloadVerificationCompleted(),
        DownloadPlacementStarted(),
        DownloadPlacementCompleted(),
    ]


def test_terminal_stops_without_spending_remaining_budget(tmp_path: Path) -> None:
    backend = ScriptedBackend(
        [
            _retryable(),
            TransportOrdinaryTerminal(
                TransportDiagnostic("httpx", "not found"), http_status=404
            ),
            _success(),
        ]
    )
    waits: list[float] = []

    result = coordinate_transfer_attempts(
        _request(tmp_path / "ComfyUI"),
        backend_name="httpx",
        backend=backend,
        settings=_settings(),
        max_attempts=3,
        wait=lambda delay, _: waits.append(delay) or False,
    )

    assert isinstance(result, AttemptOrdinaryTerminal)
    assert result.attempts == backend.calls == 2
    assert waits == [1.0]


def test_one_attempt_exhausts_without_wait(tmp_path: Path) -> None:
    backend = ScriptedBackend([_retryable()])
    waits: list[float] = []
    events = RecordingEventSink()

    result = coordinate_transfer_attempts(
        _request(tmp_path / "ComfyUI"),
        backend_name="httpx",
        backend=backend,
        settings=_settings(),
        max_attempts=1,
        wait=lambda delay, _: waits.append(delay) or False,
        event_sink=events,
    )

    assert isinstance(result, AttemptExhausted)
    assert result.attempts == backend.calls == 1
    assert waits == []
    assert events.events == [DownloadAttemptStarted(1)]


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [(None, 1.0), (0.0, 1.0), (10.0, 10.0), (90.0, 30.0)],
)
def test_retry_after_cannot_shorten_or_exceed_private_backoff(
    tmp_path: Path,
    retry_after: float | None,
    expected: float,
) -> None:
    backend = ScriptedBackend([_retryable(retry_after=retry_after), _success()])
    waits: list[float] = []
    events = RecordingEventSink()

    coordinate_transfer_attempts(
        _request(tmp_path / "ComfyUI"),
        backend_name="httpx",
        backend=backend,
        settings=_settings(),
        max_attempts=2,
        wait=lambda delay, _: waits.append(delay) or False,
        event_sink=events,
    )

    assert waits == [expected]
    retry = next(
        event for event in events.events if isinstance(event, DownloadRetryScheduled)
    )
    assert retry.reason is DownloadRetryReason.TEMPORARY_SERVER
    assert retry.http_status == (503 if retry_after is not None else None)


def test_cancellation_during_wait_prevents_following_call(tmp_path: Path) -> None:
    backend = ScriptedBackend([_retryable(), _success()])

    result = coordinate_transfer_attempts(
        _request(tmp_path / "ComfyUI"),
        backend_name="httpx",
        backend=backend,
        settings=_settings(),
        max_attempts=2,
        wait=lambda _delay, _cancel: True,
    )

    assert isinstance(result, AttemptCancelled)
    assert result.attempts == backend.calls == 1


def test_local_failure_is_not_policy_eligible(tmp_path: Path) -> None:
    class BrokenBackend:
        def download(self, request, settings):
            del request, settings
            raise DownloadFilesError("local invariant failed")

    with pytest.raises(DownloadFilesError, match="local invariant"):
        coordinate_transfer_attempts(
            _request(tmp_path / "ComfyUI"),
            backend_name="httpx",
            backend=BrokenBackend(),
            settings=_settings(),
            max_attempts=3,
            wait=lambda _delay, _cancel: False,
        )


def test_existing_target_skip_consumes_zero_transport_attempts(tmp_path: Path) -> None:
    request = _request(tmp_path / "ComfyUI")
    request.target.parent.mkdir()
    request.target.write_bytes(b"existing")
    request = FileTransferRequest(
        root=request.root,
        url=request.url,
        target=request.target,
        overwrite=False,
        expected_checksum=None,
        staging_disposition=StagingDisposition.CLEAN,
    )
    backend = ScriptedBackend([_success()])
    events = RecordingEventSink()

    result = coordinate_transfer_attempts(
        request,
        backend_name="httpx",
        backend=backend,
        settings=_settings(),
        max_attempts=3,
        wait=lambda _delay, _cancel: False,
        event_sink=events,
    )

    assert isinstance(result, AttemptSucceeded)
    assert result.attempts == backend.calls == 0
    assert events.events == []


# Resumable retries may reuse only the exact staging artifact whose identity
# remains under the coordinator's authority.
def test_aria2_resume_reuses_only_authority_proven_partial(tmp_path: Path) -> None:
    request = _request(tmp_path / "ComfyUI")

    def aria_writer(transport: TransportRequest, index: int) -> int:
        if index == 0:
            with transport.sink.open_for_write() as output:
                output.write(b"partial-")
        else:
            with transport.sink.display_path.open("ab") as output:
                output.write(b"complete")
        return transport.sink.current_length()

    backend = ScriptedBackend(
        [
            TransportRetryable(TransportDiagnostic("aria2", "timeout")),
            TransportSuccess(length=0, namespace="aria2"),
        ],
        writer=aria_writer,
    )

    result = coordinate_transfer_attempts(
        request,
        backend_name="aria2",
        backend=backend,
        settings=_settings(resume=True),
        max_attempts=2,
        wait=lambda _delay, _cancel: False,
    )

    assert isinstance(result, AttemptSucceeded)
    assert backend.resume_allowed == [False, True]
    assert request.target.read_bytes() == b"partial-complete"


def test_cancelled_aria2_backoff_discards_exact_preserved_partial(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path / "ComfyUI")
    backend = ScriptedBackend(
        [TransportRetryable(TransportDiagnostic("aria2", "timeout")), _success()]
    )

    result = coordinate_transfer_attempts(
        request,
        backend_name="aria2",
        backend=backend,
        settings=_settings(resume=True),
        max_attempts=2,
        wait=lambda _delay, _cancel: True,
    )

    assert isinstance(result, AttemptCancelled)
    assert not transfer_staging_target(request).exists()


def test_aria2_resume_authority_drift_fails_closed_without_touching_foreign_leaf(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path / "ComfyUI")
    staging = transfer_staging_target(request)
    backend = ScriptedBackend(
        [TransportRetryable(TransportDiagnostic("aria2", "timeout")), _success()]
    )

    def replace_before_resume(
        _delay: float,
        _cancel_requested,
    ) -> bool:
        replacement = staging.with_name(f"{staging.name}.foreign")
        replacement.write_bytes(b"foreign")
        os.replace(replacement, staging)
        return False

    with pytest.raises(DownloadFilesError, match="does not match authority"):
        coordinate_transfer_attempts(
            request,
            backend_name="aria2",
            backend=backend,
            settings=_settings(resume=True),
            max_attempts=2,
            wait=replace_before_resume,
        )

    assert backend.calls == 1
    assert staging.read_bytes() == b"foreign"


# A rejected resume permits one counted clean fallback while cancellation,
# ownership drift, durability failure, and exhausted budgets remain fail-closed.
def test_resume_rejection_counts_and_uses_one_immediate_clean_fallback(
    tmp_path: Path,
) -> None:
    """A rejected resumed call is counted, cleaned, and followed once without wait."""
    request = _request(tmp_path / "ComfyUI")

    def aria_writer(transport: TransportRequest, index: int) -> int:
        if index == 0:
            with transport.sink.open_for_write() as output:
                output.write(b"partial")
        elif index == 2:
            assert not transport.sink.resume_allowed
            with transport.sink.open_for_write() as output:
                output.write(b"clean-success")
        return transport.sink.current_length()

    backend = ScriptedBackend(
        [
            TransportRetryable(TransportDiagnostic("aria2", "timeout")),
            TransportResumeRejected(TransportDiagnostic("aria2", "resume rejected")),
            TransportSuccess(length=0, namespace="aria2"),
        ],
        writer=aria_writer,
    )
    waits: list[float] = []
    starts: list[int] = []
    failed_attempts: list[int] = []
    events = RecordingEventSink()

    result = coordinate_transfer_attempts(
        request,
        backend_name="aria2",
        backend=backend,
        settings=_settings(resume=True),
        max_attempts=3,
        wait=lambda delay, _: waits.append(delay) or False,
        attempt_start_observer=starts.append,
        retry_observer=lambda attempt, _error: failed_attempts.append(attempt),
        event_sink=events,
    )

    assert isinstance(result, AttemptSucceeded)
    assert result.attempts == backend.calls == 3
    assert backend.resume_allowed == [False, True, False]
    assert starts == [1, 2, 3]
    assert failed_attempts == [1, 2]
    assert waits == [1.0]
    retries = [
        event for event in events.events if isinstance(event, DownloadRetryScheduled)
    ]
    assert retries == [
        DownloadRetryScheduled(
            failed_attempt=1,
            next_attempt=2,
            delay_seconds=1.0,
            reason=DownloadRetryReason.UNKNOWN,
        ),
        DownloadRetryScheduled(
            failed_attempt=2,
            next_attempt=3,
            delay_seconds=0,
            reason=DownloadRetryReason.RESUME_REJECTED,
        ),
    ]
    assert request.target.read_bytes() == b"clean-success"
    assert not transfer_staging_target(request).exists()
    assert not Path(f"{transfer_staging_target(request)}.aria2").exists()


def test_resume_rejection_exhausts_two_attempt_budget_without_fallback(
    tmp_path: Path,
) -> None:
    """No clean request is granted when the counted rejection spends the budget."""
    request = _request(tmp_path / "ComfyUI")
    backend = ScriptedBackend(
        [
            TransportRetryable(TransportDiagnostic("aria2", "timeout")),
            TransportResumeRejected(TransportDiagnostic("aria2", "resume rejected")),
        ]
    )

    result = coordinate_transfer_attempts(
        request,
        backend_name="aria2",
        backend=backend,
        settings=_settings(resume=True),
        max_attempts=2,
        wait=lambda _delay, _cancel: False,
    )

    assert isinstance(result, AttemptExhausted)
    assert result.attempts == backend.calls == 2
    assert result.resume_authority is None
    assert not transfer_staging_target(request).exists()


def test_cancellation_after_resume_rejection_prevents_clean_fallback(
    tmp_path: Path,
) -> None:
    """Cancellation at the fallback boundary cannot start an extra clean request."""
    request = _request(tmp_path / "ComfyUI")
    backend = ScriptedBackend(
        [
            TransportRetryable(TransportDiagnostic("aria2", "timeout")),
            TransportResumeRejected(TransportDiagnostic("aria2", "resume rejected")),
            _success(),
        ]
    )

    result = coordinate_transfer_attempts(
        request,
        backend_name="aria2",
        backend=backend,
        settings=_settings(resume=True),
        max_attempts=3,
        cancel_requested=lambda: backend.calls >= 2,
        wait=lambda _delay, _cancel: False,
    )

    assert isinstance(result, AttemptCancelled)
    assert result.attempts == backend.calls == 2
    assert not transfer_staging_target(request).exists()


def test_resume_rejection_cleanup_drift_fails_closed_and_preserves_foreign_leaf(
    tmp_path: Path,
) -> None:
    """A raced partial is never deleted or converted into a clean fallback."""
    request = _request(tmp_path / "ComfyUI")
    staging = transfer_staging_target(request)

    def race_writer(transport: TransportRequest, index: int) -> int:
        if index == 0:
            with transport.sink.open_for_write() as output:
                output.write(b"partial")
        else:
            staging.unlink()
            staging.write_bytes(b"foreign")
        return len(b"partial")

    backend = ScriptedBackend(
        [
            TransportRetryable(TransportDiagnostic("aria2", "timeout")),
            TransportResumeRejected(TransportDiagnostic("aria2", "resume rejected")),
        ],
        writer=race_writer,
    )

    with pytest.raises(DownloadFilesError, match="artifact identity changed"):
        coordinate_transfer_attempts(
            request,
            backend_name="aria2",
            backend=backend,
            settings=_settings(resume=True),
            max_attempts=3,
            wait=lambda _delay, _cancel: False,
        )

    assert backend.calls == 2
    assert staging.read_bytes() == b"foreign"


def test_resume_rejection_cleanup_durability_failure_prevents_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup that cannot be made durable is local failure, not a retry event."""
    request = _request(tmp_path / "ComfyUI")
    rejection_observed = False

    def writer(transport: TransportRequest, index: int) -> int:
        nonlocal rejection_observed
        if index == 0:
            with transport.sink.open_for_write() as output:
                output.write(b"partial")
        else:
            rejection_observed = True
        return transport.sink.current_length()

    backend = ScriptedBackend(
        [
            TransportRetryable(TransportDiagnostic("aria2", "timeout")),
            TransportResumeRejected(TransportDiagnostic("aria2", "resume rejected")),
            _success(),
        ],
        writer=writer,
    )
    real_fsync = transfer_core.os.fsync

    def fail_rejection_cleanup_fsync(fd: int) -> None:
        if rejection_observed:
            raise OSError("injected durability failure")
        real_fsync(fd)

    monkeypatch.setattr(transfer_core.os, "fsync", fail_rejection_cleanup_fsync)

    with pytest.raises(DownloadFilesError, match="could not be made durable"):
        coordinate_transfer_attempts(
            request,
            backend_name="aria2",
            backend=backend,
            settings=_settings(resume=True),
            max_attempts=3,
            wait=lambda _delay, _cancel: False,
        )

    assert backend.calls == 2
    assert not transfer_staging_target(request).exists()


def test_rejected_resume_allows_only_one_clean_fallback_call(tmp_path: Path) -> None:
    """A retryable clean fallback exhausts immediately without another wait/call."""
    request = _request(tmp_path / "ComfyUI")
    backend = ScriptedBackend(
        [
            TransportRetryable(TransportDiagnostic("aria2", "timeout")),
            TransportResumeRejected(TransportDiagnostic("aria2", "resume rejected")),
            TransportRetryable(TransportDiagnostic("aria2", "clean timeout")),
            TransportSuccess(length=0, namespace="aria2"),
        ]
    )
    waits: list[float] = []

    result = coordinate_transfer_attempts(
        request,
        backend_name="aria2",
        backend=backend,
        settings=_settings(resume=True),
        max_attempts=4,
        wait=lambda delay, _cancel: waits.append(delay) or False,
    )

    assert isinstance(result, AttemptExhausted)
    assert result.attempts == backend.calls == 3
    assert waits == [1.0]
    assert not transfer_staging_target(request).exists()


# Observer or wait failures cannot invent attempts or strand resumable
# data without a continuation owner.
def test_start_observer_failure_does_not_count_or_call_adapter(tmp_path: Path) -> None:
    """An observer failure before dispatch cannot manufacture a transport attempt."""
    request = _request(tmp_path / "ComfyUI")
    backend = ScriptedBackend([_success()])

    def fail_start(_attempt: int) -> None:
        raise RuntimeError("start observer failed")

    with pytest.raises(RuntimeError, match="start observer failed"):
        coordinate_transfer_attempts(
            request,
            backend_name="httpx",
            backend=backend,
            settings=_settings(),
            max_attempts=2,
            attempt_start_observer=fail_start,
        )

    assert backend.calls == 0
    assert not transfer_staging_target(request).exists()


@pytest.mark.parametrize("failure_point", ["retry_observer", "event_sink", "wait"])
def test_retry_side_effect_failure_discards_unowned_resume_authority(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """Retry-side failures cannot strand resumable data without a continuation owner."""
    request = _request(tmp_path / "ComfyUI")
    backend = ScriptedBackend(
        [TransportRetryable(TransportDiagnostic("aria2", "timeout")), _success()]
    )

    def fail(name: str):
        def raise_failure(*_args):
            if failure_point == name:
                raise RuntimeError(f"{name} failed")
            return False

        return raise_failure

    class FailingRetryEventSink:
        def emit(self, event: DownloadEvent, /) -> None:
            if failure_point == "event_sink" and isinstance(
                event, DownloadRetryScheduled
            ):
                raise RuntimeError("event_sink failed")

    with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
        coordinate_transfer_attempts(
            request,
            backend_name="aria2",
            backend=backend,
            settings=_settings(resume=True),
            max_attempts=2,
            retry_observer=fail("retry_observer"),
            event_sink=FailingRetryEventSink(),
            wait=fail("wait"),
        )

    assert backend.calls == 1
    assert not transfer_staging_target(request).exists()
    assert not Path(f"{transfer_staging_target(request)}.aria2").exists()
