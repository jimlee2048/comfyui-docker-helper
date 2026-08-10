"""Runtime download owner and lifecycle integration coverage."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container import runtime_files as runtime_files_module
from comfyui_docker_helper.container.download_files import (
    DownloadCancelled,
    DownloaderSettings,
    TransportRequest,
    TransportSuccess,
)
from comfyui_docker_helper.container.process_control import DirectProcessStarter
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_downloads import (
    RuntimeAsyncDownloadQueueHandle,
    RuntimeAsyncQueueStarter,
    start_runtime_async_download_queue,
)
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeFilePlan,
    RuntimeFilePlanItem,
)
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookPlan,
    RuntimeHookResult,
)
from comfyui_docker_helper.container.runtime_lifecycle import (
    EntrypointError,
    ReadinessWaiter,
    RuntimeHookRunner,
)
from comfyui_docker_helper.container.runtime_serve import run_runtime_serve
from comfyui_docker_helper.container.runtime_state import (
    RuntimeState,
    RuntimeStateError,
    load_runtime_state,
    write_runtime_state,
)
from comfyui_docker_helper.container.transfer_core import TransferDownloadFilesError


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
        runtime_files_module,
        "HttpxDownloader",
        lambda *, log: backend,
    )


def _staging_target(item: RuntimeFilePlanItem) -> Path:
    return runtime_files_module.runtime_file_staging_target(item)


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


def _expected_state_is_visible(
    state_path: Path,
    expected_statuses: Mapping[str, str],
) -> bool:
    try:
        entries = _state_by_target(state_path)
    except RuntimeStateError as error:
        if str(error).startswith("runtime state changed during operation:"):
            return False
        raise
    return {
        target: entry.status
        for target, entry in entries.items()
        if target in expected_statuses
    } == expected_statuses


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
) -> int:
    kwargs: dict[str, object] = {}
    if runtime_async_queue_starter is not None:
        kwargs["runtime_async_queue_starter"] = runtime_async_queue_starter
    if runtime_hook_runner is not None:
        kwargs["runtime_hook_runner"] = runtime_hook_runner
    if readiness_waiter is not None:
        kwargs["readiness_waiter"] = readiness_waiter
    return run_runtime_serve(
        runtime=runtime,
        baked_config_path=config,
        mounted_config_path=state_path.parent / "missing-mounted.toml",
        environ={},
        runner=runner,
        runtime_state_path=state_path,
        **kwargs,
    )


# State polling retries only the expected atomic-replacement observation.
def test_state_polling_does_not_hide_persistent_invalid_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")

    with pytest.raises(RuntimeStateError, match="runtime state is invalid"):
        _expected_state_is_visible(state_path, {"models/a.bin": "completed"})


# Mixed-mode scheduling coverage proves mode partitioning keeps declaration
# order within each queue and completes sync files before async acceptance.
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
url = "https://example.com/async-a.bin"
dir = "models"
filename = "async-a.bin"
download_mode = "async"

[[files]]
url = "https://example.com/sync-a.bin"
dir = "models"
filename = "sync-a.bin"
download_mode = "sync"

[[files]]
url = "https://example.com/async-b.bin"
dir = "models"
filename = "async-b.bin"
download_mode = "async"

[[files]]
url = "https://example.com/sync-b.bin"
dir = "models"
filename = "sync-b.bin"
download_mode = "sync"
""",
    )
    state_path = tmp_path / "state.json"
    backend = AsyncBackend()
    _install_async_backend(monkeypatch, backend)
    events: list[str] = []
    async_filenames: list[str] = []

    class AcceptedQueue:
        def request_stop(self) -> None:
            pytest.fail("a completed test queue must not be stopped")

        def terminate_backends(self) -> None:
            pytest.fail("a completed test queue has no active backend")

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return False

    def async_starter(
        plan: RuntimeFilePlan,
        **kwargs: object,
    ) -> AcceptedQueue:
        handle_observer = kwargs.pop("handle_observer")
        cancel_requested = kwargs.pop("cancel_requested")
        assert callable(handle_observer)
        assert callable(cancel_requested)
        del kwargs
        assert [_source_filename(call[0]) for call in backend.calls] == [
            "sync-a.bin",
            "sync-b.bin",
        ]
        events.append("async-accepted")
        async_filenames.extend(item.filename for item in plan.items)
        handle = AcceptedQueue()
        handle_observer(handle)
        return handle

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return FakeChild(0)

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=runner,
            runtime_async_queue_starter=async_starter,
        )
        == 0
    )

    assert async_filenames == ["async-a.bin", "async-b.bin"]
    assert events == ["async-accepted", "spawn"]
    entries = _state_by_target(state_path)
    assert [
        entries[f"models/{name}"].status
        for name in ("sync-a.bin", "sync-b.bin", "async-a.bin", "async-b.bin")
    ] == ["completed", "completed", "pending", "pending"]


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
url = "https://example.com/model.bin"
dir = "models"
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
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del env, log
        assert cancel_requested() is False
        assert [hook.filename for hook in plan.for_phase(phase)] == ["10-post.sh"]
        assert phase == "post-start"
        assert not (runtime.comfyui_path / "models" / "model.bin").exists()
        events.append("post-start")
        backend.release.set()
        return ()

    assert (
        run_runtime_serve(
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
        )
        == 0
    )

    assert events == ["spawn", "readiness", "post-start"]
    assert (runtime.comfyui_path / "models" / "model.bin").read_bytes() == (
        b"async-bytes"
    )
    assert _state_by_target(state_path)["models/model.bin"].status == "completed"


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
url = "https://example.com/model.bin"
dir = "models"
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
        log: Logger,
        handle_observer: Callable[[RuntimeAsyncDownloadQueueHandle], None],
        cancel_requested: Callable[[], bool],
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
            log=log,
            handle_observer=handle_observer,
            cancel_requested=cancel_requested,
        )

    with pytest.raises(
        EntrypointError,
        match="async runtime download queue failed to start",
    ):
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=lambda *_args, **_kwargs: pytest.fail("runner must not start"),
            runtime_async_queue_starter=replace_generation_then_start,
        )

    assert starter_calls == 1


# Async startup suppresses exception-style signal delivery while publishing and
# starting the real handle, then force-stops it before application spawn.
def test_repeated_signal_before_async_acceptance_force_stops_published_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForceObservedBackend(AsyncBackend):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_calls = 0
            self.force_cancel_calls = 0

        def cancel(self, *, deadline: float | None = None) -> None:
            self.cancel_calls += 1
            super().cancel(deadline=deadline)

        def force_cancel(self) -> None:
            self.force_cancel_calls += 1
            AsyncBackend.cancel(self)

        def download(
            self,
            item: TransportRequest,
            settings: DownloaderSettings,
        ) -> TransportSuccess:
            self.calls.append((item, settings))
            self.entered.set()
            self.release.wait(timeout=1)
            if self.cancelled:
                raise DownloadCancelled("cancelled")
            return super().download(item, settings)

    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "async"
default_downloader = "httpx"

[[files]]
url = "https://example.com/model.bin"
dir = "models"
filename = "model.bin"
""",
    )
    state_path = tmp_path / "state.json"
    handlers = _capture_signal_handlers(monkeypatch)
    backend = ForceObservedBackend()
    backend.block = True
    _install_async_backend(monkeypatch, backend)
    original_start = threading.Thread.start
    signal_injected = False

    def signal_between_publication_and_thread_start(thread: threading.Thread) -> None:
        nonlocal signal_injected
        if thread.name == "cdh-runtime-async-downloads":
            original_start(thread)
            assert backend.entered.wait(timeout=1)
            signal_injected = True
            first = handlers[signal.SIGTERM]
            repeated = handlers[signal.SIGINT]
            assert callable(first)
            assert callable(repeated)
            first(signal.SIGTERM, None)
            repeated(signal.SIGINT, None)
            return
        original_start(thread)

    monkeypatch.setattr(
        threading.Thread,
        "start",
        signal_between_publication_and_thread_start,
    )

    assert (
        _run_with_real_async_queue(
            runtime=runtime,
            config=config,
            state_path=state_path,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "ComfyUI must not spawn before async acceptance"
            ),
        )
        == 143
    )

    assert signal_injected is True
    assert backend.cancelled is True
    _eventually(lambda: backend.cancel_calls >= 1)
    assert backend.force_cancel_calls == 1
    _eventually(
        lambda: (
            not any(
                thread.name == "cdh-runtime-async-downloads" and thread.is_alive()
                for thread in threading.enumerate()
            )
        )
    )
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
url = "https://example.com/model.bin"
dir = "models"
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
        def download(
            self,
            item: TransportRequest,
            settings: DownloaderSettings,
        ) -> TransportSuccess:
            self.calls.append((item, settings))
            self.entered.set()
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
url = "https://example.com/model.bin"
dir = "models"
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
        log: Logger,
        handle_observer: Callable[[RuntimeAsyncDownloadQueueHandle], None],
        cancel_requested: Callable[[], bool],
    ) -> RuntimeAsyncDownloadQueueHandle:
        return start_runtime_async_download_queue(
            plan,
            config=config,
            runtime=runtime,
            runtime_state_path=runtime_state_path,
            expected_run_id=expected_run_id,
            log=log,
            handle_observer=handle_observer,
            cancel_requested=cancel_requested,
        )

    class ShutdownChild(FakeChild):
        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls > 1:
                assert self.returncode is not None
                return self.returncode
            assert backend.entered.wait(timeout=1)
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
url = "https://example.com/model.bin"
dir = "models"
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
url = "https://example.com/a.bin"
dir = "models"
filename = "a.bin"

[[files]]
url = "https://example.com/b.bin"
dir = "models"
filename = "b.bin"
""",
    )
    backend = AsyncBackend()
    backend.failures["a.bin"] = None
    backend.payloads["b.bin"] = b"later"
    _install_async_backend(monkeypatch, backend)
    events: list[str] = []

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
            for item in runtime_files_module.build_runtime_file_plan(
                [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
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
            self.deadline: float | None = None

        def cancel(self, *, deadline: float | None = None) -> None:
            self.deadline = deadline
            self.cancel_entered.set()
            self.cancel_release.wait(timeout=1)
            super().cancel(deadline=deadline)

    runtime = _runtime(tmp_path)
    config = _write(
        tmp_path / "runtime.toml",
        """
[cdh]
default_download_mode = "async"
default_downloader = "httpx"
shutdown_timeout = 2.3

[[files]]
url = "https://example.com/model.bin"
dir = "models"
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
        assert backend.cancel_entered.wait(timeout=0.1)
        assert not backend.cancel_release.is_set()
        events.append("stop-hook")
        backend.cancel_release.set()
        return ()

    started = time.monotonic()
    try:
        assert (
            run_runtime_serve(
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

    assert time.monotonic() - started < 0.8
    assert events == ["stop-hook"]
    assert backend.deadline is not None
    _eventually(lambda: backend.cancelled)


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
url = "https://example.com/model.bin"
dir = "models"
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
