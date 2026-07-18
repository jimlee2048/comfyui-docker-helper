"""Runtime SSH service lifetime ownership."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container.process_control import (
    DirectProcess,
    reap_process_terminal,
    request_force_direct_process,
    request_terminate_direct_process,
    terminate_direct_process_until,
)
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_diagnostics import runtime_error_reason
from comfyui_docker_helper.container.ssh import (
    SshdProcess,
    SshdStartupError,
    start_sshd_if_enabled,
)
from comfyui_docker_helper.errors import ApplicationError

SSHD_STOP_TIMEOUT_SECONDS = 5.0
SSHD_STOP_POLL_INTERVAL_SECONDS = 0.05


class RuntimeSshStarter(Protocol):
    """Start the configured runtime SSH service."""

    def __call__(
        self,
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Callable[[str], object],
        preparation_process_observer: Callable[[DirectProcess | None], None],
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
        runtime: ContainerRuntime,
        starter: RuntimeSshStarter = start_sshd_if_enabled,
        log: Callable[[str], object] = print,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._starter = starter
        self._log = log
        self._handle: SshdProcess | None = None
        self._shutdown_requested = threading.Event()
        self._startup_thread: threading.Thread | None = None
        self._startup_process: DirectProcess | None = None
        self._startup_lock = threading.Lock()
        self._handle_reaped = threading.Event()

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

        def observe_preparation_process(process: DirectProcess | None) -> None:
            with self._startup_lock:
                self._startup_process = process
                cancellation_requested = self._shutdown_requested.is_set()
            if process is not None and cancellation_requested:
                try:
                    request_terminate_direct_process(process)
                except Exception as error:
                    print(
                        "WARNING: SSH startup terminate failed: "
                        f"reason={runtime_error_reason(error)}",
                        file=sys.stderr,
                    )

        def start() -> None:
            try:
                handle = self._starter(
                    self._config,
                    runtime=self._runtime,
                    log=self._log,
                    preparation_process_observer=observe_preparation_process,
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
                    f"SSH runtime service failed to start: {error}"
                ) from error
            raise error
        if len(result) != 1:
            raise RuntimeSshServiceError("SSH runtime service result is missing")
        self._handle = result[0]

    def ensure_running_before_comfyui(self) -> None:
        """Reject startup if an activated sshd has already exited."""
        if self._handle is None:
            return
        returncode = self._handle.poll()
        if returncode is not None:
            raise RuntimeSshServiceError(
                f"SSH runtime service exited before ComfyUI: {returncode}"
            )

    def monitor_after_comfyui_start(self) -> None:
        """Report unexpected sshd exit without changing ComfyUI ownership."""
        if self._handle is None:
            return

        def wait_for_exit() -> None:
            assert self._handle is not None
            try:
                returncode = self._handle.wait()
            except Exception as error:
                if self._shutdown_requested.is_set():
                    return
                print(
                    "WARNING: SSH runtime service monitor failed: "
                    f"reason={runtime_error_reason(error)}",
                    file=sys.stderr,
                )
                return
            else:
                self._handle_reaped.set()
            if self._shutdown_requested.is_set():
                return
            print(
                "WARNING: SSH runtime service exited unexpectedly: "
                f"returncode={returncode}",
                file=sys.stderr,
            )

        threading.Thread(
            target=wait_for_exit,
            name="cdh-sshd-monitor",
            daemon=True,
        ).start()

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
        stopped = stop_runtime_ssh_service(
            self._handle,
            cancel_requested=cancel_requested,
            shutdown_requested=self._shutdown_requested,
            timeout=timeout,
            poll_interval=poll_interval,
            monotonic=monotonic,
            sleep=sleep,
            log=self._log,
        )
        if stopped:
            self._handle_reaped.set()
        return stopped

    def request_stop(self) -> None:
        """Promptly start cooperative cancellation without waiting."""
        self._shutdown_requested.set()
        self._signal_startup_process(force=False)
        handle = self._handle
        if handle is None or handle.poll() is not None:
            return
        self._log("SSH runtime service stop requested")
        try:
            request_terminate_direct_process(handle)
        except Exception as error:
            print(
                "WARNING: SSH runtime service terminate failed: "
                f"reason={runtime_error_reason(error)}",
                file=sys.stderr,
            )

    def is_stopped(self) -> bool:
        """Reap and report all owned startup and sshd operations."""
        startup = self._startup_thread
        if startup is not None:
            startup.join(timeout=0.0)
            if startup.is_alive():
                return False
        handle = self._handle
        if handle is None:
            return True
        if self._handle_reaped.is_set():
            return True
        try:
            terminal = reap_process_terminal(handle)
        except Exception as error:
            print(
                "WARNING: SSH runtime service reap failed: "
                f"reason={runtime_error_reason(error)}",
                file=sys.stderr,
            )
            return False
        if terminal is not None:
            self._handle_reaped.set()
            return True
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
        except Exception as error:
            print(
                "WARNING: SSH startup process signal failed: "
                f"reason={runtime_error_reason(error)}",
                file=sys.stderr,
            )


def stop_runtime_ssh_service(
    handle: SshdProcess | None,
    *,
    cancel_requested: Callable[[], bool],
    shutdown_requested: threading.Event,
    timeout: float = SSHD_STOP_TIMEOUT_SECONDS,
    poll_interval: float = SSHD_STOP_POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    log: Callable[[str], object] = print,
) -> bool:
    """Terminate, bound, and reap one cdh-owned sshd child."""
    if handle is None:
        return True
    try:
        terminal = reap_process_terminal(handle)
    except Exception as error:
        print(
            "WARNING: SSH runtime service reap failed: "
            f"reason={runtime_error_reason(error)}",
            file=sys.stderr,
        )
        return False
    if terminal is not None:
        return True

    shutdown_requested.set()
    log("SSH runtime service stop requested")
    try:
        result = terminate_direct_process_until(
            handle,
            deadline=monotonic() + timeout,
            poll_interval=poll_interval,
            force_requested=cancel_requested,
            monotonic=monotonic,
            sleep=sleep,
        )
    except Exception as error:
        print(
            "WARNING: SSH runtime service shutdown failed: "
            f"reason={runtime_error_reason(error)}",
            file=sys.stderr,
        )
        return False
    if result.forced:
        print(
            "WARNING: SSH runtime service required force termination",
            file=sys.stderr,
        )
        return False
    log("SSH runtime service stopped")
    return True
