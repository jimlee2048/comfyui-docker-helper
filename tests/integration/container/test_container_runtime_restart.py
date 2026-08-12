"""Serial complete-instance runtime restart integration coverage."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from comfyui_docker_helper.config import Diagnostic
from comfyui_docker_helper.container import runtime_lifecycle as lifecycle_module
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
from comfyui_docker_helper.container.runtime_files import Logger, RuntimeFilePlan
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookError,
    RuntimeHookPlan,
    RuntimeHookResult,
)
from comfyui_docker_helper.container.runtime_serve import (
    EntrypointError,
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
) -> None:
    runtime = _runtime(tmp_path)
    config = tmp_path / "runtime.toml"
    hooks = tmp_path / "hooks"
    _write_config(config, "only")
    _write_hook(hooks, "stop", "10-stop.sh")
    events: list[str] = []
    child = _RestartChild(events, "only")
    failure_observer: list[Callable[[str], object]] = []

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

    with pytest.raises(EntrypointError, match="runtime logging failed"):
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


# Restart arbitration must fully stop the current instance before replacement.
def test_restart_replaces_the_complete_generation_without_owner_overlap(
    tmp_path: Path,
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
    ) -> tuple[RuntimeHookResult, ...]:
        del runtime, env, log, cancel_requested
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
) -> None:
    runtime = _runtime(tmp_path)
    config = tmp_path / "runtime.toml"
    _write_config(config, "old")
    children: list[_RestartChild] = []
    submission: RuntimeRestartSubmission | None = None

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

    with pytest.raises(EntrypointError, match="runtime configuration is invalid"):
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


def test_stop_hook_failure_blocks_successor_after_old_owner_cleanup(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop", "30-stop.sh")
    children: list[_RestartChild] = []
    submission: RuntimeRestartSubmission | None = None

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

    with pytest.raises(EntrypointError, match="runtime stop hook failed"):
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
url = "https://example.com/model.bin"
dir = "models/checkpoints"
filename = "model.bin"
""",
            encoding="utf-8",
        )
        _write_hook(hooks, "post-start", "20-new-post.sh")
        _write_hook(hooks, "stop", "30-new-stop-must-not-run.sh")
        submission = controller.submit_restart(delivery_expected=False)

    with pytest.raises(EntrypointError, match="runtime hook failed"):
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

    with pytest.raises(EntrypointError, match="runtime hook failed"):
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
