"""Runtime download owner and lifecycle integration coverage."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from tests.runtime_event_support import (
    RecordingRuntimeEventSink,
)
from tests.runtime_event_support import (
    run_runtime_generation_once_for_test as run_runtime_generation_once,
)

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container.process.control import DirectProcessStarter
from comfyui_docker_helper.container.process.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime.downloads import (
    RuntimeAsyncDownloadQueueHandle,
    RuntimeAsyncQueueStarter,
    RuntimeAsyncQueueStartupError,
    start_runtime_async_download_queue,
    stop_runtime_async_download_queue,
)
from comfyui_docker_helper.container.runtime.event_delivery import (
    RuntimeBackgroundEventSink,
)
from comfyui_docker_helper.container.runtime.events import (
    RuntimeDownloadItemCompleted,
    RuntimeDownloadQueue,
    RuntimeDownloadQueueState,
    RuntimeDownloadQueueSummary,
    RuntimeDownloadQueueWarning,
    RuntimeDownloadQueueWarningKind,
)
from comfyui_docker_helper.container.runtime.files import (
    download as runtime_file_download_module,
)
from comfyui_docker_helper.container.runtime.files.models import (
    RuntimeFilePlan,
    RuntimeFilePlanItem,
)
from comfyui_docker_helper.container.runtime.files.planning import (
    build_runtime_file_plan,
    runtime_file_staging_target,
)
from comfyui_docker_helper.container.runtime.hooks import (
    RuntimeHookPlan,
    RuntimeHookResult,
)
from comfyui_docker_helper.container.runtime.lifecycle import (
    ReadinessWaiter,
    RuntimeExecutionError,
    RuntimeHookRunner,
)
from comfyui_docker_helper.container.runtime.state import (
    RuntimeState,
    RuntimeStateError,
    load_runtime_state,
    write_runtime_state,
)
from comfyui_docker_helper.container.transfer.core import (
    DownloadCancelled,
    DownloaderSettings,
    TransferDownloadFilesError,
    TransportRequest,
    TransportSuccess,
)
from comfyui_docker_helper.container.transfer.credentials import (
    DownloaderCredentialPolicy,
)


class FakeChild:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self.wait_calls = 0
        self.signals: list[signal.Signals] = []

    def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = self._wait_returncode
        return self._wait_returncode

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)
        self.returncode = -int(sig)

    def terminate(self) -> None:
        self.returncode = self._wait_returncode


class AsyncBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[TransportRequest, DownloaderSettings]] = []
        self.payloads: dict[str, bytes] = {}
        self.failures: dict[str, int | None] = {}
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.cancelled = False

    def cancel(self, *, deadline: float | None = None) -> None:
        del deadline
        self.cancelled = True
        self.release.set()

    def force_cancel(self) -> None:
        self.cancel()

    def download(
        self,
        item: TransportRequest,
        settings: DownloaderSettings,
    ) -> TransportSuccess:
        self.calls.append((item, settings))
        self.entered.set()
        if self.block:
            self.release.wait(timeout=1)
        filename = _source_filename(item)
        remaining = self.failures.get(filename, 0)
        if remaining is None or remaining > 0:
            if remaining is not None:
                self.failures[filename] = remaining - 1
            with item.sink.open_for_write() as output:
                output.write(b"partial")
            raise TransferDownloadFilesError(f"failed {filename}")
        payload = self.payloads.get(filename, b"downloaded")
        with item.sink.open_for_write() as output:
            output.write(payload)
        return TransportSuccess(length=len(payload), namespace="httpx", http_status=200)


def _runtime(tmp_path: Path) -> ContainerRuntime:
    runtime = ContainerRuntime(
        workspace=tmp_path / "workspace",
        comfyui_path=tmp_path / "workspace" / "ComfyUI",
        virtual_env=tmp_path / "venv",
    )
    runtime.comfyui_path.mkdir(parents=True)
    return runtime


def _write(path: Path, document: str) -> Path:
    path.write_text(document, encoding="utf-8")
    return path


def _write_hook(root: Path, phase: str, filename: str) -> Path:
    phase_dir = root / f"{phase}.d"
    phase_dir.mkdir(parents=True, exist_ok=True)
    path = phase_dir / filename
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


def _install_async_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: AsyncBackend,
) -> None:
    monkeypatch.setattr(
        runtime_file_download_module,
        "HttpxDownloader",
        lambda: backend,
    )


def _staging_target(item: RuntimeFilePlanItem) -> Path:
    return runtime_file_staging_target(item)


def _source_filename(request: TransportRequest) -> str:
    return Path(urlsplit(request.url).path).name


def _capture_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[signal.Signals, object]:
    handlers: dict[signal.Signals, object] = {}

    def fake_getsignal(sig: signal.Signals) -> object:
        return f"previous-{signal.Signals(sig).name}"

    def fake_signal(sig: signal.Signals, handler: object) -> object:
        handlers[signal.Signals(sig)] = handler
        return f"previous-{signal.Signals(sig).name}"

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)
    return handlers


def _state_by_target(state_path: Path):
    state = load_runtime_state(state_path)
    return {entry.target: entry for entry in state.downloads.values()}


def _eventually(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def _run_with_real_async_queue(
    *,
    runtime: ContainerRuntime,
    config: Path,
    state_path: Path,
    runner: DirectProcessStarter,
    runtime_async_queue_starter: RuntimeAsyncQueueStarter | None = None,
    runtime_hook_runner: RuntimeHookRunner | None = None,
    readiness_waiter: ReadinessWaiter | None = None,
    background_event_sink: object | None = None,
) -> int:
    kwargs: dict[str, object] = {}
    if runtime_async_queue_starter is not None:
        kwargs["runtime_async_queue_starter"] = runtime_async_queue_starter
    if runtime_hook_runner is not None:
        kwargs["runtime_hook_runner"] = runtime_hook_runner
    if readiness_waiter is not None:
        kwargs["readiness_waiter"] = readiness_waiter
    if background_event_sink is not None:
        kwargs["background_event_sink"] = background_event_sink
    return run_runtime_generation_once(
        runtime=runtime,
        baked_config_path=config,
        mounted_config_path=state_path.parent / "missing-mounted.toml",
        environ={},
        runner=runner,
        runtime_state_path=state_path,
        **kwargs,
    )


# The main-thread queue owner reports force escalation without raw timing or
# backend detail when semantic Runtime presentation is active.
def test_forced_async_queue_stop_emits_one_controlled_warning() -> None:
    events: list[object] = []

    class Recorder:
        def emit(self, event: object, /) -> None:
            events.append(event)

    class ActiveQueue:
        alive = True

        def request_stop(self) -> None:
            return

        def request_backend_termination(self, *, deadline: float | None) -> None:
            del deadline

        def terminate_backends(self) -> None:
            self.alive = False

        def backend_termination_is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return self.alive

    assert (
        stop_runtime_async_download_queue(
            ActiveQueue(),
            cancel_requested=lambda: True,
            event_sink=Recorder(),
        )
        is False
    )
    assert events == [
        RuntimeDownloadQueueWarning(
            RuntimeDownloadQueueWarningKind.FORCE_TERMINATION_REQUIRED
        )
    ]


# Mixed-mode scheduling coverage proves mode partitioning keeps declaration
# order within each queue, completes sync files before async acceptance, and
# executes the accepted async queue in declaration order.
def test_mixed_runtime_downloads_preserve_queue_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_downloader = "httpx"

[[files]]
type = "http"
url = "https://example.com/async-a.bin"
target_dir = "models"
filename = "async-a.bin"
download_mode = "async"

[[files]]
type = "http"
url = "https://example.com/sync-a.bin"
target_dir = "models"
filename = "sync-a.bin"
download_mode = "sync"

[[files]]
type = "http"
url = "https://example.com/async-b.bin"
target_dir = "models"
filename = "async-b.bin"
download_mode = "async"

[[files]]
type = "http"
url = "https://example.com/sync-b.bin"
target_dir = "models"
filename = "sync-b.bin"
download_mode = "sync"
""",
    )
    state_path = tmp_path / "state.json"
    backend = AsyncBackend()
    _install_async_backend(monkeypatch, backend)
    presentation = RecordingRuntimeEventSink()

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell

        class Child(FakeChild):
            def wait(self) -> int:
                _eventually(lambda: len(backend.calls) == 4)
                _eventually(
                    lambda: all(
                        (runtime.comfyui_path / "models" / filename).is_file()
                        for filename in (
                            "sync-a.bin",
                            "sync-b.bin",
                            "async-a.bin",
                            "async-b.bin",
                        )
                    )
                )
                return super().wait()

        return Child(0)

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=runner,
            background_event_sink=presentation,
        )
        == 0
    )

    assert [_source_filename(call[0]) for call in backend.calls] == [
        "sync-a.bin",
        "sync-b.bin",
        "async-a.bin",
        "async-b.bin",
    ]
    queue_states = [
        (event.queue, event.state)
        for event in presentation.events
        if isinstance(event, RuntimeDownloadQueueSummary)
    ]
    assert queue_states == [
        (
            RuntimeDownloadQueue.SYNCHRONOUS,
            RuntimeDownloadQueueState.ACCEPTED,
        ),
        (
            RuntimeDownloadQueue.SYNCHRONOUS,
            RuntimeDownloadQueueState.COMPLETED,
        ),
        (
            RuntimeDownloadQueue.ASYNCHRONOUS,
            RuntimeDownloadQueueState.ACCEPTED,
        ),
        (
            RuntimeDownloadQueue.ASYNCHRONOUS,
            RuntimeDownloadQueueState.COMPLETED,
        ),
    ]
    entries = _state_by_target(state_path)
    assert [
        entries[f"models/{name}"].status
        for name in ("sync-a.bin", "sync-b.bin", "async-a.bin", "async-b.bin")
    ] == ["completed", "completed", "completed", "completed"]


# Async queue acceptance coverage proves startup hooks and readiness are not
# blocked by in-flight downloads, while completion still updates final state.
def test_actual_async_queue_acceptance_does_not_block_startup_hooks_or_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "async"
default_downloader = "httpx"

[comfyui]
port = 8299

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "post-start", "10-post.sh")
    state_path = tmp_path / "state.json"
    backend = AsyncBackend()
    backend.block = True
    backend.payloads["model.bin"] = b"async-bytes"
    _install_async_backend(monkeypatch, backend)
    events: list[str] = []
    presentation = RecordingRuntimeEventSink()

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        assert backend.entered.wait(timeout=1)
        events.append("spawn")

        class Child(FakeChild):
            def wait(self) -> int:
                _eventually(
                    lambda: (runtime.comfyui_path / "models" / "model.bin").is_file()
                )
                return super().wait()

        return Child(0)

    def readiness_waiter(port: int, *, child: FakeChild) -> None:
        assert port == 8299
        assert child.poll() is None
        assert not (runtime.comfyui_path / "models" / "model.bin").exists()
        events.append("readiness")

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        cancel_requested: Callable[[], bool],
        event_sink: object,
    ) -> tuple[RuntimeHookResult, ...]:
        del env, event_sink
        assert cancel_requested() is False
        assert [hook.filename for hook in plan.for_phase(phase)] == ["10-post.sh"]
        assert phase == "post-start"
        assert not (runtime.comfyui_path / "models" / "model.bin").exists()
        events.append("post-start")
        backend.release.set()
        return ()

    assert (
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=config,
            mounted_config_path=tmp_path / "missing-mounted.toml",
            baked_hooks_path=tmp_path / "missing-baked-hooks",
            mounted_hooks_path=hooks,
            environ={},
            runner=runner,
            runtime_state_path=state_path,
            runtime_hook_runner=runtime_hook_runner,
            readiness_waiter=readiness_waiter,
            background_event_sink=presentation,
        )
        == 0
    )

    assert events == ["spawn", "readiness", "post-start"]
    assert (runtime.comfyui_path / "models" / "model.bin").read_bytes() == (
        b"async-bytes"
    )
    assert _state_by_target(state_path)["models/model.bin"].status == "completed"
    queue_states = [
        event.state
        for event in presentation.events
        if isinstance(event, RuntimeDownloadQueueSummary)
    ]
    assert queue_states == [
        RuntimeDownloadQueueState.ACCEPTED,
        RuntimeDownloadQueueState.COMPLETED,
    ]
    assert any(
        isinstance(event, RuntimeDownloadItemCompleted) for event in presentation.events
    )


def test_async_queue_rejects_replaced_start_generation_before_thread(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "async"
default_downloader = "httpx"

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )
    state_path = tmp_path / "state.json"
    starter_calls = 0

    def replace_generation_then_start(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        expected_run_id: str,
        handle_observer: Callable[[RuntimeAsyncDownloadQueueHandle], None],
        cancel_requested: Callable[[], bool],
        event_sink: RuntimeBackgroundEventSink,
    ):
        nonlocal starter_calls
        starter_calls += 1
        state = load_runtime_state(runtime_state_path)
        write_runtime_state(
            runtime_state_path,
            RuntimeState(
                schema_version=state.schema_version,
                run_id="replaced-generation",
                downloads=state.downloads,
            ),
        )
        return start_runtime_async_download_queue(
            plan,
            config=config,
            runtime=runtime,
            runtime_state_path=runtime_state_path,
            expected_run_id=expected_run_id,
            handle_observer=handle_observer,
            cancel_requested=cancel_requested,
            event_sink=event_sink,
        )

    with pytest.raises(RuntimeExecutionError) as raised:
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=lambda *_args, **_kwargs: pytest.fail("runner must not start"),
            runtime_async_queue_starter=replace_generation_then_start,
        )

    assert str(raised.value) == "async runtime download queue failed to start"
    startup_error = raised.value.__cause__
    assert isinstance(startup_error, RuntimeAsyncQueueStartupError)
    assert isinstance(startup_error.__cause__, RuntimeStateError)
    assert starter_calls == 1


# Async startup publishes its typed handle before its worker can be accepted;
# repeated signals force-stop that published queue before application spawn.
def test_repeated_signal_before_async_acceptance_force_stops_published_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PublishedQueue:
        def __init__(self) -> None:
            self.alive = True
            self.accepted = False
            self.stop_requested = False
            self.force_requested = False
            self.call_trace: list[str] = []
            self.request_stop_calls = 0
            self.request_backend_termination_calls = 0
            self.terminate_backends_calls = 0

        def request_stop(self) -> None:
            self.call_trace.append("request_stop")
            self.request_stop_calls += 1
            self.stop_requested = True

        def request_backend_termination(self, *, deadline: float | None) -> None:
            del deadline
            self.call_trace.append("request_backend_termination")
            self.request_backend_termination_calls += 1

        def terminate_backends(self) -> None:
            self.call_trace.append("terminate_backends")
            self.terminate_backends_calls += 1
            self.force_requested = True
            self.alive = False

        def backend_termination_is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            self.call_trace.append("join")
            if self.alive and timeout not in (0, 0.0):
                pytest.fail("queue joined before repeated-signal force escalation")

        def is_alive(self) -> bool:
            if self.alive and self.stop_requested and not self.force_requested:
                pytest.fail("ordinary queue fallback reached before force escalation")
            return self.alive

        def wait_until_stopped(
            self,
            *,
            timeout: float,
            poll_interval: float,
            monotonic: Callable[[], float] = time.monotonic,
        ) -> bool:
            del timeout, poll_interval, monotonic
            return not self.alive

    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "async"
default_downloader = "httpx"

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )
    state_path = tmp_path / "state.json"
    handlers = _capture_signal_handlers(monkeypatch)
    published: PublishedQueue | None = None

    def publish_then_signal(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        expected_run_id: str,
        handle_observer: Callable[[RuntimeAsyncDownloadQueueHandle], None],
        cancel_requested: Callable[[], bool],
        credential_policy: DownloaderCredentialPolicy | None = None,
        event_sink: RuntimeBackgroundEventSink,
    ) -> RuntimeAsyncDownloadQueueHandle:
        del (
            plan,
            config,
            runtime,
            runtime_state_path,
            expected_run_id,
            cancel_requested,
            credential_policy,
            event_sink,
        )
        nonlocal published
        queue = PublishedQueue()
        published = queue
        handle_observer(queue)
        first = handlers[signal.SIGTERM]
        repeated = handlers[signal.SIGINT]
        assert callable(first)
        assert callable(repeated)
        first(signal.SIGTERM, None)
        repeated(signal.SIGINT, None)
        return queue

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "ComfyUI must not spawn before async acceptance"
            ),
            runtime_async_queue_starter=publish_then_signal,
        )
        == 143
    )

    assert published is not None
    assert published.accepted is False
    force_index = published.call_trace.index("terminate_backends")
    assert published.call_trace.index("request_stop") < force_index
    assert published.call_trace.index("request_backend_termination") < force_index
    assert published.request_stop_calls >= 1
    assert published.request_backend_termination_calls >= 1
    assert published.terminate_backends_calls >= 1
    assert published.is_alive() is False
    assert not (runtime.comfyui_path / "models" / "model.bin").exists()
    assert _state_by_target(state_path)["models/model.bin"].status != "completed"


# Synchronous activation publishes its exact backend before execution, so a
# startup signal can cancel and quiesce that operation before any hook or child.
def test_signal_cancels_published_synchronous_backend_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """[cdh]
default_download_mode = "sync"
default_downloader = "httpx"

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )
    state_path = tmp_path / "state.json"
    handlers = _capture_signal_handlers(monkeypatch)

    class SignalThenBlockBackend(AsyncBackend):
        cancel_calls = 0

        def cancel(self, *, deadline: float | None = None) -> None:
            assert deadline is not None
            self.cancel_calls += 1
            super().cancel(deadline=deadline)

        def download(
            self,
            item: TransportRequest,
            settings: DownloaderSettings,
        ) -> TransportSuccess:
            self.calls.append((item, settings))
            self.entered.set()
            handler = handlers[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)
            self.release.wait(timeout=2)
            if self.cancelled:
                raise DownloadCancelled("cancelled")
            raise AssertionError("the synchronous backend was not cancelled")

    backend = SignalThenBlockBackend()
    _install_async_backend(monkeypatch, backend)

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "ComfyUI must not start during synchronous cancellation"
            ),
        )
        == 143
    )
    assert backend.cancel_calls == 1
    assert backend.cancelled is True
    assert not (runtime.comfyui_path / "models" / "model.bin").exists()


# Restart coverage protects staging isolation: interrupted async downloads remain
# resumable without exposing partial files at their final targets.
def test_interrupted_async_download_restarts_without_exposing_partial_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingUntilCancelledBackend(AsyncBackend):
        def __init__(self) -> None:
            super().__init__()
            self.partial_path: Path | None = None
            self.partial_written = threading.Event()

        def download(
            self,
            item: TransportRequest,
            settings: DownloaderSettings,
        ) -> TransportSuccess:
            self.calls.append((item, settings))
            self.entered.set()
            self.partial_path = item.sink.display_path
            with item.sink.open_for_write() as output:
                output.write(b"partial")
            self.partial_written.set()
            self.release.wait(timeout=1)
            if self.cancelled:
                raise DownloadCancelled("cancelled")
            payload = self.payloads.get(_source_filename(item), b"downloaded")
            with item.sink.open_for_write() as output:
                output.write(payload)
            return TransportSuccess(
                length=len(payload), namespace="httpx", http_status=200
            )

    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "async"
default_downloader = "httpx"

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )
    state_path = tmp_path / "state.json"
    backend = BlockingUntilCancelledBackend()
    _install_async_backend(monkeypatch, backend)
    handlers = _capture_signal_handlers(monkeypatch)
    first_child: FakeChild | None = None

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        expected_run_id: str,
        handle_observer: Callable[[RuntimeAsyncDownloadQueueHandle], None],
        cancel_requested: Callable[[], bool],
        event_sink: RuntimeBackgroundEventSink,
    ) -> RuntimeAsyncDownloadQueueHandle:
        return start_runtime_async_download_queue(
            plan,
            config=config,
            runtime=runtime,
            runtime_state_path=runtime_state_path,
            expected_run_id=expected_run_id,
            handle_observer=handle_observer,
            cancel_requested=cancel_requested,
            event_sink=event_sink,
        )

    class ShutdownChild(FakeChild):
        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls > 1:
                assert self.returncode is not None
                return self.returncode
            assert backend.entered.wait(timeout=1)
            assert backend.partial_written.wait(timeout=1)
            assert backend.partial_path is not None
            assert backend.partial_path.read_bytes() == b"partial"
            assert not (runtime.comfyui_path / "models" / "model.bin").exists()
            interrupted_entries = _state_by_target(state_path)
            assert interrupted_entries["models/model.bin"].status == "pending"
            handler = handlers[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)
            raise AssertionError("shutdown signal handler should interrupt wait")

    def first_runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        nonlocal first_child
        del argv, cwd, env, shell
        first_child = ShutdownChild()
        return first_child

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=first_runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
        )
        == 143
    )

    assert first_child is not None
    assert first_child.signals == [signal.SIGTERM]
    assert backend.cancelled is True
    assert not (runtime.comfyui_path / "models" / "model.bin").exists()
    assert backend.partial_path is not None
    assert not backend.partial_path.exists()
    interrupted_entries = _state_by_target(state_path)
    assert interrupted_entries["models/model.bin"].status == "pending"

    resumed = AsyncBackend()
    resumed.payloads["model.bin"] = b"resumed"
    _install_async_backend(monkeypatch, resumed)

    def second_runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell

        class Child(FakeChild):
            def wait(self) -> int:
                _eventually(
                    lambda: (runtime.comfyui_path / "models" / "model.bin").is_file()
                )
                return super().wait()

        return Child(0)

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=second_runner,
        )
        == 0
    )

    assert (runtime.comfyui_path / "models" / "model.bin").read_bytes() == b"resumed"
    completed_entries = _state_by_target(state_path)
    assert completed_entries["models/model.bin"].status == "completed"


# Restart coverage proves that a completed entry with a missing final is
# rescheduled through the real async queue.
def test_missing_completed_final_schedules_async_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "async"
default_downloader = "httpx"

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
overwrite = true
""",
    )
    state_path = tmp_path / "state.json"
    backend = AsyncBackend()
    backend.payloads["model.bin"] = b"first"
    _install_async_backend(monkeypatch, backend)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell

        class Child(FakeChild):
            def wait(self) -> int:
                _eventually(
                    lambda: (runtime.comfyui_path / "models" / "model.bin").is_file()
                )
                return super().wait()

        return Child(0)

    assert not state_path.exists()
    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=runner,
        )
        == 0
    )
    assert state_path.exists()
    assert backend.calls
    first_entries = _state_by_target(state_path)
    assert first_entries["models/model.bin"].status == "completed"
    assert (runtime.comfyui_path / "models" / "model.bin").read_bytes() == b"first"

    (runtime.comfyui_path / "models" / "model.bin").unlink()
    retry_backend = AsyncBackend()
    retry_backend.payloads["model.bin"] = b"second"
    _install_async_backend(monkeypatch, retry_backend)

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=runner,
        )
        == 0
    )

    assert [_source_filename(call[0]) for call in retry_backend.calls] == ["model.bin"]
    assert (runtime.comfyui_path / "models" / "model.bin").read_bytes() == b"second"
    assert _state_by_target(state_path)["models/model.bin"].status == "completed"


# Exhausted-policy coverage pins current-run behavior while persisted recovery
# state remains pending for the next start.
@pytest.mark.parametrize(
    ("policy", "expected_calls", "expected_statuses"),
    [
        (
            "continue",
            ["a.bin", "a.bin", "b.bin"],
            {"models/a.bin": "pending", "models/b.bin": "completed"},
        ),
        (
            "fail",
            ["a.bin", "a.bin"],
            {"models/a.bin": "pending", "models/b.bin": "pending"},
        ),
    ],
)
def test_async_exhausted_policy_keeps_comfyui_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
    expected_calls: list[str],
    expected_statuses: dict[str, str],
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / f"{policy}.toml",
        f"""
[cdh]
default_download_mode = "async"
default_downloader = "httpx"
download_max_attempts = 2
download_failure_policy = "{policy}"

[[files]]
type = "http"
url = "https://example.com/a.bin"
target_dir = "models"
filename = "a.bin"

[[files]]
type = "http"
url = "https://example.com/b.bin"
target_dir = "models"
filename = "b.bin"
""",
    )
    backend = AsyncBackend()
    backend.failures["a.bin"] = None
    backend.payloads["b.bin"] = b"later"
    _install_async_backend(monkeypatch, backend)
    events: list[str] = []
    presentation = RecordingRuntimeEventSink()

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")

        class Child(FakeChild):
            def wait(self) -> int:
                assert backend.entered.wait(timeout=1)
                _eventually(
                    lambda: len(backend.calls) == len(expected_calls),
                    timeout=2.5,
                )
                if policy == "continue":
                    _eventually(
                        lambda: (runtime.comfyui_path / "models" / "b.bin").is_file()
                    )
                return super().wait()

        return Child(0)

    state_path = tmp_path / f"{policy}-state.json"
    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=runner,
            background_event_sink=presentation,
        )
        == 0
    )

    assert events == ["spawn"]
    assert [_source_filename(call[0]) for call in backend.calls] == expected_calls
    entries = _state_by_target(state_path)
    assert {target: entries[target].status for target in expected_statuses} == (
        expected_statuses
    )
    assert not (runtime.comfyui_path / "models" / "a.bin").exists()
    assert not _staging_target(
        next(
            item
            for item in build_runtime_file_plan(
                [
                    {
                        "type": "http",
                        "url": "https://example.com/a.bin",
                        "target_dir": "models",
                        "filename": "a.bin",
                    }
                ],
                comfyui_path=runtime.comfyui_path,
                default_download_mode="async",
            ).items
            if item.filename == "a.bin"
        )
    ).exists()
    if policy == "continue":
        assert (runtime.comfyui_path / "models" / "b.bin").read_bytes() == b"later"
    else:
        assert not (runtime.comfyui_path / "models" / "b.bin").exists()
    queue_warnings = [
        event.kind
        for event in presentation.events
        if isinstance(event, RuntimeDownloadQueueWarning)
    ]
    assert queue_warnings == (
        [RuntimeDownloadQueueWarningKind.STOPPED_AFTER_FAILURE]
        if policy == "fail"
        else []
    )


# Backend teardown may be slow, but the first signal must still reach stop hooks
# promptly.
def test_signal_shutdown_does_not_wait_for_blocking_backend_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingCancellationBackend(AsyncBackend):
        def __init__(self) -> None:
            super().__init__()
            self.block = True
            self.cancel_entered = threading.Event()
            self.cancel_release = threading.Event()
            self.cancel_completed = threading.Event()
            self.deadline: float | None = None

        def cancel(self, *, deadline: float | None = None) -> None:
            self.deadline = deadline
            self.cancel_entered.set()
            try:
                self.cancel_release.wait(timeout=1)
                super().cancel(deadline=deadline)
            finally:
                self.cancel_completed.set()

    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "async"
default_downloader = "httpx"
shutdown_timeout = 2.3

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop", "10-stop.sh")
    state_path = tmp_path / "state.json"
    backend = BlockingCancellationBackend()
    _install_async_backend(monkeypatch, backend)
    handlers = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    class ShutdownChild(FakeChild):
        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                assert backend.entered.wait(timeout=1)
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                raise AssertionError("shutdown handler should interrupt wait")
            assert self.returncode is not None
            return self.returncode

    def stop_hooks(*_args: object, **_kwargs: object) -> tuple[RuntimeHookResult, ...]:
        assert backend.cancel_entered.wait(timeout=1)
        assert not backend.cancel_completed.is_set()
        events.append("stop-hook")
        backend.cancel_release.set()
        return ()

    try:
        assert (
            run_runtime_generation_once(
                runtime=runtime,
                baked_config_path=config,
                mounted_config_path=tmp_path / "missing-mounted.toml",
                baked_hooks_path=tmp_path / "missing-baked-hooks",
                mounted_hooks_path=hooks,
                environ={},
                runner=lambda *_args, **_kwargs: ShutdownChild(),
                runtime_state_path=state_path,
                runtime_stop_hook_runner=stop_hooks,  # type: ignore[arg-type]
            )
            == 143
        )
    finally:
        backend.cancel_release.set()

    assert events == ["stop-hook"]
    assert backend.deadline is not None
    assert backend.cancel_completed.wait(timeout=1)
    assert backend.cancelled is True


# Cross-start accounting coverage proves each start owns its complete in-memory
# attempt budget while persisted recovery state remains actionable.
def test_sync_attempt_budget_is_owned_by_each_container_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "sync"
default_downloader = "httpx"
download_max_attempts = 2
download_failure_policy = "continue"

[[files]]
type = "http"
url = "https://example.com/model.bin"
target_dir = "models"
filename = "model.bin"
""",
    )
    state_path = tmp_path / "state.json"
    backend = AsyncBackend()
    backend.failures["model.bin"] = None
    _install_async_backend(monkeypatch, backend)

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        return FakeChild(0)

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=runner,
        )
        == 0
    )
    first = load_runtime_state(state_path)
    first_entry = next(iter(first.downloads.values()))
    assert len(backend.calls) == 2
    assert first_entry.status == "pending"

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=runner,
        )
        == 0
    )
    second = load_runtime_state(state_path)
    second_entry = next(iter(second.downloads.values()))
    assert len(backend.calls) == 4
    assert second.run_id != first.run_id
    assert second_entry.status == "pending"
