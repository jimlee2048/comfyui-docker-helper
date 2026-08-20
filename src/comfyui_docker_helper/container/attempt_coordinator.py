"""Single total-attempt policy shared by build and runtime downloads."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from comfyui_docker_helper.cli_output.events import EventSink
from comfyui_docker_helper.config.url_validation import DownloaderName
from comfyui_docker_helper.container.download_events import (
    DownloadAttemptStarted,
    DownloadEvent,
    DownloadRetryReason,
    DownloadRetryScheduled,
)
from comfyui_docker_helper.container.downloader_credentials import (
    DownloaderCredentialError,
)
from comfyui_docker_helper.container.transfer_core import (
    DownloadBackend,
    DownloadCancelled,
    DownloaderSettings,
    DownloadFilesError,
    FileTransferOutcome,
    FileTransferRequest,
    ResumeAuthority,
    ResumeRejectedDownloadFilesError,
    StagingDisposition,
    TerminalTransferDownloadFilesError,
    TransferDownloadFilesError,
    TransportOutcome,
    TransportRequest,
    discard_preserved_transfer,
    transfer_file,
)


class CancelRequested(Protocol):
    def __call__(self) -> bool: ...


class CancellableWait(Protocol):
    def __call__(self, timeout: float, cancel_requested: CancelRequested) -> bool: ...


class AttemptRetryObserver(Protocol):
    def __call__(self, attempt: int, error: TransferDownloadFilesError) -> None: ...


class AttemptStartObserver(Protocol):
    def __call__(self, attempt: int) -> None: ...


@dataclass(frozen=True, slots=True)
class AttemptSucceeded:
    attempts: int
    outcome: FileTransferOutcome


@dataclass(frozen=True, slots=True)
class AttemptOrdinaryTerminal:
    attempts: int
    error: TerminalTransferDownloadFilesError


@dataclass(frozen=True, slots=True)
class AttemptExhausted:
    attempts: int
    error: TransferDownloadFilesError
    resume_authority: ResumeAuthority | None = None


@dataclass(frozen=True, slots=True)
class AttemptCancelled:
    attempts: int
    resume_authority: ResumeAuthority | None = None


@dataclass(frozen=True, slots=True)
class AttemptLocalFailure:
    """A non-retryable local credential failure with truthful network count."""

    attempts: int
    error: DownloaderCredentialError


type AttemptResult = (
    AttemptSucceeded
    | AttemptOrdinaryTerminal
    | AttemptExhausted
    | AttemptCancelled
    | AttemptLocalFailure
)


class _OneCallBackend:
    """Count and enforce the one-adapter-call boundary of one core attempt."""

    def __init__(
        self,
        backend: DownloadBackend,
        *,
        before_call: Callable[[TransportRequest], None] | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self._backend = backend
        self._before_call = before_call
        self._on_call = on_call
        self.calls = 0

    def download(
        self,
        request: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportOutcome:
        if self.calls:
            raise DownloadFilesError(
                "one transfer-core attempt called its backend more than once"
            )
        try:
            if self._before_call is not None:
                self._before_call(request)
            if self._on_call is not None:
                self._on_call()
            self.calls += 1
            return self._backend.download(request, settings)
        except DownloaderCredentialError as error:
            self.calls = 1 if error.network_attempted else 0
            raise


def coordinate_transfer_attempts(
    request: FileTransferRequest,
    *,
    backend_name: DownloaderName,
    backend: DownloadBackend,
    settings: DownloaderSettings,
    max_attempts: int,
    cancel_requested: CancelRequested = lambda: False,
    wait: CancellableWait | None = None,
    backend_call_admission: Callable[[TransportRequest], None] | None = None,
    attempt_start_observer: AttemptStartObserver | None = None,
    retry_observer: AttemptRetryObserver | None = None,
    event_sink: EventSink[DownloadEvent] | None = None,
    continuation_owner: bool = False,
) -> AttemptResult:
    """Run exactly one total transport-attempt budget for one desired file."""
    if max_attempts < 1:
        raise DownloadFilesError("download attempt budget must be at least one")
    cancellable_wait = wait or _default_cancellable_wait
    attempts = 0
    allow_resume = backend_name == "aria2" and settings.aria2.resume_download
    resume_authority = request.resume_authority
    clean_fallback_pending = False
    if resume_authority is not None and not allow_resume:
        raise DownloadFilesError(
            "resume authority requires the resumable aria2 backend"
        )

    while attempts < max_attempts:
        if cancel_requested():
            if resume_authority is not None and not continuation_owner:
                _discard_resume(request, resume_authority)
                resume_authority = None
            return AttemptCancelled(
                attempts=attempts,
                resume_authority=resume_authority,
            )

        admission = replace(
            request,
            staging_disposition=(
                StagingDisposition.PRESERVE
                if resume_authority is not None
                else StagingDisposition.CLEAN
            ),
            resume_authority=resume_authority,
            preserve_on_retryable=allow_resume
            and (attempts + 1 < max_attempts or continuation_owner),
            preserve_on_cancellation=allow_resume and continuation_owner,
        )
        next_attempt = attempts + 1
        clean_fallback_attempt = clean_fallback_pending
        clean_fallback_pending = False

        def observe_attempt_start(attempt: int = next_attempt) -> None:
            if attempt_start_observer is not None:
                attempt_start_observer(attempt)
            if event_sink is not None:
                event_sink.emit(DownloadAttemptStarted(attempt))

        one_call = _OneCallBackend(
            backend,
            before_call=backend_call_admission,
            on_call=observe_attempt_start,
        )
        try:
            outcome = transfer_file(
                admission,
                backend=one_call,
                settings=settings,
                event_sink=event_sink,
            )
        except DownloaderCredentialError as error:
            attempts += one_call.calls
            return AttemptLocalFailure(attempts=attempts, error=error)
        except DownloadCancelled as error:
            attempts += one_call.calls
            return AttemptCancelled(
                attempts=attempts,
                resume_authority=error.resume_authority,
            )
        except ResumeRejectedDownloadFilesError as error:
            attempts += one_call.calls
            if one_call.calls != 1 or resume_authority is None or not allow_resume:
                raise DownloadFilesError(
                    "resume rejection did not consume one admitted resumed attempt"
                ) from error
            resume_authority = None
            allow_resume = False
            exhausted = TransferDownloadFilesError(
                str(error),
                reason=DownloadRetryReason.RESUME_REJECTED,
            )
            if attempts >= max_attempts:
                return AttemptExhausted(
                    attempts=attempts,
                    error=exhausted,
                )
            if retry_observer is not None:
                retry_observer(attempts, exhausted)
            if event_sink is not None:
                event_sink.emit(
                    DownloadRetryScheduled(
                        failed_attempt=attempts,
                        next_attempt=attempts + 1,
                        delay_seconds=0,
                        reason=DownloadRetryReason.RESUME_REJECTED,
                    )
                )
            # The server rejected this exact continuation capability. The next
            # counted attempt starts clean immediately and resume stays disabled.
            clean_fallback_pending = True
            continue
        except TerminalTransferDownloadFilesError as error:
            attempts += one_call.calls
            return AttemptOrdinaryTerminal(attempts=attempts, error=error)
        except TransferDownloadFilesError as error:
            attempts += one_call.calls
            if one_call.calls != 1:
                raise DownloadFilesError(
                    "retryable transfer result did not consume one backend call"
                ) from error
            if attempts >= max_attempts:
                return AttemptExhausted(
                    attempts=attempts,
                    error=error,
                    resume_authority=error.resume_authority,
                )
            resume_authority = error.resume_authority
            if resume_authority is not None and not allow_resume:
                raise DownloadFilesError(
                    "non-resumable backend returned resume authority"
                ) from error
            if clean_fallback_attempt:
                return AttemptExhausted(
                    attempts=attempts,
                    error=error,
                    resume_authority=resume_authority,
                )
            delay = _selected_delay(attempts, error.retry_after_seconds)
            try:
                if retry_observer is not None:
                    retry_observer(attempts, error)
                if event_sink is not None:
                    event_sink.emit(
                        DownloadRetryScheduled(
                            failed_attempt=attempts,
                            next_attempt=attempts + 1,
                            delay_seconds=delay,
                            reason=error.reason,
                            http_status=error.http_status,
                        )
                    )
                cancelled = cancellable_wait(delay, cancel_requested)
            except Exception:
                if resume_authority is not None and not continuation_owner:
                    _discard_resume(request, resume_authority)
                raise
            if cancelled or cancel_requested():
                if resume_authority is not None and not continuation_owner:
                    _discard_resume(request, resume_authority)
                    resume_authority = None
                return AttemptCancelled(
                    attempts=attempts,
                    resume_authority=resume_authority,
                )
            continue

        attempts += one_call.calls
        if one_call.calls == 0:
            return AttemptSucceeded(attempts=attempts, outcome=outcome)
        if one_call.calls != 1:
            raise DownloadFilesError(
                "successful transfer attempt did not call its backend exactly once"
            )
        return AttemptSucceeded(attempts=attempts, outcome=outcome)

    raise AssertionError("transfer attempt coordinator produced no result")


def _discard_resume(
    request: FileTransferRequest,
    authority: ResumeAuthority,
) -> None:
    discard_preserved_transfer(
        replace(
            request,
            staging_disposition=StagingDisposition.PRESERVE,
            resume_authority=authority,
            preserve_on_retryable=False,
        )
    )


def _selected_delay(attempt: int, retry_after_seconds: float | None) -> float:
    deterministic = min(float(2 ** (attempt - 1)), 30.0)
    if retry_after_seconds is None:
        return deterministic
    return min(max(deterministic, retry_after_seconds), 30.0)


def _default_cancellable_wait(
    timeout: float,
    cancel_requested: CancelRequested,
) -> bool:
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while True:
        if cancel_requested():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return cancel_requested()
        wake.wait(min(remaining, 0.05))
