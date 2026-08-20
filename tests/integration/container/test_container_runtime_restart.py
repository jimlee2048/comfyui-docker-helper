"""Serial complete-instance runtime restart integration coverage."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from unittest.mock import Mock

import pytest

from comfyui_docker_helper.config import Diagnostic
from comfyui_docker_helper.container import runtime_lifecycle as lifecycle_module
from comfyui_docker_helper.container import runtime_serve as runtime_serve_module
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_control import (
    RuntimeAcceptedResponse,
    RuntimeAckRequest,
    RuntimeRestartRequest,
    RuntimeTerminalResponse,
    connect_runtime_control,
    receive_runtime_control_response,
    send_runtime_control_message,
)
from comfyui_docker_helper.container.runtime_controller import (
    RuntimeController,
    RuntimeRestartSubmission,
    RuntimeRestartTicket,
)
from comfyui_docker_helper.container.runtime_downloads import (
    RuntimeAsyncDownloadQueueHandle,
)
from comfyui_docker_helper.container.runtime_events import (
    RuntimeGenerationAdmitted,
    RuntimeGenerationOperation,
    RuntimeGenerationReady,
    RuntimeGenerationStopCause,
    RuntimeGenerationStopped,
    RuntimeGenerationStopping,
    RuntimePhase,
    RuntimePhaseCompleted,
    RuntimePhaseFailed,
    RuntimePhaseStarted,
    RuntimeSshOutcome,
    RuntimeSshStatus,
)
from comfyui_docker_helper.container.runtime_files import Logger, RuntimeFilePlan
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    RuntimeHookPlan,
    RuntimeHookResult,
)
from comfyui_docker_helper.container.runtime_serve import (
    RuntimeExecutionError,
    run_runtime_serve,
)


class _RestartChild:
    def __init__(self, events: list[str], name: str) -> None:
        self.returncode: int | None = None
        self.events = events
        self.name = name
        self.signals: list[signal.Signals] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        assert self.returncode is not None
        self.events.append(f"{self.name}:reap")
        return self.returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)
        self.events.append(f"{self.name}:signal:{sig.name}")
        self.returncode = -int(sig)

    def terminate(self) -> None:
        self.events.append(f"{self.name}:terminate")
        self.returncode = -int(signal.SIGTERM)

    def kill(self) -> None:
        self.events.append(f"{self.name}:kill")
        self.returncode = -int(signal.SIGKILL)


def _runtime(tmp_path: Path) -> ContainerRuntime:
    runtime = ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )
    runtime.comfyui_path.mkdir(parents=True)
    return runtime


def _write_config(path: Path, marker: str) -> None:
    path.write_text(
        f'[comfyui]\nextra_args = ["--{marker}"]\n',
        encoding="utf-8",
    )


def _write_hook(root: Path, phase: str, filename: str) -> Path:
    path = root / f"{phase}.d" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _hook_names(plan: RuntimeHookPlan, phase: str) -> list[str]:
    return [hook.filename for hook in plan.for_phase(phase)]


# A controller-lifetime logging failure wakes the runtime and uses normal cleanup.
def test_primary_logging_failure_wakes_serve_and_cleans_exact_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = tmp_path / "runtime.toml"
    hooks = tmp_path / "hooks"
    _write_config(config, "only")
    _write_hook(hooks, "stop", "10-stop.sh")
    events: list[str] = []
    child = _RestartChild(events, "only")
    failure_observer: list[Callable[[str], object]] = []
    semantic_events: list[object] = []

    monkeypatch.setattr(
        runtime_serve_module,
        "default_runtime_display",
        lambda _settings: Mock(emit=semantic_events.append),
    )

    class InjectedLogging:
        def __enter__(self) -> InjectedLogging:
            events.append("logging:start")
            return self

        def __exit__(self, *_exc: object) -> None:
            events.append("logging:close")

    def logging_factory(observer: Callable[[str], object]) -> InjectedLogging:
        failure_observer.append(observer)
        return InjectedLogging()

    def runner(*_args: object, **_kwargs: object) -> _RestartChild:
        events.append("only:spawn")
        return child

    def stop_hooks(
        _plan: RuntimeHookPlan,
        **_kwargs: object,
    ) -> tuple[RuntimeHookResult, ...]:
        events.append("hooks:stop")
        return ()

    def generation_running(_controller: RuntimeController) -> None:
        assert len(failure_observer) == 1
        failure_observer[0]("Runtime stdout primary output failed.")
        events.append("logging:failed")

    with pytest.raises(RuntimeExecutionError, match="runtime logging failed"):
        run_runtime_serve(
            runtime=runtime,
            mounted_config_path=config,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_hooks_path=hooks,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            environ={"PATH": "/usr/bin"},
            runner=runner,  # type: ignore[arg-type]
            runtime_stop_hook_runner=stop_hooks,  # type: ignore[arg-type]
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
            generation_running=generation_running,
            runtime_logging_factory=logging_factory,  # type: ignore[arg-type]
        )

    assert child.signals == [signal.SIGTERM]
    assert events == [
        "logging:start",
        "only:spawn",
        "logging:failed",
        "hooks:stop",
        "only:signal:SIGTERM",
        "only:reap",
        "logging:close",
    ]
    stopped = [
        event
        for event in semantic_events
        if isinstance(event, RuntimeGenerationStopped)
    ]
    assert [(event.generation, event.cause) for event in stopped] == [
        ("gen-1", RuntimeGenerationStopCause.CONTROLLER_FAILURE)
    ]


def test_runtime_display_is_constructed_inside_logging_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []

    class InjectedLogging:
        active = False

        def __enter__(self) -> InjectedLogging:
            self.active = True
            timeline.append("logging:enter")
            return self

        def __exit__(self, *_exc: object) -> None:
            self.active = False
            timeline.append("logging:exit")

    logging = InjectedLogging()
    deliveries: list[object] = []

    class InjectedDelivery:
        def __init__(self, _sink: object, **_kwargs: object) -> None:
            deliveries.append(self)

        def __enter__(self) -> InjectedDelivery:
            assert logging.active is True
            timeline.append("delivery:enter")
            return self

        def __exit__(self, *_exc: object) -> None:
            assert logging.active is True
            timeline.append("delivery:exit")

    def display_factory(_settings: object) -> object:
        assert logging.active is True
        timeline.append("display:create")
        return object()

    def run_serve(**kwargs: object) -> int:
        assert logging.active is True
        assert kwargs["event_sink"] is not None
        assert kwargs["background_event_sink"] is deliveries[0]
        timeline.append("serve:run")
        return 0

    monkeypatch.setattr(
        runtime_serve_module, "default_runtime_display", display_factory
    )
    monkeypatch.setattr(runtime_serve_module, "_run_runtime_serve", run_serve)
    monkeypatch.setattr(
        runtime_serve_module,
        "RuntimeEventDelivery",
        InjectedDelivery,
    )

    assert (
        run_runtime_serve(
            runtime_logging_factory=lambda _observer: logging,  # type: ignore[arg-type]
        )
        == 0
    )
    assert timeline == [
        "logging:enter",
        "display:create",
        "delivery:enter",
        "serve:run",
        "delivery:exit",
        "logging:exit",
    ]


def test_pre_lifecycle_primary_failure_closes_admitted_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semantic_events: list[object] = []
    failure_observers: list[Callable[[str], object]] = []

    class InjectedLogging:
        def __enter__(self) -> InjectedLogging:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def logging_factory(observer: Callable[[str], object]) -> InjectedLogging:
        failure_observers.append(observer)
        return InjectedLogging()

    class FailingAfterAdmissionDisplay:
        def emit(self, event: object) -> None:
            semantic_events.append(event)
            if isinstance(event, RuntimeGenerationAdmitted):
                assert len(failure_observers) == 1
                failure_observers[0]("primary output failed before lifecycle ownership")

    monkeypatch.setattr(
        runtime_serve_module,
        "default_runtime_display",
        lambda _settings: FailingAfterAdmissionDisplay(),
    )

    with pytest.raises(RuntimeExecutionError, match="runtime logging failed"):
        run_runtime_serve(
            runtime=_runtime(tmp_path),
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: pytest.fail("must not start"),
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
            runtime_logging_factory=logging_factory,  # type: ignore[arg-type]
        )

    assert (
        RuntimeGenerationAdmitted("gen-1", RuntimeGenerationOperation.INITIAL_START)
        in semantic_events
    )
    assert semantic_events.count(RuntimeGenerationStopping("gen-1")) == 1
    assert (
        semantic_events.count(
            RuntimeGenerationStopped(
                "gen-1", RuntimeGenerationStopCause.CONTROLLER_FAILURE
            )
        )
        == 1
    )


def test_pre_lifecycle_signal_closes_admitted_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[signal.Signals, object] = {}
    semantic_events: list[object] = []

    monkeypatch.setattr(
        signal,
        "getsignal",
        lambda sig: handlers.get(sig, signal.SIG_DFL),
    )

    def install_handler(sig: signal.Signals, handler: object) -> object:
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(signal, "signal", install_handler)
    monkeypatch.setattr(
        runtime_serve_module,
        "default_runtime_display",
        lambda _settings: Mock(emit=semantic_events.append),
    )

    def interrupt_factory(_factory: object) -> object:
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        raise AssertionError("signal handler must interrupt generation creation")

    monkeypatch.setattr(
        runtime_serve_module.RuntimeGenerationFactory,
        "create_generation",
        interrupt_factory,
    )

    assert (
        run_runtime_serve(
            runtime=_runtime(tmp_path),
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: pytest.fail("must not start"),
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
        )
        == 143
    )
    assert semantic_events.count(RuntimeGenerationStopping("gen-1")) == 1
    assert (
        semantic_events.count(
            RuntimeGenerationStopped(
                "gen-1", RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN
            )
        )
        == 1
    )


def test_signal_during_serve_stopping_finishes_terminal_event_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[signal.Signals, object] = {}
    semantic_events: list[object] = []
    invalid_config = tmp_path / "invalid.toml"
    invalid_config.write_text("[comfyui\ninvalid", encoding="utf-8")

    monkeypatch.setattr(
        signal,
        "getsignal",
        lambda sig: handlers.get(sig, signal.SIG_DFL),
    )

    def install_handler(sig: signal.Signals, handler: object) -> object:
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(signal, "signal", install_handler)

    class SignalOnStoppingDisplay:
        def emit(self, event: object) -> None:
            semantic_events.append(event)
            if isinstance(event, RuntimeGenerationStopping):
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)

    monkeypatch.setattr(
        runtime_serve_module,
        "default_runtime_display",
        lambda _settings: SignalOnStoppingDisplay(),
    )

    assert (
        run_runtime_serve(
            runtime=_runtime(tmp_path),
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=invalid_config,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: pytest.fail("must not start"),
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
        )
        == 143
    )
    assert (
        semantic_events.count(
            RuntimeGenerationAdmitted("gen-1", RuntimeGenerationOperation.INITIAL_START)
        )
        == 1
    )
    assert semantic_events.count(RuntimeGenerationStopping("gen-1")) == 1
    assert (
        semantic_events.count(
            RuntimeGenerationStopped(
                "gen-1", RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN
            )
        )
        == 1
    )


def test_signal_at_lifecycle_handoff_closes_generation_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[signal.Signals, object] = {}
    installs: dict[signal.Signals, int] = {}
    semantic_events: list[object] = []

    monkeypatch.setattr(
        signal,
        "getsignal",
        lambda sig: handlers.get(sig, signal.SIG_DFL),
    )

    def install_handler(sig: signal.Signals, handler: object) -> object:
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        installs[sig] = installs.get(sig, 0) + 1
        if sig is signal.SIGTERM and installs[sig] == 2:
            assert callable(handler)
            handler(signal.SIGTERM, None)
        return previous

    monkeypatch.setattr(signal, "signal", install_handler)
    monkeypatch.setattr(
        runtime_serve_module,
        "default_runtime_display",
        lambda _settings: Mock(emit=semantic_events.append),
    )

    assert (
        run_runtime_serve(
            runtime=_runtime(tmp_path),
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: pytest.fail("must not start"),
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
        )
        == 143
    )
    assert semantic_events.count(RuntimeGenerationStopping("gen-1")) == 1
    assert (
        semantic_events.count(
            RuntimeGenerationStopped(
                "gen-1", RuntimeGenerationStopCause.EXTERNAL_SHUTDOWN
            )
        )
        == 1
    )


def test_late_inner_signal_is_forwarded_after_lifecycle_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[signal.Signals, object] = {}
    observed: list[signal.Signals] = []
    semantic_events: list[object] = []

    monkeypatch.setattr(
        signal,
        "getsignal",
        lambda sig: handlers.get(sig, signal.SIG_DFL),
    )

    def install_handler(sig: signal.Signals, handler: object) -> object:
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(signal, "signal", install_handler)
    original_observe = RuntimeController.observe_external_signal

    def observe(controller: RuntimeController, sig: signal.Signals) -> None:
        observed.append(sig)
        original_observe(controller, sig)

    monkeypatch.setattr(RuntimeController, "observe_external_signal", observe)

    class SignalOnStoppedDisplay:
        def emit(self, event: object) -> None:
            semantic_events.append(event)
            if isinstance(event, RuntimeGenerationStopped):
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)

    class NaturalChild:
        returncode = 0

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        runtime_serve_module,
        "default_runtime_display",
        lambda _settings: SignalOnStoppedDisplay(),
    )

    assert (
        run_runtime_serve(
            runtime=_runtime(tmp_path),
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: NaturalChild(),  # type: ignore[arg-type]
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
        )
        == 0
    )
    assert observed == [signal.SIGTERM]
    assert semantic_events.count(RuntimeGenerationStopping("gen-1")) == 1
    assert (
        semantic_events.count(
            RuntimeGenerationStopped("gen-1", RuntimeGenerationStopCause.NATURAL_EXIT)
        )
        == 1
    )


# Restart arbitration must fully stop the current instance before replacement.
def test_restart_replaces_the_complete_generation_without_owner_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = tmp_path / "runtime.toml"
    hooks = tmp_path / "hooks"
    _write_config(config, "old")
    old_hooks = [
        _write_hook(hooks, "pre-start", "10-old-pre.sh"),
        _write_hook(hooks, "post-start", "20-old-post.sh"),
        _write_hook(hooks, "stop", "30-old-stop.sh"),
    ]
    events: list[str] = []
    children: list[_RestartChild] = []
    submission: RuntimeRestartSubmission | None = None
    semantic_events: list[object] = []

    class RecordingDisplay:
        def emit(self, event: object) -> None:
            semantic_events.append(event)

    monkeypatch.setattr(
        runtime_serve_module,
        "default_runtime_display",
        lambda _settings: RecordingDisplay(),
    )

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> _RestartChild:
        del cwd, env, shell
        marker = "new" if "--new" in argv else "old"
        child = _RestartChild(events, marker)
        children.append(child)
        events.append(f"{marker}:spawn")
        return child

    def startup_hooks(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None,
        log: Logger,
        cancel_requested: Callable[[], bool],
        event_sink: object = None,
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log, cancel_requested, event_sink
        events.append(f"{phase}:{','.join(_hook_names(plan, phase))}")
        return ()

    def stop_hooks(
        plan: RuntimeHookPlan,
        **_kwargs: object,
    ) -> tuple[RuntimeHookResult, ...]:
        events.append(f"stop:{','.join(_hook_names(plan, 'stop'))}")
        return ()

    def generation_running(controller: RuntimeController) -> None:
        nonlocal submission
        if len(children) == 1:
            _write_config(config, "new")
            for path in old_hooks:
                path.unlink()
            _write_hook(hooks, "pre-start", "10-new-pre.sh")
            _write_hook(hooks, "post-start", "20-new-post.sh")
            _write_hook(hooks, "stop", "30-new-stop.sh")
            submission = controller.submit_restart(delivery_expected=False)
            assert submission.disposition == "submitted"
            events.append("restart:submitted")
        else:
            children[-1].returncode = 0

    assert (
        run_runtime_serve(
            runtime=runtime,
            mounted_config_path=config,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_hooks_path=hooks,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_hook_runner=startup_hooks,
            runtime_stop_hook_runner=stop_hooks,
            readiness_waiter=lambda _port, *, child: events.append("readiness"),
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
            generation_running=generation_running,
        )
        == 0
    )

    assert len(children) == 2
    assert children[0].signals == [signal.SIGTERM]
    assert submission is not None and submission.ticket is not None
    assert submission.ticket.snapshot().state == "succeeded"
    admitted = [
        event
        for event in semantic_events
        if isinstance(event, RuntimeGenerationAdmitted)
    ]
    assert [(event.generation, event.operation) for event in admitted] == [
        ("gen-1", RuntimeGenerationOperation.INITIAL_START),
        ("gen-2", RuntimeGenerationOperation.OPERATOR_RESTART),
    ]
    ready = [
        event for event in semantic_events if isinstance(event, RuntimeGenerationReady)
    ]
    assert [event.generation for event in ready] == ["gen-1", "gen-2"]
    assert semantic_events.index(ready[0]) < semantic_events.index(admitted[1])
    assert [
        event.status
        for event in semantic_events
        if isinstance(event, RuntimeSshOutcome)
    ] == [RuntimeSshStatus.DISABLED, RuntimeSshStatus.DISABLED]
    stopping = [
        event
        for event in semantic_events
        if isinstance(event, RuntimeGenerationStopping)
    ]
    stopped = [
        event
        for event in semantic_events
        if isinstance(event, RuntimeGenerationStopped)
    ]
    assert [event.generation for event in stopping] == ["gen-1", "gen-2"]
    assert [(event.generation, event.cause) for event in stopped] == [
        ("gen-1", RuntimeGenerationStopCause.OPERATOR_RESTART),
        ("gen-2", RuntimeGenerationStopCause.NATURAL_EXIT),
    ]
    assert (
        semantic_events.index(ready[0])
        < semantic_events.index(stopping[0])
        < semantic_events.index(stopped[0])
        < semantic_events.index(admitted[1])
        < semantic_events.index(ready[1])
        < semantic_events.index(stopping[1])
        < semantic_events.index(stopped[1])
    )
    assert events == [
        "pre-start:10-old-pre.sh",
        "old:spawn",
        "readiness",
        "post-start:20-old-post.sh",
        "restart:submitted",
        "stop:30-old-stop.sh",
        "old:signal:SIGTERM",
        "old:reap",
        "pre-start:10-new-pre.sh",
        "new:spawn",
        "readiness",
        "post-start:20-new-post.sh",
        "new:reap",
    ]


def test_successor_admission_failure_exits_without_starting_a_second_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = tmp_path / "runtime.toml"
    _write_config(config, "old")
    children: list[_RestartChild] = []
    submission: RuntimeRestartSubmission | None = None
    semantic_events: list[object] = []

    monkeypatch.setattr(
        runtime_serve_module,
        "default_runtime_display",
        lambda _settings: Mock(emit=semantic_events.append),
    )

    def runner(
        _argv: Sequence[str],
        **_kwargs: object,
    ) -> _RestartChild:
        child = _RestartChild([], "old")
        children.append(child)
        return child

    def generation_running(controller: RuntimeController) -> None:
        nonlocal submission
        config.write_text("[comfyui\ninvalid", encoding="utf-8")
        submission = controller.submit_restart(delivery_expected=False)

    with pytest.raises(RuntimeExecutionError, match="runtime configuration is invalid"):
        run_runtime_serve(
            runtime=runtime,
            mounted_config_path=config,
            baked_config_path=tmp_path / "missing-baked.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={"PATH": "/usr/bin"},
            runner=runner,  # type: ignore[arg-type]
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
            generation_running=generation_running,
        )

    assert len(children) == 1
    assert children[0].signals == [signal.SIGTERM]
    assert submission is not None and submission.ticket is not None
    ticket = submission.ticket.snapshot()
    assert ticket.state == "failed"
    assert ticket.operation == "op-1"
    admitted = [
        event
        for event in semantic_events
        if isinstance(event, RuntimeGenerationAdmitted)
    ]
    assert [event.generation for event in admitted] == ["gen-1", "gen-2"]
    assert semantic_events.count(RuntimeGenerationStopping("gen-2")) == 1
    assert (
        semantic_events.count(
            RuntimeGenerationStopped(
                "gen-2", RuntimeGenerationStopCause.STARTUP_FAILURE
            )
        )
        == 1
    )


def test_stop_hook_failure_blocks_successor_after_old_owner_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop", "30-stop.sh")
    children: list[_RestartChild] = []
    submission: RuntimeRestartSubmission | None = None
    semantic_events: list[object] = []

    class RecordingDisplay:
        def emit(self, event: object) -> None:
            semantic_events.append(event)

    monkeypatch.setattr(
        runtime_serve_module,
        "default_runtime_display",
        lambda _settings: RecordingDisplay(),
    )

    def runner(
        _argv: Sequence[str],
        **_kwargs: object,
    ) -> _RestartChild:
        child = _RestartChild([], "old")
        children.append(child)
        return child

    def generation_running(controller: RuntimeController) -> None:
        nonlocal submission
        submission = controller.submit_restart(delivery_expected=False)

    def failing_stop_hooks(
        _plan: RuntimeHookPlan,
        **_kwargs: object,
    ) -> tuple[RuntimeHookResult, ...]:
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "stop", "30-stop.sh"),
                    code="runtime_hook.execution_failed",
                    message="synthetic stop failure",
                ),
            )
        )

    with pytest.raises(RuntimeExecutionError, match="runtime stop hook failed"):
        run_runtime_serve(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,  # type: ignore[arg-type]
            runtime_stop_hook_runner=failing_stop_hooks,  # type: ignore[arg-type]
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
            generation_running=generation_running,
        )

    assert len(children) == 1
    assert children[0].signals == [signal.SIGTERM]
    assert children[0].returncode == -int(signal.SIGTERM)
    assert submission is not None and submission.ticket is not None
    assert submission.ticket.snapshot().state == "failed"
    stop_started = RuntimePhaseStarted(RuntimePhase.STOP_HOOKS)
    stop_failed = RuntimePhaseFailed(RuntimePhase.STOP_HOOKS)
    cleanup_started = RuntimePhaseStarted(RuntimePhase.GENERATION_CLEANUP)
    cleanup_completed = RuntimePhaseCompleted(RuntimePhase.GENERATION_CLEANUP)
    assert stop_started in semantic_events
    assert stop_failed in semantic_events
    assert RuntimePhaseCompleted(RuntimePhase.STOP_HOOKS) not in semantic_events
    assert cleanup_started in semantic_events
    assert cleanup_completed in semantic_events
    stopped = next(
        event
        for event in semantic_events
        if isinstance(event, RuntimeGenerationStopped)
    )
    assert (
        semantic_events.index(stop_started)
        < semantic_events.index(stop_failed)
        < semantic_events.index(cleanup_started)
        < semantic_events.index(cleanup_completed)
        < semantic_events.index(stopped)
    )


# Container shutdown always wins if it arrives between current and replacement.
def test_external_signal_in_generation_gap_suppresses_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    handlers: dict[signal.Signals, object] = {}
    children: list[_RestartChild] = []
    submission: RuntimeRestartSubmission | None = None

    def install_handler(sig: signal.Signals, handler: object) -> object:
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(
        signal, "getsignal", lambda sig: handlers.get(sig, signal.SIG_DFL)
    )
    monkeypatch.setattr(signal, "signal", install_handler)
    original_allocate = RuntimeController.allocate_restart_successor
    original_mark_external = RuntimeController.mark_external_shutdown

    def signal_before_successor(_controller: RuntimeController) -> str | None:
        handler = handlers[signal.SIGINT]
        assert callable(handler)
        handler(signal.SIGINT, None)
        return original_allocate(_controller)

    monkeypatch.setattr(
        RuntimeController,
        "allocate_restart_successor",
        signal_before_successor,
    )

    def repeat_during_outer_cleanup(controller: RuntimeController) -> None:
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        original_mark_external(controller)

    monkeypatch.setattr(
        RuntimeController,
        "mark_external_shutdown",
        repeat_during_outer_cleanup,
    )

    def runner(
        _argv: Sequence[str],
        **_kwargs: object,
    ) -> _RestartChild:
        child = _RestartChild([], "old")
        children.append(child)
        return child

    def generation_running(controller: RuntimeController) -> None:
        nonlocal submission
        submission = controller.submit_restart(delivery_expected=False)

    assert (
        run_runtime_serve(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=tmp_path / "missing-mounted-hooks",
            environ={"PATH": "/usr/bin"},
            runner=runner,  # type: ignore[arg-type]
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
            generation_running=generation_running,
        )
        == 130
    )

    assert len(children) == 1
    assert children[0].signals == [signal.SIGTERM]
    assert submission is not None and submission.ticket is not None
    assert submission.ticket.snapshot().state == "failed"


# A failed replacement is fully cleaned before clients receive its terminal result.
def test_successor_post_start_failure_cleans_exact_owners_before_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = tmp_path / "runtime.toml"
    hooks = tmp_path / "hooks"
    _write_config(config, "old")
    events: list[str] = []
    children: list[_RestartChild] = []
    submission: RuntimeRestartSubmission | None = None
    stop_hook_calls: list[str] = []
    hook_deadlines: list[float] = []

    class ManualClock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += max(0.0, seconds)

    class OwnedAsyncQueue:
        alive = True

        def request_stop(self) -> None:
            events.append("async:stop")

        def request_backend_termination(self, *, deadline: float | None) -> None:
            del deadline

        def terminate_backends(self) -> None:
            events.append("async:force")

        def backend_termination_is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            del timeout
            events.append("async:reap")
            self.alive = False

        def is_alive(self) -> bool:
            return self.alive

    class OwnedSshd:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            events.append("ssh:terminate")
            self.returncode = 0

        def kill(self) -> None:
            events.append("ssh:kill")
            self.returncode = -int(signal.SIGKILL)

        def wait(self) -> int:
            assert self.returncode is not None
            events.append("ssh:reap")
            return self.returncode

    class ActiveHook:
        pid = 4242
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self) -> int:
            assert self.returncode is not None
            events.append("hook:reap")
            return self.returncode

    async_queue = OwnedAsyncQueue()
    active_hook = ActiveHook()
    clock = ManualClock()

    def async_starter(
        _plan: RuntimeFilePlan,
        **kwargs: object,
    ) -> RuntimeAsyncDownloadQueueHandle:
        observer = kwargs["handle_observer"]
        assert callable(observer)
        observer(async_queue)
        events.append("async:start")
        return async_queue  # type: ignore[return-value]

    def ssh_starter(_config: object, **_kwargs: object) -> OwnedSshd:
        events.append("ssh:start")
        return OwnedSshd()

    def terminate_hook(
        process: object,
        *,
        deadline: float,
        **_kwargs: object,
    ) -> object:
        assert process is active_hook
        hook_deadlines.append(deadline)
        events.append("hook:terminate")
        active_hook.returncode = -int(signal.SIGTERM)
        active_hook.wait()
        return object()

    monkeypatch.setattr(
        lifecycle_module,
        "terminate_process_group_until",
        terminate_hook,
    )
    original_publish = RuntimeRestartTicket._publish

    def observe_ticket_publish(
        ticket: RuntimeRestartTicket,
        state: object,
        **kwargs: object,
    ) -> None:
        original_publish(ticket, state, **kwargs)  # type: ignore[arg-type]
        if state == "failed":
            events.append("ticket:failed")

    monkeypatch.setattr(RuntimeRestartTicket, "_publish", observe_ticket_publish)

    def runner(
        argv: Sequence[str],
        **_kwargs: object,
    ) -> _RestartChild:
        marker = "new" if "--new" in argv else "old"
        child = _RestartChild(events, marker)
        children.append(child)
        events.append(f"{marker}:spawn")
        return child

    def startup_hooks(
        _plan: RuntimeHookPlan,
        phase: str,
        **_kwargs: object,
    ) -> tuple[RuntimeHookResult, ...]:
        assert phase == "post-start"
        events.append("new:post-start-fail")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "post-start", "20-new-post.sh"),
                    code="runtime_hook.execution_failed",
                    message="synthetic successor failure",
                ),
            ),
            active_process=active_hook,  # type: ignore[arg-type]
        )

    def stop_hooks(
        _plan: RuntimeHookPlan,
        **_kwargs: object,
    ) -> tuple[RuntimeHookResult, ...]:
        stop_hook_calls.append("called")
        return ()

    def generation_running(controller: RuntimeController) -> None:
        nonlocal submission
        config.write_text(
            """
[comfyui]
extra_args = ["--new"]

[cdh]
shutdown_timeout = 1
default_download_mode = "async"

[system.ssh]
enable = true
password = "secret"

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models/checkpoints"
filename = "model.bin"
""",
            encoding="utf-8",
        )
        _write_hook(hooks, "post-start", "20-new-post.sh")
        _write_hook(hooks, "stop", "30-new-stop-must-not-run.sh")
        submission = controller.submit_restart(delivery_expected=False)

    with pytest.raises(RuntimeExecutionError, match="runtime hook failed"):
        run_runtime_serve(
            runtime=runtime,
            mounted_config_path=config,
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_hooks_path=hooks,
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            environ={"PATH": "/usr/bin"},
            runner=runner,  # type: ignore[arg-type]
            runtime_hook_runner=startup_hooks,  # type: ignore[arg-type]
            runtime_stop_hook_runner=stop_hooks,  # type: ignore[arg-type]
            readiness_waiter=lambda _port, *, child: events.append("new:readiness"),
            runtime_async_queue_starter=async_starter,
            runtime_ssh_starter=ssh_starter,  # type: ignore[arg-type]
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=tmp_path / "control" / "runtime.sock",
            generation_running=generation_running,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert len(children) == 2
    assert children[1].returncode == -int(signal.SIGTERM)
    assert async_queue.alive is False
    assert active_hook.returncode == -int(signal.SIGTERM)
    assert stop_hook_calls == []
    assert hook_deadlines == [1.0]
    assert submission is not None and submission.ticket is not None
    assert submission.ticket.snapshot().state == "failed"
    assert events.index("async:reap") < events.index("ticket:failed")
    assert events.index("ssh:reap") < events.index("ticket:failed")
    assert events.index("new:terminate") < events.index("ticket:failed")
    assert events.index("new:reap") < events.index("ticket:failed")
    assert events.index("hook:reap") < events.index("ticket:failed")


def test_successor_cleanup_precedes_real_terminal_delivery_and_ack(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    endpoint = tmp_path / "control" / "runtime.sock"
    events: list[str] = []
    children: list[_RestartChild] = []
    client_errors: list[BaseException] = []
    client_thread: threading.Thread | None = None

    def runner(
        _argv: Sequence[str],
        **_kwargs: object,
    ) -> _RestartChild:
        name = "old" if not children else "successor"
        child = _RestartChild(events, name)
        children.append(child)
        events.append(f"{name}:spawn")
        return child

    def failing_post_start(
        _plan: RuntimeHookPlan,
        phase: str,
        **_kwargs: object,
    ) -> tuple[RuntimeHookResult, ...]:
        assert phase == "post-start"
        events.append("successor:post-start-fail")
        raise RuntimeHookError(
            (
                Diagnostic(
                    path=("hooks", "mounted", "post-start", "20-fail.sh"),
                    code="runtime_hook.execution_failed",
                    message="synthetic successor failure",
                ),
            )
        )

    def restart_client() -> None:
        peer = None
        try:
            peer = connect_runtime_control(endpoint)
            send_runtime_control_message(peer, RuntimeRestartRequest())
            accepted = receive_runtime_control_response(peer)
            assert accepted == RuntimeAcceptedResponse(operation="op-1")
            events.append("client:accepted")
            terminal = receive_runtime_control_response(peer)
            assert isinstance(terminal, RuntimeTerminalResponse)
            assert terminal.operation == "op-1"
            assert terminal.result == "failed"
            assert terminal.message is not None
            assert "synthetic successor failure" in terminal.message
            events.append("client:terminal")
            send_runtime_control_message(peer, RuntimeAckRequest(operation="op-1"))
            events.append("client:ack")
        except BaseException as error:
            client_errors.append(error)
        finally:
            if peer is not None:
                peer.close()

    def generation_running(_controller: RuntimeController) -> None:
        nonlocal client_thread
        _write_hook(hooks, "post-start", "20-fail.sh")
        client_thread = threading.Thread(target=restart_client)
        client_thread.start()

    with pytest.raises(RuntimeExecutionError, match="runtime hook failed"):
        run_runtime_serve(
            runtime=runtime,
            baked_config_path=tmp_path / "missing-baked.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,  # type: ignore[arg-type]
            runtime_hook_runner=failing_post_start,  # type: ignore[arg-type]
            readiness_waiter=lambda _port, *, child: None,
            runtime_state_path=tmp_path / "state.json",
            control_socket_path=endpoint,
            generation_running=generation_running,
        )

    assert client_thread is not None
    client_thread.join(timeout=2.0)
    assert not client_thread.is_alive()
    assert client_errors == []
    assert len(children) == 2
    assert children[1].returncode == -int(signal.SIGTERM)
    assert events.index("successor:terminate") < events.index("client:terminal")
    assert events.index("successor:reap") < events.index("client:terminal")
    assert events.index("client:terminal") < events.index("client:ack")
