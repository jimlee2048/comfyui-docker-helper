"""End-to-end async runtime download coverage for entrypoint orchestration."""

from __future__ import annotations

import signal
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container import entrypoint as entrypoint_module
from comfyui_docker_helper.container import runtime_files as runtime_files_module
from comfyui_docker_helper.container.download_files import (
    DownloadCancelled,
    DownloaderSettings,
    TransportRequest,
    TransportSuccess,
)
from comfyui_docker_helper.container.entrypoint import run_entrypoint
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeFilePlan,
    RuntimeFilePlanItem,
)
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookPlan,
    RuntimeHookResult,
)
from comfyui_docker_helper.container.runtime_state import load_runtime_state
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
        self.entered = entrypoint_module.threading.Event()
        self.release = entrypoint_module.threading.Event()
        self.block = False
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        self.release.set()

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
    return {entry.target: entry for entry in state.downloads.entries.values()}


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
    runner: entrypoint_module.EntrypointRunner,
    runtime_async_queue_starter: entrypoint_module.RuntimeAsyncQueueStarter
    | None = None,
    runtime_hook_runner: entrypoint_module.RuntimeHookRunner | None = None,
    readiness_waiter: entrypoint_module.ReadinessWaiter | None = None,
) -> int:
    kwargs: dict[str, object] = {}
    if runtime_async_queue_starter is not None:
        kwargs["runtime_async_queue_starter"] = runtime_async_queue_starter
    if runtime_hook_runner is not None:
        kwargs["runtime_hook_runner"] = runtime_hook_runner
    if readiness_waiter is not None:
        kwargs["readiness_waiter"] = readiness_waiter
    return run_entrypoint(
        runtime=runtime,
        baked_config_path=config,
        mounted_config_path=state_path.parent / "missing-mounted.toml",
        environ={},
        runner=runner,
        runtime_state_path=state_path,
        **kwargs,
    )


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
        run_entrypoint(
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

    class CancelOnStopHandle:
        def __init__(
            self,
            handle: entrypoint_module.RuntimeAsyncDownloadQueueHandle,
        ) -> None:
            self.handle = handle

        def request_stop(self) -> None:
            self.handle.request_stop()
            self.handle.terminate_backends()

        def terminate_backends(self) -> None:
            self.handle.terminate_backends()

        def join(self, timeout: float | None = None) -> None:
            self.handle.join(timeout)

        def is_alive(self) -> bool:
            return self.handle.is_alive()

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        log: Logger,
    ) -> CancelOnStopHandle:
        return CancelOnStopHandle(
            entrypoint_module.start_runtime_async_download_queue(
                plan,
                config=config,
                runtime=runtime,
                runtime_state_path=runtime_state_path,
                log=log,
            )
        )

    class ShutdownChild(FakeChild):
        def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls > 1:
                assert self.returncode is not None
                return self.returncode
            assert backend.entered.wait(timeout=1)
            interrupted_entries = _state_by_target(state_path)
            assert interrupted_entries["models/model.bin"].status == "downloading"
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
    assert interrupted_entries["models/model.bin"].status == "downloading"

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


# Scheduling and cleanup coverage keeps missing/stale state behavior and
# cdh-owned staging-file garbage collection tied to the real async queue.
def test_missing_and_stale_state_schedule_async_downloads(
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


# Exhausted-policy coverage pins async failure semantics: ComfyUI keeps running,
# state records exhaustion, and continue/fail controls later queued files.
@pytest.mark.parametrize(
    ("policy", "expected_calls", "expected_statuses"),
    [
        (
            "continue",
            ["a.bin", "a.bin", "b.bin"],
            {"models/a.bin": "exhausted", "models/b.bin": "completed"},
        ),
        (
            "fail",
            ["a.bin", "a.bin"],
            {"models/a.bin": "exhausted", "models/b.bin": "pending"},
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
                    lambda: (
                        {
                            target: entry.status
                            for target, entry in _state_by_target(state_path).items()
                            if target in expected_statuses
                        }
                        == expected_statuses
                    ),
                    timeout=2.5,
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
                        "files": [
                            {
                                "url": "https://example.com/a.bin",
                                "dir": "models",
                                "filename": "a.bin",
                            }
                        ]
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
