"""Single-owner state and restart arbitration for the container runtime."""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass, field
from typing import Literal

from comfyui_docker_helper.container.runtime_control import (
    RuntimeControllerPhase,
    RuntimeControllerState,
)

type RuntimeRestartTicketState = Literal[
    "pending",
    "accepted",
    "succeeded",
    "failed",
    "rejected",
]
type RuntimeRestartSubmissionDisposition = Literal["submitted", "busy"]


class RuntimeControllerError(RuntimeError):
    """An invalid internal controller transition."""


@dataclass(frozen=True, slots=True)
class RuntimeRestartTicketSnapshot:
    revision: int
    state: RuntimeRestartTicketState
    operation: str | None
    message: str | None


@dataclass(slots=True)
class RuntimeRestartTicket:
    """One listener proposal observed through immutable snapshots."""

    _condition: threading.Condition = field(default_factory=threading.Condition)
    _revision: int = 0
    _state: RuntimeRestartTicketState = "pending"
    _operation: str | None = None
    _message: str | None = None
    _delivery_complete: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> RuntimeRestartTicketSnapshot:
        with self._condition:
            return self._snapshot_unlocked()

    def wait_for_change(
        self,
        revision: int,
        timeout: float | None = None,
    ) -> RuntimeRestartTicketSnapshot:
        with self._condition:
            self._condition.wait_for(lambda: self._revision != revision, timeout)
            return self._snapshot_unlocked()

    def mark_delivery_complete(self) -> None:
        self._delivery_complete.set()

    def wait_for_delivery(self, timeout: float) -> bool:
        return self._delivery_complete.wait(timeout)

    def _publish(
        self,
        state: RuntimeRestartTicketState,
        *,
        operation: str | None = None,
        message: str | None = None,
    ) -> None:
        with self._condition:
            self._revision += 1
            self._state = state
            if operation is not None:
                self._operation = operation
            self._message = message
            self._condition.notify_all()

    def _snapshot_unlocked(self) -> RuntimeRestartTicketSnapshot:
        return RuntimeRestartTicketSnapshot(
            revision=self._revision,
            state=self._state,
            operation=self._operation,
            message=self._message,
        )


@dataclass(frozen=True, slots=True)
class RuntimeRestartSubmission:
    disposition: RuntimeRestartSubmissionDisposition
    ticket: RuntimeRestartTicket | None = None
    active_operation: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeExternalShutdownSnapshot:
    signal: signal.Signals | None
    repeated: bool


@dataclass(frozen=True, slots=True)
class RuntimeLastRestartSnapshot:
    id: str
    result: Literal["succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class RuntimeControllerSnapshot:
    state: RuntimeControllerState
    phase: RuntimeControllerPhase | None
    generation: str | None
    operation: str | None
    last_restart: RuntimeLastRestartSnapshot | None


class RuntimeController:
    """Own the sole mutable runtime state and restart proposal slot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._restart_wakeup = threading.Event()
        self._state: RuntimeControllerState = "starting"
        self._phase: RuntimeControllerPhase | None = "admitting"
        self._generation_counter = 0
        self._generation: str | None = None
        self._operation_counter = 0
        self._operation: str | None = None
        self._last_restart: RuntimeLastRestartSnapshot | None = None
        self._pending_restart: RuntimeRestartTicket | None = None
        self._active_restart: RuntimeRestartTicket | None = None
        self._active_terminal_result: Literal["succeeded", "failed"] | None = None
        self._external_signal_state: tuple[signal.Signals | None, bool] = (
            None,
            False,
        )
        self._runtime_failure: str | None = None

    def snapshot(self) -> RuntimeControllerSnapshot:
        with self._lock:
            return RuntimeControllerSnapshot(
                state=self._state,
                phase=self._phase,
                generation=self._generation,
                operation=self._operation,
                last_restart=self._last_restart,
            )

    def submit_restart(
        self,
        *,
        delivery_expected: bool = True,
    ) -> RuntimeRestartSubmission:
        """Publish at most one pending proposal without accepting it."""
        with self._lock:
            if (
                self._state != "running"
                or self._pending_restart is not None
                or self._active_restart is not None
                or self._external_signal_state[0] is not None
                or self._runtime_failure is not None
            ):
                return RuntimeRestartSubmission(
                    disposition="busy",
                    active_operation=self._operation,
                )
            ticket = RuntimeRestartTicket()
            if not delivery_expected:
                ticket.mark_delivery_complete()
            self._pending_restart = ticket
            self._restart_wakeup.set()
            return RuntimeRestartSubmission(disposition="submitted", ticket=ticket)

    def withdraw_restart(self, ticket: RuntimeRestartTicket) -> bool:
        """Withdraw only a proposal that has not crossed acceptance."""
        with self._lock:
            if self._pending_restart is not ticket:
                return False
            self._pending_restart = None
            self._restart_wakeup.clear()
            ticket._publish("rejected", message="Restart request was withdrawn.")
            return True

    def accept_if_requested(self, *, accepted_at: float) -> bool:
        """Linearize one pending restart on the lifecycle main thread."""
        del accepted_at
        with self._lock:
            ticket = self._pending_restart
            if (
                ticket is None
                or self._state != "running"
                or self._external_signal_state[0] is not None
                or self._runtime_failure is not None
            ):
                if ticket is not None:
                    if self._external_signal_state[0] is not None:
                        self._reject_pending_unlocked(
                            "Container shutdown was requested."
                        )
                    elif self._runtime_failure is not None:
                        self._reject_pending_unlocked(self._runtime_failure)
                return False
            self._operation_counter += 1
            operation = f"op-{self._operation_counter}"
            self._pending_restart = None
            self._active_restart = ticket
            self._operation = operation
            self._state = "restarting"
            self._phase = "stopping_generation"
            self._restart_wakeup.clear()
            ticket._publish("accepted", operation=operation)
            return True

    def wait(self, timeout: float) -> bool:
        """Provide the level-triggered wake required by one generation wait."""
        return self._restart_wakeup.wait(timeout)

    def begin_initial_admission(self) -> str:
        with self._lock:
            if self._generation is not None or self._state != "starting":
                raise RuntimeControllerError("Initial admission already began.")
            if self._external_signal_state[0] is not None:
                raise RuntimeControllerError("External shutdown is already admitted.")
            if self._runtime_failure is not None:
                raise RuntimeControllerError("Runtime controller failure is admitted.")
            self._generation_counter = 1
            self._generation = "gen-1"
            return self._generation

    def mark_initial_generation_running(self) -> None:
        with self._lock:
            if (
                self._state != "starting"
                or self._phase != "admitting"
                or self._generation is None
            ):
                raise RuntimeControllerError("Initial generation is not admitting.")
            if self._external_signal_state[0] is not None:
                raise RuntimeControllerError("External shutdown is already admitted.")
            if self._runtime_failure is not None:
                raise RuntimeControllerError("Runtime controller failure is admitted.")
            self._state = "running"
            self._phase = None

    def allocate_restart_successor(self) -> str | None:
        """Allocate the successor only while shutdown disposition permits it."""
        with self._lock:
            if (
                self._state != "restarting"
                or self._phase != "stopping_generation"
                or self._active_restart is None
            ):
                raise RuntimeControllerError(
                    "No stopped restart may admit a successor."
                )
            if self._external_signal_state[0] is not None:
                self._publish_restart_terminal_unlocked(
                    "failed",
                    message="Container shutdown interrupted restart.",
                )
                return None
            if self._runtime_failure is not None:
                self._publish_restart_terminal_unlocked(
                    "failed",
                    message=self._runtime_failure,
                )
                return None
            self._generation_counter += 1
            self._generation = f"gen-{self._generation_counter}"
            self._phase = "starting_generation"
            return self._generation

    def publish_restart_terminal(
        self,
        result: Literal["succeeded", "failed"],
        *,
        message: str | None = None,
    ) -> None:
        with self._lock:
            self._publish_restart_terminal_unlocked(result, message=message)

    def release_successful_restart(self) -> bool:
        with self._lock:
            if self._active_terminal_result != "succeeded":
                raise RuntimeControllerError("No successful restart may be released.")
            if self._external_signal_state[0] is not None or self._state == "stopping":
                self._state = "stopping"
                self._phase = "finalizing"
                return False
            if self._runtime_failure is not None:
                self._state = "stopping"
                self._phase = "finalizing"
                return False
            if (
                self._state != "restarting"
                or self._phase != "finalizing"
                or self._active_restart is None
                or self._operation is None
            ):
                raise RuntimeControllerError(
                    "Successful restart state is inconsistent."
                )
            self._operation = None
            self._active_restart = None
            self._active_terminal_result = None
            self._state = "running"
            self._phase = None
            return True

    def mark_generation_terminal(self, message: str) -> None:
        """Reject any unaccepted proposal and enter irreversible finalization."""
        with self._lock:
            if (
                self._active_restart is not None
                or self._active_terminal_result is not None
            ):
                raise RuntimeControllerError(
                    "An active restart requires an explicit terminal result."
                )
            self._reject_pending_unlocked(message)
            self._state = "stopping"
            self._phase = "finalizing"

    def observe_external_signal(
        self,
        sig: signal.Signals,
    ) -> None:
        """Latch one external signal without controller locks or I/O."""
        requested_signal, repeated = self._external_signal_state
        if requested_signal is None:
            self._external_signal_state = (sig, False)
        elif not repeated:
            self._external_signal_state = (requested_signal, True)

    def mark_external_shutdown(self) -> None:
        """Apply the already-latched shutdown disposition on the main thread."""
        with self._lock:
            if self._external_signal_state[0] is None:
                raise RuntimeControllerError("No external shutdown was observed.")
            self._reject_pending_unlocked("Container shutdown was requested.")
            if (
                self._active_restart is not None
                and self._active_terminal_result is None
            ):
                self._publish_restart_terminal_unlocked(
                    "failed",
                    message="Container shutdown interrupted restart.",
                )
            self._state = "stopping"
            self._phase = "finalizing"

    def external_shutdown_snapshot(self) -> RuntimeExternalShutdownSnapshot:
        requested_signal, repeated = self._external_signal_state
        return RuntimeExternalShutdownSnapshot(
            signal=requested_signal,
            repeated=repeated,
        )

    def observe_runtime_failure(self, message: str) -> None:
        """Latch the first controller-fatal runtime failure and wake the owner."""
        with self._lock:
            if self._runtime_failure is not None:
                return
            self._runtime_failure = message
            self._restart_wakeup.set()

    def runtime_failure_message(self) -> str | None:
        with self._lock:
            return self._runtime_failure

    def wait_for_terminal_delivery(self, timeout: float) -> bool:
        with self._lock:
            ticket = self._active_restart
            if ticket is None or self._active_terminal_result is None:
                return True
        return ticket.wait_for_delivery(timeout)

    def _publish_restart_terminal_unlocked(
        self,
        result: Literal["succeeded", "failed"],
        *,
        message: str | None,
    ) -> None:
        if result == "succeeded" and self._runtime_failure is not None:
            result = "failed"
            message = self._runtime_failure
        ticket = self._active_restart
        operation = self._operation
        if (
            ticket is None
            or operation is None
            or self._active_terminal_result is not None
        ):
            raise RuntimeControllerError("No active restart may publish a result.")
        if result == "succeeded" and (
            self._state != "restarting" or self._phase != "starting_generation"
        ):
            raise RuntimeControllerError("No successor is ready to complete.")
        self._last_restart = RuntimeLastRestartSnapshot(id=operation, result=result)
        self._active_terminal_result = result
        self._phase = "finalizing"
        if result == "failed":
            self._state = "stopping"
        ticket._publish(result, operation=operation, message=message)

    def _reject_pending_unlocked(self, message: str) -> None:
        ticket = self._pending_restart
        if ticket is None:
            return
        self._pending_restart = None
        self._restart_wakeup.clear()
        ticket._publish("rejected", message=message)
