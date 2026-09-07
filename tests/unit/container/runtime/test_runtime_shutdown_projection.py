"""Lifecycle deadline authority and bounded final-controller cleanup."""

from __future__ import annotations

import signal
import time

import pytest

from comfyui_docker_helper.container.runtime import serve as serve_module
from comfyui_docker_helper.container.runtime.lifecycle import (
    _StartupShutdownRequested,
    _StartupShutdownState,
)
from comfyui_docker_helper.container.runtime.logging import RuntimeLoggingBroker
from comfyui_docker_helper.container.runtime.shutdown import RuntimeShutdownDeadline
from comfyui_docker_helper.errors import ApplicationError


def test_external_takeover_reuses_accepted_restart_deadline():
    observed = []
    state = _StartupShutdownState(
        shutdown_timeout=10,
        monotonic=lambda: 100,
        shutdown_deadline_observer=observed.append,
        generation="gen-1",
    )
    timeline = state.accept_timeline(now=5)
    state.raise_on_signal = False
    state.request_shutdown(signal.SIGTERM)
    state.request_shutdown(signal.SIGINT)
    assert state.timeline is timeline
    assert observed and all(
        item == RuntimeShutdownDeadline("gen-1", 15) for item in observed
    )
    assert state.repeated_signal_requested()


@pytest.mark.parametrize("remaining", [None, -1.0, 0.0, 0.2, 10.0])
def test_exceptional_controller_cleanup_uses_one_clipped_deadline(
    monkeypatch, remaining
):
    closes = []

    class Broker(RuntimeLoggingBroker):
        def start(self):
            pass

        def configure(self, settings):
            self._settings = settings

        def close(self, *, deadline, force_requested):
            closes.append(("broker", deadline))
            assert not force_requested()

    class Delivery:
        def __init__(self, *args, **kwargs):
            pass

        def close(self, *, deadline, force_requested):
            closes.append(("delivery", deadline))

    def fail(**kwargs):
        if remaining is not None:
            kwargs["controller"].observe_shutdown_deadline(
                RuntimeShutdownDeadline("gen-1", 10 + remaining)
            )
        raise ApplicationError("controlled", exit_code=7)

    class Server:
        def __init__(self, *_args):
            pass

        def start(self):
            pass

        def stop_accepting(self, *, deadline, force_requested):
            closes.append(("stop_accepting", deadline))

        def close(self, *, deadline, force_requested):
            closes.append(("server", deadline))

    monkeypatch.setattr(
        serve_module, "open_runtime_control_listener", lambda path: None
    )
    monkeypatch.setattr(serve_module, "RuntimeControlServer", Server)
    monkeypatch.setattr(serve_module, "RuntimeEventDelivery", Delivery)
    monkeypatch.setattr(serve_module, "_run_runtime_serve", fail)
    before = time.monotonic()
    result = serve_module.run_runtime_serve(
        environ={"CDH_LOG_MODE": "memory"},
        runtime_logging_factory=lambda observer: Broker(),
        monotonic=lambda: 10,
    )
    after = time.monotonic()
    assert result == 7
    assert [item[0] for item in closes] == [
        "stop_accepting",
        "delivery",
        "broker",
        "server",
    ]
    assert len({item[1] for item in closes}) == 1
    allowance = 0.5 if remaining is None else min(0.5, max(0, remaining))
    assert before + allowance <= closes[0][1] <= after + allowance


def test_first_and_repeated_signals_during_final_cleanup_are_observed(monkeypatch):
    handlers = {}
    observations = []

    def install(sig, handler):
        handlers[sig] = handler

    monkeypatch.setattr(signal, "getsignal", lambda sig: signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", install)

    class Broker(RuntimeLoggingBroker):
        def start(self):
            pass

        def configure(self, settings):
            self._settings = settings

        def close(self, *, deadline, force_requested):
            observations.append(force_requested())

    class Delivery:
        def __init__(self, *args, **kwargs):
            pass

        def close(self, *, deadline, force_requested):
            assert not force_requested()
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            assert force_requested()

    class Server:
        def __init__(self, *_args):
            pass

        def start(self):
            pass

        def stop_accepting(self, *, deadline, force_requested):
            pass

        def close(self, *, deadline, force_requested):
            observations.append(force_requested())

    monkeypatch.setattr(
        serve_module, "open_runtime_control_listener", lambda path: None
    )
    monkeypatch.setattr(serve_module, "RuntimeControlServer", Server)
    monkeypatch.setattr(serve_module, "RuntimeEventDelivery", Delivery)
    monkeypatch.setattr(serve_module, "_run_runtime_serve", lambda **kwargs: 0)
    assert (
        serve_module.run_runtime_serve(
            environ={"CDH_LOG_MODE": "memory"},
            runtime_logging_factory=lambda observer: Broker(),
        )
        == 0
    )
    assert observations == [True, True]
    assert handlers[signal.SIGTERM] == signal.SIG_DFL


def test_signal_between_timeline_assignment_and_observer_still_projects_deadline():
    observed = []

    class InterruptedState(_StartupShutdownState):
        interrupted = False

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if name == "timeline" and value is not None and not self.interrupted:
                self.interrupted = True
                self.request_shutdown(signal.SIGTERM)

    state = InterruptedState(
        shutdown_timeout=10,
        monotonic=lambda: 100,
        shutdown_deadline_observer=observed.append,
        generation="gen-1",
    )
    with pytest.raises(_StartupShutdownRequested):
        state.accept_timeline(now=5)
    assert state.timeline.deadline == 15
    assert observed == [RuntimeShutdownDeadline("gen-1", 15)]
