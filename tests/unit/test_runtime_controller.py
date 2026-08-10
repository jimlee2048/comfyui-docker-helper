"""Single-owner runtime controller transition and arbitration coverage."""

from __future__ import annotations

import signal
from dataclasses import asdict

import pytest

from comfyui_docker_helper.container.runtime_controller import (
    RuntimeController,
    RuntimeControllerError,
)


def _running_controller() -> RuntimeController:
    controller = RuntimeController()
    controller.begin_initial_admission()
    controller.mark_initial_generation_running()
    return controller


def test_initial_and_running_status_have_only_fixed_fields() -> None:
    controller = RuntimeController()

    assert asdict(controller.snapshot()) == {
        "state": "starting",
        "phase": "admitting",
        "generation": None,
        "operation": None,
        "last_restart": None,
    }

    assert controller.begin_initial_admission() == "gen-1"
    controller.mark_initial_generation_running()

    assert asdict(controller.snapshot()) == {
        "state": "running",
        "phase": None,
        "generation": "gen-1",
        "operation": None,
        "last_restart": None,
    }


def test_restart_is_busy_until_initial_generation_is_running() -> None:
    controller = RuntimeController()

    submission = controller.submit_restart()

    assert submission.disposition == "busy"
    assert submission.ticket is None
    assert submission.active_operation is None


def test_main_thread_acceptance_allocates_operation_and_stopping_state() -> None:
    controller = _running_controller()
    submission = controller.submit_restart()
    assert submission.ticket is not None
    pending = submission.ticket.snapshot()
    assert pending.state == "pending"
    assert controller.wait(0) is True

    assert controller.accept_if_requested(accepted_at=12.0) is True

    accepted = submission.ticket.wait_for_change(pending.revision, timeout=0)
    assert accepted.state == "accepted"
    assert accepted.operation == "op-1"
    assert asdict(controller.snapshot()) == {
        "state": "restarting",
        "phase": "stopping_generation",
        "generation": "gen-1",
        "operation": "op-1",
        "last_restart": None,
    }
    assert controller.wait(0) is False


def test_second_restart_is_busy_and_not_queued() -> None:
    controller = _running_controller()
    first = controller.submit_restart()
    assert first.ticket is not None

    pending_busy = controller.submit_restart()
    assert pending_busy.disposition == "busy"
    assert pending_busy.active_operation is None
    assert controller.accept_if_requested(accepted_at=0.0) is True

    active_busy = controller.submit_restart()
    assert active_busy.disposition == "busy"
    assert active_busy.active_operation == "op-1"


def test_successor_allocation_and_success_release_the_mutation_slot() -> None:
    controller = _running_controller()
    submission = controller.submit_restart()
    assert submission.ticket is not None
    controller.accept_if_requested(accepted_at=0.0)

    assert controller.allocate_restart_successor() == "gen-2"
    assert controller.snapshot().phase == "starting_generation"
    controller.publish_restart_terminal("succeeded")

    terminal = submission.ticket.snapshot()
    assert terminal.state == "succeeded"
    assert terminal.operation == "op-1"
    assert controller.snapshot().phase == "finalizing"
    assert controller.snapshot().operation == "op-1"
    assert controller.release_successful_restart() is True

    snapshot = controller.snapshot()
    assert snapshot.state == "running"
    assert snapshot.phase is None
    assert snapshot.generation == "gen-2"
    assert snapshot.operation is None
    assert snapshot.last_restart is not None
    assert asdict(snapshot.last_restart) == {
        "id": "op-1",
        "result": "succeeded",
    }
    second = controller.submit_restart()
    assert second.disposition == "submitted"
    assert controller.accept_if_requested(accepted_at=1.0) is True
    assert second.ticket is not None
    assert second.ticket.snapshot().operation == "op-2"


def test_restart_failure_is_terminal_and_retains_one_summary() -> None:
    controller = _running_controller()
    submission = controller.submit_restart()
    assert submission.ticket is not None
    controller.accept_if_requested(accepted_at=0.0)

    controller.publish_restart_terminal("failed", message="successor failed")

    ticket = submission.ticket.snapshot()
    assert ticket.state == "failed"
    assert ticket.message == "successor failed"
    snapshot = controller.snapshot()
    assert snapshot.state == "stopping"
    assert snapshot.phase == "finalizing"
    assert snapshot.operation == "op-1"
    assert snapshot.last_restart is not None
    assert snapshot.last_restart.result == "failed"
    assert submission.ticket.snapshot().state == "failed"


def test_natural_exit_wins_before_restart_acceptance() -> None:
    controller = _running_controller()
    submission = controller.submit_restart()
    assert submission.ticket is not None

    controller.mark_generation_terminal("ComfyUI exited.")

    assert controller.accept_if_requested(accepted_at=0.0) is False
    assert submission.ticket.snapshot().state == "rejected"
    assert controller.snapshot().state == "stopping"


def test_external_shutdown_wins_before_acceptance_and_is_irreversible() -> None:
    controller = _running_controller()
    submission = controller.submit_restart()
    assert submission.ticket is not None

    controller.observe_external_signal(signal.SIGINT)
    first = controller.external_shutdown_snapshot()

    assert first.signal is signal.SIGINT
    assert first.repeated is False
    assert controller.accept_if_requested(accepted_at=0.0) is False
    assert submission.ticket.snapshot().state == "rejected"
    controller.mark_external_shutdown()
    assert controller.snapshot().state == "stopping"

    controller.observe_external_signal(signal.SIGTERM)
    second = controller.external_shutdown_snapshot()
    assert second.signal is signal.SIGINT
    assert second.repeated is True


def test_external_shutdown_after_old_quiescence_suppresses_successor() -> None:
    controller = _running_controller()
    submission = controller.submit_restart()
    assert submission.ticket is not None
    assert controller.accept_if_requested(accepted_at=0.0) is True

    controller.observe_external_signal(signal.SIGTERM)

    assert controller.allocate_restart_successor() is None
    snapshot = controller.snapshot()
    assert snapshot.state == "stopping"
    assert snapshot.phase == "finalizing"
    assert snapshot.operation == "op-1"
    assert snapshot.last_restart is not None
    assert snapshot.last_restart.result == "failed"


def test_pending_restart_can_be_withdrawn_but_accepted_restart_cannot() -> None:
    controller = _running_controller()
    first = controller.submit_restart()
    assert first.ticket is not None

    assert controller.withdraw_restart(first.ticket) is True
    assert first.ticket.snapshot().state == "rejected"
    assert controller.accept_if_requested(accepted_at=0.0) is False

    second = controller.submit_restart()
    assert second.ticket is not None
    assert controller.accept_if_requested(accepted_at=0.0) is True
    assert controller.withdraw_restart(second.ticket) is False


def test_external_shutdown_after_success_terminal_cannot_restore_running() -> None:
    controller = _running_controller()
    submission = controller.submit_restart()
    assert submission.ticket is not None
    controller.accept_if_requested(accepted_at=0.0)
    controller.allocate_restart_successor()
    controller.publish_restart_terminal("succeeded")

    controller.observe_external_signal(signal.SIGTERM)
    controller.mark_external_shutdown()

    assert controller.release_successful_restart() is False
    snapshot = controller.snapshot()
    assert snapshot.state == "stopping"
    assert snapshot.phase == "finalizing"
    assert snapshot.generation == "gen-2"
    assert snapshot.operation == "op-1"
    assert snapshot.last_restart is not None
    assert snapshot.last_restart.result == "succeeded"


def test_generation_terminal_cannot_orphan_an_active_restart() -> None:
    controller = _running_controller()
    submission = controller.submit_restart()
    assert submission.ticket is not None
    controller.accept_if_requested(accepted_at=0.0)

    with pytest.raises(RuntimeControllerError, match="explicit terminal"):
        controller.mark_generation_terminal("wrong terminal path")

    assert submission.ticket.snapshot().state == "accepted"
    assert controller.snapshot().operation == "op-1"


def test_invalid_transition_fails_without_mutating_state() -> None:
    controller = RuntimeController()

    with pytest.raises(RuntimeControllerError, match="No stopped restart"):
        controller.allocate_restart_successor()

    assert controller.snapshot().state == "starting"


def test_runtime_failure_wakes_owner_and_rejects_restart_admission() -> None:
    controller = _running_controller()
    pending = controller.submit_restart(delivery_expected=False)
    assert pending.ticket is not None

    controller.observe_runtime_failure("stdout failed")
    controller.observe_runtime_failure("stderr failed later")

    assert controller.runtime_failure_message() == "stdout failed"
    assert controller.wait(0) is True
    assert controller.accept_if_requested(accepted_at=0.0) is False
    assert pending.ticket.snapshot().state == "rejected"
    assert pending.ticket.snapshot().message == "stdout failed"
    assert controller.submit_restart().disposition == "busy"


def test_runtime_failure_after_restart_cleanup_blocks_successor() -> None:
    controller = _running_controller()
    submission = controller.submit_restart(delivery_expected=False)
    assert submission.ticket is not None
    assert controller.accept_if_requested(accepted_at=0.0) is True

    controller.observe_runtime_failure("stderr failed")

    assert controller.allocate_restart_successor() is None
    assert submission.ticket.snapshot().state == "failed"
    assert submission.ticket.snapshot().message == "stderr failed"
    assert controller.snapshot().last_restart is not None
    assert controller.snapshot().last_restart.result == "failed"


def test_runtime_failure_before_success_checkpoint_publishes_failure() -> None:
    controller = _running_controller()
    submission = controller.submit_restart(delivery_expected=False)
    assert submission.ticket is not None
    assert controller.accept_if_requested(accepted_at=0.0) is True
    assert controller.allocate_restart_successor() == "gen-2"

    controller.observe_runtime_failure("stdout failed at checkpoint")
    controller.publish_restart_terminal("succeeded")

    assert submission.ticket.snapshot().state == "failed"
    assert submission.ticket.snapshot().message == "stdout failed at checkpoint"
    assert controller.snapshot().last_restart is not None
    assert controller.snapshot().last_restart.result == "failed"
    with pytest.raises(RuntimeControllerError, match="No successful restart"):
        controller.release_successful_restart()
