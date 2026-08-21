"""Runtime SSH service lifetime ownership."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Literal, Protocol, runtime_checkable

from comfyui_docker_helper.cli_output import EventSink
from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container.process_control import (
    DirectProcess,
    reap_process_terminal,
    request_force_direct_process,
    request_terminate_direct_process,
    terminate_direct_process_until,
)
from comfyui_docker_helper.container.runtime_event_delivery import (
    RuntimeBackgroundEventSink,
    safe_runtime_event_sink,
)
from comfyui_docker_helper.container.runtime_events import (
    RuntimeEvent,
    RuntimeSshWarning,
    RuntimeSshWarningKind,
)
from comfyui_docker_helper.container.ssh import (
    SshCredentialPreparationError,
    SshdConfigPreparationError,
    SshdConfigValidationError,
    SshdProcess,
    SshdReadinessError,
    SshdStartupError,
    SshEnvironmentProjectionError,
    SshPreparationWarningKind,
    start_sshd_if_enabled,
)
from comfyui_docker_helper.errors import ApplicationError

SSHD_STOP_TIMEOUT_SECONDS = 5.0
SSHD_STOP_POLL_INTERVAL_SECONDS = 0.05

_SSH_ENVIRONMENT_PROJECTION_FAILURE = "SSH environment projection failed"
_SSH_CONFIG_PREPARATION_FAILURE = "SSH protected configuration preparation failed"
_SSH_CONFIG_VALIDATION_FAILURE = "SSH configuration parser validation failed"
_SSH_CREDENTIAL_PREPARATION_FAILURE = "SSH credential preparation failed"
_SSH_STARTUP_READINESS_FAILURE = "SSH startup/readiness failed"
_SSH_EARLY_EXIT_FAILURE = "SSH runtime service exited before ComfyUI"


class RuntimeSshStarter(Protocol):
    """Start the configured runtime SSH service."""

    def __call__(
        self,
        config: RuntimeConfig,
        *,
        environment: Mapping[bytes, bytes],
        cancel_requested: Callable[[], bool],
        preparation_process_observer: Callable[[DirectProcess | None], None],
        preparation_warning_observer: Callable[[SshPreparationWarningKind], object],
    ) -> SshdProcess | None: ...


@runtime_checkable
class ForceEscalationCancellation(Protocol):
    """Cancellation source that exposes repeated-signal force escalation."""

    def force_requested(self) -> bool: ...


@runtime_checkable
class DeadlineBoundCancellation(Protocol):
    """Cancellation source that exposes the owning absolute deadline."""

    def shutdown_deadline(self) -> float | None: ...


@runtime_checkable
class WakeableForceCancellation(Protocol):
    """Cancellation source that wakes when force escalation is requested."""

    def wait_for_force(self, timeout: float) -> object: ...


class RuntimeSshServiceError(ApplicationError):
    """The runtime SSH service failed at a lifecycle boundary."""


def _contains_ssh_error(
    error: BaseException,
    expected: type[BaseException],
) -> bool:
    """Find one typed SSH failure without formatting its private detail."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, expected):
            return True
        current = current.__cause__ or current.__context__
    return False


def _runtime_ssh_failure_message(error: BaseException) -> str:
    """Project typed SSH activation failures into fixed safe categories."""
    if _contains_ssh_error(error, SshEnvironmentProjectionError):
        return _SSH_ENVIRONMENT_PROJECTION_FAILURE
    if _contains_ssh_error(error, SshdConfigPreparationError):
        return _SSH_CONFIG_PREPARATION_FAILURE
    if _contains_ssh_error(error, SshdConfigValidationError):
        return _SSH_CONFIG_VALIDATION_FAILURE
    if _contains_ssh_error(error, SshCredentialPreparationError):
        return _SSH_CREDENTIAL_PREPARATION_FAILURE
    if _contains_ssh_error(error, SshdReadinessError):
        return _SSH_STARTUP_READINESS_FAILURE
    return _SSH_STARTUP_READINESS_FAILURE


def _force_requested(cancel_requested: Callable[[], bool]) -> bool:
    return (
        isinstance(cancel_requested, ForceEscalationCancellation)
        and cancel_requested.force_requested()
    )


def _startup_cancellation_deadline(
    cancel_requested: Callable[[], bool],
    *,
    monotonic: Callable[[], float],
) -> float:
    deadline = monotonic() + SSHD_STOP_TIMEOUT_SECONDS
    if isinstance(cancel_requested, DeadlineBoundCancellation):
        outer_deadline = cancel_requested.shutdown_deadline()
        if outer_deadline is not None:
            deadline = min(deadline, outer_deadline)
    return deadline


class RuntimeSshService:
    """Own sshd activation, health monitoring, and bounded shutdown."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        environment: Mapping[bytes, bytes],
        background_event_sink: RuntimeBackgroundEventSink,
        event_sink: EventSink[RuntimeEvent],
        starter: RuntimeSshStarter = start_sshd_if_enabled,
    ) -> None:
        self._config = config
        self._environment = environment
        self._starter = starter
        self._background_event_sink = background_event_sink
        self._event_sink = safe_runtime_event_sink(event_sink)
        self._reported_direct_warnings: set[RuntimeSshWarningKind] = set()
        self._handle: SshdProcess | None = None
        self._shutdown_requested = threading.Event()
        self._startup_thread: threading.Thread | None = None
        self._startup_process: DirectProcess | None = None
        self._startup_lock = threading.Lock()
        self._handle_reaped = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def start(
        self,
        *,
        cancel_requested: Callable[[], bool] = lambda: False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Run SSH preparation as one published, cancellable startup operation."""
        if not self._config.system.ssh.enable:
            return
        result: list[SshdProcess | None] = []
        errors: list[BaseException] = []
        preparation_warnings: list[SshPreparationWarningKind] = []

        def observe_preparation_process(process: DirectProcess | None) -> None:
            with self._startup_lock:
                self._startup_process = process
                cancellation_requested = self._shutdown_requested.is_set()
            if process is not None and cancellation_requested:
                try:
                    request_terminate_direct_process(process)
                except Exception:
                    self._emit_background_warning(
                        RuntimeSshWarningKind.STARTUP_TERMINATION_FAILED,
                    )

        def start() -> None:
            try:
                handle = self._starter(
                    self._config,
                    environment=self._environment,
                    cancel_requested=self._shutdown_requested.is_set,
                    preparation_process_observer=observe_preparation_process,
                    preparation_warning_observer=preparation_warnings.append,
                )
                result.append(handle)
            except BaseException as error:
                errors.append(error)
            finally:
                observe_preparation_process(None)

        thread = threading.Thread(
            target=start,
            name="cdh-ssh-startup",
            daemon=True,
        )
        self._startup_thread = thread
        thread.start()
        cancellation_deadline: float | None = None
        forced = False
        while thread.is_alive():
            if cancel_requested():
                if cancellation_deadline is None:
                    cancellation_deadline = _startup_cancellation_deadline(
                        cancel_requested,
                        monotonic=monotonic,
                    )
                self.request_stop()
                now = monotonic()
                if _force_requested(cancel_requested) or now >= cancellation_deadline:
                    self.request_force_stop()
                    forced = True
                delay = SSHD_STOP_POLL_INTERVAL_SECONDS
                if not forced:
                    delay = min(delay, max(0.0, cancellation_deadline - now))
                if isinstance(cancel_requested, WakeableForceCancellation):
                    cancel_requested.wait_for_force(delay)
                else:
                    thread.join(timeout=delay)
            else:
                thread.join(timeout=SSHD_STOP_POLL_INTERVAL_SECONDS)
        for warning in preparation_warnings:
            self._emit_direct_warning(RuntimeSshWarningKind(warning.value))
        if cancel_requested():
            if result and result[0] is not None:
                self._handle = result[0]
                self.request_force_stop()
                while not self.is_stopped():
                    if isinstance(cancel_requested, WakeableForceCancellation):
                        cancel_requested.wait_for_force(SSHD_STOP_POLL_INTERVAL_SECONDS)
                    else:
                        time.sleep(SSHD_STOP_POLL_INTERVAL_SECONDS)
            return
        if errors:
            error = errors[0]
            if isinstance(error, (SshdStartupError, ApplicationError)):
                raise RuntimeSshServiceError(
                    _runtime_ssh_failure_message(error)
                ) from error
            raise error
        if len(result) != 1:
            raise RuntimeSshServiceError(_SSH_STARTUP_READINESS_FAILURE)
        self._handle = result[0]

    def ensure_running_before_comfyui(self) -> None:
        """Reject startup if an activated sshd has already exited."""
        if self._handle is None:
            return
        returncode = self._handle.poll()
        if returncode is not None:
            raise RuntimeSshServiceError(_SSH_EARLY_EXIT_FAILURE)

    def startup_outcome(
        self,
    ) -> Literal["disabled", "enabled-without-credentials", "ready"]:
        """Return the controlled outcome after successful startup validation."""
        if not self._config.system.ssh.enable:
            return "disabled"
        if self._handle is None:
            return "enabled-without-credentials"
        return "ready"

    def monitor_after_comfyui_start(self) -> None:
        """Report unexpected sshd exit without changing ComfyUI ownership."""
        if self._handle is None:
            return

        def wait_for_exit() -> None:
            assert self._handle is not None
            try:
                returncode = self._handle.wait()
            except Exception:
                if self._shutdown_requested.is_set():
                    return
                self._emit_background_warning(
                    RuntimeSshWarningKind.MONITOR_FAILED,
                )
                return
            if self._shutdown_requested.is_set():
                self._handle_reaped.set()
                return
            self._emit_background_warning(
                RuntimeSshWarningKind.EXITED_UNEXPECTEDLY,
                returncode=returncode,
            )
            self._handle_reaped.set()

        thread = threading.Thread(
            target=wait_for_exit,
            name="cdh-sshd-monitor",
            daemon=True,
        )
        self._monitor_thread = thread
        thread.start()

    def stop(
        self,
        *,
        cancel_requested: Callable[[], bool],
        timeout: float = SSHD_STOP_TIMEOUT_SECONDS,
        poll_interval: float = SSHD_STOP_POLL_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = time.sleep,
    ) -> bool:
        """Terminate, bound, and reap the owned sshd child."""
        deadline = monotonic() + timeout
        stopped = stop_runtime_ssh_service(
            self._handle,
            cancel_requested=cancel_requested,
            shutdown_requested=self._shutdown_requested,
            timeout=timeout,
            poll_interval=poll_interval,
            monotonic=monotonic,
            sleep=sleep,
            warning_observer=self._emit_direct_warning,
        )
        if stopped:
            self._handle_reaped.set()
        monitor_stopped = self._wait_for_monitor(
            deadline=deadline,
            monotonic=monotonic,
        )
        return stopped and monitor_stopped

    def request_stop(self) -> None:
        """Promptly start cooperative cancellation without waiting."""
        self._shutdown_requested.set()
        self._signal_startup_process(force=False)
        handle = self._handle
        if handle is None or handle.poll() is not None:
            return
        try:
            request_terminate_direct_process(handle)
        except Exception:
            self._emit_direct_warning(RuntimeSshWarningKind.SERVICE_TERMINATION_FAILED)

    def is_stopped(self) -> bool:
        """Reap and report all owned startup and sshd operations."""
        startup = self._startup_thread
        if startup is not None:
            startup.join(timeout=0.0)
            if startup.is_alive():
                return False
        handle = self._handle
        if handle is None:
            return self._monitor_is_stopped()
        if self._handle_reaped.is_set():
            return self._monitor_is_stopped()
        try:
            terminal = reap_process_terminal(handle)
        except Exception:
            self._emit_direct_warning(RuntimeSshWarningKind.SERVICE_REAP_FAILED)
            return False
        if terminal is not None:
            self._handle_reaped.set()
            return self._monitor_is_stopped()
        return False

    def request_force_stop(self) -> None:
        """Request immediate force for every currently owned SSH process."""
        self._shutdown_requested.set()
        self._signal_startup_process(force=True)
        handle = self._handle
        if handle is not None:
            request_force_direct_process(handle)

    def _signal_startup_process(self, *, force: bool) -> None:
        with self._startup_lock:
            process = self._startup_process
        if process is None:
            return
        try:
            if force:
                request_force_direct_process(process)
            else:
                request_terminate_direct_process(process)
        except Exception:
            self._emit_direct_warning(
                RuntimeSshWarningKind.STARTUP_PROCESS_SIGNAL_FAILED
            )

    def _monitor_is_stopped(self) -> bool:
        monitor = self._monitor_thread
        if monitor is None:
            return True
        monitor.join(timeout=0.0)
        return not monitor.is_alive()

    def _wait_for_monitor(
        self,
        *,
        deadline: float,
        monotonic: Callable[[], float],
    ) -> bool:
        monitor = self._monitor_thread
        if monitor is None:
            return True
        monitor.join(timeout=max(0.0, deadline - monotonic()))
        return not monitor.is_alive()

    def _emit_background_warning(
        self,
        kind: RuntimeSshWarningKind,
        *,
        returncode: int | None = None,
    ) -> None:
        self._background_event_sink.emit(RuntimeSshWarning(kind, returncode))

    def _emit_direct_warning(self, kind: RuntimeSshWarningKind) -> None:
        if kind in self._reported_direct_warnings:
            return
        self._reported_direct_warnings.add(kind)
        self._event_sink.emit(RuntimeSshWarning(kind))


def stop_runtime_ssh_service(
    handle: SshdProcess | None,
    *,
    cancel_requested: Callable[[], bool],
    shutdown_requested: threading.Event,
    timeout: float = SSHD_STOP_TIMEOUT_SECONDS,
    poll_interval: float = SSHD_STOP_POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    warning_observer: Callable[[RuntimeSshWarningKind], object],
) -> bool:
    """Terminate, bound, and reap one cdh-owned sshd child."""
    if handle is None:
        return True
    try:
        terminal = reap_process_terminal(handle)
    except Exception:
        warning_observer(RuntimeSshWarningKind.SERVICE_REAP_FAILED)
        return False
    if terminal is not None:
        return True

    shutdown_requested.set()
    try:
        result = terminate_direct_process_until(
            handle,
            deadline=monotonic() + timeout,
            poll_interval=poll_interval,
            force_requested=cancel_requested,
            monotonic=monotonic,
            sleep=sleep,
        )
    except Exception:
        warning_observer(RuntimeSshWarningKind.SERVICE_SHUTDOWN_FAILED)
        return False
    if result.forced:
        warning_observer(RuntimeSshWarningKind.FORCE_TERMINATION_REQUIRED)
        return False
    return True
