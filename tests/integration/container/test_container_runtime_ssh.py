"""Runtime SSH service owner and lifecycle integration coverage."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pytest

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container import ssh as ssh_module
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_events import (
    RuntimeSshOutcome,
    RuntimeSshStatus,
    RuntimeSshWarning,
    RuntimeSshWarningKind,
)
from comfyui_docker_helper.container.runtime_files import (
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
)
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookPlan,
    RuntimeHookResult,
)
from comfyui_docker_helper.container.runtime_serve import (
    RuntimeExecutionError,
)
from comfyui_docker_helper.container.runtime_ssh_service import (
    RuntimeSshService,
    RuntimeSshServiceError,
    stop_runtime_ssh_service,
)
from comfyui_docker_helper.container.ssh import (
    SshCredentialPreparationError,
    SshdConfigPreparationError,
    SshdConfigValidationError,
    SshdReadinessError,
    SshdStartupError,
    SshEnvironmentProjectionError,
    SshPreparationWarningKind,
    start_sshd_if_enabled,
)
from tests.runtime_event_support import (
    RecordingRuntimeEventSink,
)
from tests.runtime_event_support import (
    run_runtime_generation_once_for_test as run_runtime_generation_once,
)

VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "test@example"
)
SECOND_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg "
    "second@example"
)

_PREPARATION_PROCESS_TIMEOUT_SECONDS = 10
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 10


@pytest.fixture
def owned_preparation_processes() -> Iterator[list[subprocess.Popen[bytes]]]:
    processes: list[subprocess.Popen[bytes]] = []
    yield processes

    cleanup_failures = 0
    for process in processes:
        if process.poll() is not None:
            continue
        with suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                process.kill()
            try:
                process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                cleanup_failures += 1

    if cleanup_failures:
        pytest.fail(
            "test-owned SSH preparation process cleanup did not complete",
            pytrace=False,
        )


class FakeChild:
    """Minimal ComfyUI process handle for runtime integration tests."""

    def __init__(
        self,
        returncode: int = 0,
        *,
        events: list[str] | None = None,
        handlers: Mapping[signal.Signals, object] | None = None,
        shutdown_signal: signal.Signals | None = None,
    ) -> None:
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self._events = events
        self._handlers = handlers
        self._shutdown_signal = shutdown_signal
        self.signals: list[signal.Signals] = []
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def wait(self) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1 and self._shutdown_signal is not None:
            assert self._handlers is not None
            handler = self._handlers[self._shutdown_signal]
            assert callable(handler)
            handler(self._shutdown_signal, None)
            raise AssertionError("shutdown handler should interrupt wait")
        if self._events is not None:
            self._events.append("wait")
        self.returncode = self._wait_returncode
        return self._wait_returncode

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)
        if self._events is not None:
            self._events.append(f"signal:{sig.name}")
        if self._wait_returncode == -int(sig):
            self.returncode = self._wait_returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._wait_returncode

    def kill(self) -> None:
        self.killed = True
        if self._events is not None:
            self._events.append("kill")
        self.returncode = -int(signal.SIGKILL)


class WaitForEventChild(FakeChild):
    """ComfyUI child that stays alive until an external event is observed."""

    def __init__(
        self,
        event: threading.Event,
        *,
        returncode: int = 0,
    ) -> None:
        super().__init__(returncode)
        self._event = event

    def wait(self) -> int:
        assert self._event.wait(timeout=1)
        return super().wait()


class FakeSshdProcess:
    """Minimal sshd process handle with deterministic stop/monitor behavior."""

    def __init__(
        self,
        *,
        returncode: int | None = None,
        wait_returncode: int | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.returncode = returncode
        self._wait_returncode = wait_returncode
        self._events = events
        self.waited = threading.Event()
        self.released = threading.Event()

    def wait(self) -> int:
        if self._wait_returncode is None:
            self.released.wait(timeout=1)
            self.returncode = 0 if self.returncode is None else self.returncode
        else:
            self.returncode = self._wait_returncode
        self.waited.set()
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        if self._events is not None:
            self._events.append("ssh-stop")
        self.returncode = 0
        self.released.set()

    def kill(self) -> None:
        if self._events is not None:
            self._events.append("ssh-kill")
        self.returncode = -int(signal.SIGKILL)
        self.released.set()


class FakeAsyncHandle:
    """Minimal async download queue handle used for shutdown ordering."""

    def __init__(self, events: list[str], *, alive: bool = True) -> None:
        self._events = events
        self._alive = alive

    def request_stop(self) -> None:
        self._events.append("async-stop")

    def terminate_backends(self) -> None:
        self._events.append("async-terminate-backends")

    def request_backend_termination(self, *, deadline: float | None) -> None:
        del deadline
        self.terminate_backends()

    def backend_termination_is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self._events.append("async-join")
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


@dataclass(frozen=True, slots=True)
class SensitiveCommandCall:
    argv: list[str]
    input_data: bytes
    description: str


@dataclass(frozen=True, slots=True)
class PlainCommandCall:
    argv: list[str]
    description: str


class RecordingSensitiveCommandRunner:
    def __init__(self) -> None:
        self.calls: list[SensitiveCommandCall] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input_data: bytes,
        description: str,
    ) -> int:
        self.calls.append(
            SensitiveCommandCall(
                argv=list(argv),
                input_data=input_data,
                description=description,
            )
        )
        return 0


class RecordingCommandRunner:
    def __init__(self) -> None:
        self.calls: list[PlainCommandCall] = []

    def __call__(self, argv: Sequence[str], *, description: str) -> int:
        self.calls.append(PlainCommandCall(argv=list(argv), description=description))
        return 0


class RecordingProcessStarter:
    def __init__(self, process: FakeSshdProcess) -> None:
        self.process = process
        self.calls: list[PlainCommandCall] = []

    def __call__(self, argv: Sequence[str], *, description: str) -> FakeSshdProcess:
        self.calls.append(PlainCommandCall(argv=list(argv), description=description))
        return self.process


class OwnershipRecorder:
    def __init__(self) -> None:
        self.chown_calls: list[tuple[Path, int, int]] = []
        self.chmod_calls: list[tuple[Path, int]] = []
        self.fchown_calls: list[tuple[int, int]] = []
        self.fchmod_calls: list[int] = []

    def chown(self, path: str | Path, uid: int, gid: int) -> None:
        self.chown_calls.append((Path(path), uid, gid))

    def chmod(self, path: str | Path, mode: int) -> None:
        target = Path(path)
        self.chmod_calls.append((target, mode))
        target.chmod(mode)

    def fchown(self, descriptor: int, uid: int, gid: int) -> None:
        del descriptor
        self.fchown_calls.append((uid, gid))

    def fchmod(self, descriptor: int, mode: int) -> None:
        self.fchmod_calls.append(mode)
        os.fchmod(descriptor, mode)


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


def _missing_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / f"missing-{name}"


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


# Default/inactive SSH coverage ensures normal runtime startup is untouched.
def test_default_inactive_ssh_does_not_call_starter_and_spawns(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
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
        return FakeChild(0)

    def runtime_ssh_starter(
        config: RuntimeConfig,
        **_kwargs: object,
    ) -> FakeSshdProcess:
        del config
        events.append("ssh-start")
        return FakeSshdProcess()

    assert (
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_ssh_starter=runtime_ssh_starter,
        )
        == 0
    )

    assert events == ["spawn"]


# Activation coverage verifies SSH starts after sync/pre-start and before async/spawn.
def test_runtime_enabled_ssh_starts_after_sync_and_pre_start_before_async_and_spawn(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    baked = _write(
        tmp_path / "baked.toml",
        f"""
[system.ssh]
enable = true
password = "baked-secret"
pub_keys = ["{VALID_SSH_KEY}"]

[[files]]
type = "http"
url = "https://example.com/sync.bin"
target_dir = "models"
filename = "sync.bin"
download_mode = "sync"

[[files]]
type = "http"
url = "https://example.com/async.bin"
target_dir = "models"
filename = "async.bin"
download_mode = "async"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "pre-start", "10-pre.sh")
    events: list[str] = []

    def runtime_downloader(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        **_kwargs: object,
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config
        assert [item.filename for item in plan.items] == ["sync.bin"]
        events.append("sync-download")
        return ()

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        cancel_requested: Callable[[], bool],
        event_sink: object,
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, event_sink
        assert phase == "pre-start"
        assert cancel_requested() is False
        assert events == ["sync-download"]
        events.append("pre-start")
        return ()

    def runtime_ssh_starter(
        config: RuntimeConfig,
        **_kwargs: object,
    ) -> FakeSshdProcess:
        assert config.system.ssh.enable is True
        assert config.system.ssh.password == "baked-secret"
        assert config.system.ssh.pub_keys == [VALID_SSH_KEY]
        assert events == ["sync-download", "pre-start"]
        events.append("ssh-start")
        return FakeSshdProcess()

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        expected_run_id: str,
        handle_observer: Callable[[FakeAsyncHandle], None],
        cancel_requested: Callable[[], bool],
        event_sink: object,
    ) -> FakeAsyncHandle:
        del config, runtime, runtime_state_path, expected_run_id, event_sink
        assert cancel_requested() is False
        assert [item.filename for item in plan.items] == ["async.bin"]
        assert events == ["sync-download", "pre-start", "ssh-start"]
        events.append("async-start")
        handle = FakeAsyncHandle(events, alive=False)
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
        assert events == ["sync-download", "pre-start", "ssh-start", "async-start"]
        events.append("spawn")
        return FakeChild(0)

    assert (
        run_runtime_generation_once(
            runtime=runtime,
            runtime_state_path=tmp_path / "state.json",
            baked_config_path=baked,
            mounted_config_path=_missing_path(tmp_path, "mounted-config.toml"),
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_downloader=runtime_downloader,
            runtime_hook_runner=runtime_hook_runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_ssh_starter=runtime_ssh_starter,
        )
        == 0
    )

    assert events == ["sync-download", "pre-start", "ssh-start", "async-start", "spawn"]


# Real helper coverage exercises credential prep and runtime sshd argv.
def test_runtime_can_start_real_ssh_helper_with_fake_system_dependencies(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    secret = "real-helper-secret"
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[system.ssh]
enable = true
port = 3022
password = "{secret}"
pub_keys = ["{VALID_SSH_KEY}"]
""",
    )
    credential_runner = RecordingSensitiveCommandRunner()
    command_runner = RecordingCommandRunner()
    process = FakeSshdProcess()
    process_starter = RecordingProcessStarter(process)
    ownership = OwnershipRecorder()
    root_home = tmp_path / "root"
    root_home.mkdir(mode=0o700)
    runtime_dir = tmp_path / "run" / "sshd"
    config_dir = tmp_path / "run" / "cdh"
    config_dir.mkdir(mode=0o700, parents=True)
    config_dir.chmod(0o700)
    events: list[str] = []

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        environment: Mapping[bytes, bytes],
        cancel_requested: Callable[[], bool],
        preparation_process_observer: Callable[[object | None], None],
        preparation_warning_observer: Callable[[SshPreparationWarningKind], object],
    ) -> FakeSshdProcess | None:
        events.append("ssh-start")
        return start_sshd_if_enabled(
            config,
            environment=environment,
            root_home=root_home,
            runtime_dir=runtime_dir,
            config_dir=config_dir,
            credential_command_runner=credential_runner,
            credential_chown=ownership.chown,
            credential_chmod=ownership.chmod,
            credential_fchown=ownership.fchown,
            credential_fchmod=ownership.fchmod,
            credential_owner_uid=os.getuid(),
            credential_owner_gid=os.getgid(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=command_runner,
            preflight_command_runner=command_runner,
            process_starter=process_starter,
            readiness_probe=lambda _port, _timeout: b"SSH-2.0-test",
            cancel_requested=cancel_requested,
            preparation_process_observer=preparation_process_observer,
            preparation_warning_observer=preparation_warning_observer,
        )

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
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_ssh_starter=runtime_ssh_starter,
        )
        == 0
    )

    authorized_keys = root_home / ".ssh" / "authorized_keys"
    assert events == ["ssh-start", "spawn"]
    assert authorized_keys.read_text(encoding="utf-8") == f"{VALID_SSH_KEY}\n"
    assert ownership.chown_calls == [
        (root_home / ".ssh", os.getuid(), os.getgid()),
    ]
    assert ownership.chmod_calls == [
        (root_home / ".ssh", 0o700),
    ]
    assert ownership.fchown_calls == [(os.getuid(), os.getgid())]
    assert ownership.fchmod_calls == [0o600]
    assert credential_runner.calls == [
        SensitiveCommandCall(
            argv=["chpasswd"],
            input_data=f"root:{secret}\n".encode(),
            description="set root SSH password",
        ),
        SensitiveCommandCall(
            argv=["passwd", "-u", "root"],
            input_data=b"",
            description="unlock root SSH account",
        ),
    ]
    assert command_runner.calls == [
        PlainCommandCall(["/usr/bin/ssh-keygen", "-A"], "generate OpenSSH host keys"),
        PlainCommandCall(
            ["/usr/sbin/sshd", "-t", "-f", process_starter.calls[0].argv[2]],
            "validate sshd configuration",
        ),
    ]
    assert runtime_dir.is_dir()
    assert len(process_starter.calls) == 1
    config_path = Path(process_starter.calls[0].argv[2])
    assert process_starter.calls[0] == PlainCommandCall(
        ["/usr/sbin/sshd", "-f", os.fspath(config_path), "-D", "-e"],
        "start sshd",
    )
    assert config_path.parent == config_dir
    assert not config_path.exists()
    assert secret not in " ".join(process_starter.calls[0].argv)
    assert VALID_SSH_KEY not in " ".join(process_starter.calls[0].argv)


def test_env_ssh_overrides_and_appends_key_at_runtime_boundary(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    baked = _write(
        tmp_path / "baked.toml",
        """
[system.ssh]
enable = false
port = 2022
password = "baked-secret"
pub_keys = []
""",
    )
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[system.ssh]
enable = false
port = 2200
password = "mounted-secret"
pub_keys = ["{VALID_SSH_KEY}"]
""",
    )
    seen: list[RuntimeConfig] = []
    seen_environments: list[Mapping[bytes, bytes]] = []

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        environment: Mapping[bytes, bytes],
        cancel_requested: Callable[[], bool],
        **_kwargs: object,
    ) -> FakeSshdProcess:
        seen.append(config)
        seen_environments.append(environment)
        assert cancel_requested() is False
        return FakeSshdProcess()

    assert (
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=baked,
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={
                "PATH": "/usr/bin",
                "SSH_ENABLE": " yes ",
                "SSH_PORT": " 3022 ",
                "SSH_PASSWORD": " env-secret ",
                "SSH_PUB_KEY": f" {SECOND_SSH_KEY} ",
            },
            runner=lambda *_args, **_kwargs: FakeChild(0),
            runtime_ssh_starter=runtime_ssh_starter,
        )
        == 0
    )

    assert len(seen) == 1
    assert seen[0].system.ssh.enable is True
    assert seen[0].system.ssh.port == 3022
    assert seen[0].system.ssh.password == " env-secret "
    assert seen[0].system.ssh.pub_keys == [VALID_SSH_KEY, SECOND_SSH_KEY]
    assert dict(seen_environments[0]) == {
        b"PATH": b"/usr/bin",
        b"SSH_ENABLE": b" yes ",
        b"SSH_PORT": b" 3022 ",
        b"SSH_PASSWORD": b" env-secret ",
        b"SSH_PUB_KEY": f" {SECOND_SSH_KEY} ".encode(),
    }


# Disabled and credential-less SSH cases must continue without starting sshd.
@pytest.mark.parametrize(
    ("document", "environ"),
    [
        pytest.param(
            """
[system.ssh]
enable = false
password = "configured-secret"
pub_keys = []
""",
            {"SSH_PUB_KEY": SECOND_SSH_KEY},
            id="config-disabled",
        ),
        pytest.param(
            f"""
[system.ssh]
enable = true
password = "configured-secret"
pub_keys = ["{VALID_SSH_KEY}"]
""",
            {"SSH_ENABLE": " false ", "SSH_PASSWORD": "env-secret"},
            id="environment-disabled",
        ),
    ],
)
def test_disabled_ssh_does_not_start_even_with_credentials(
    tmp_path: Path,
    document: str,
    environ: dict[str, str],
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(tmp_path / "mounted.toml", document)
    events: list[str] = []

    def runtime_ssh_starter(
        config: RuntimeConfig,
        **_kwargs: object,
    ) -> FakeSshdProcess:
        del config
        events.append("ssh-start")
        return FakeSshdProcess()

    assert (
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin", **environ},
            runner=lambda *_args, **_kwargs: FakeChild(0),
            runtime_ssh_starter=runtime_ssh_starter,
        )
        == 0
    )

    assert events == []


def test_enabled_ssh_without_credentials_warns_and_continues(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
""",
    )
    events: list[str] = []
    runtime_events = RecordingRuntimeEventSink()

    assert (
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: events.append("spawn") or FakeChild(0),
            event_sink=runtime_events,
        )
        == 0
    )

    assert events == ["spawn"]
    assert [
        event for event in runtime_events.events if isinstance(event, RuntimeSshOutcome)
    ] == [RuntimeSshOutcome(RuntimeSshStatus.ENABLED_WITHOUT_CREDENTIALS)]


def test_managed_ssh_preparation_warning_is_emitted_directly_after_join() -> None:
    config = RuntimeConfig.model_validate(
        {"system": {"ssh": {"enable": True, "password": "secret"}}}
    )
    events: list[tuple[object, str]] = []

    class Recorder:
        def emit(self, event: object, /) -> None:
            events.append((event, threading.current_thread().name))

    def managed_starter(
        _config: RuntimeConfig,
        *,
        environment: Mapping[bytes, bytes],
        cancel_requested: Callable[[], bool],
        preparation_process_observer: Callable[[object | None], None],
        preparation_warning_observer: Callable[[SshPreparationWarningKind], object],
    ) -> None:
        del environment, cancel_requested, preparation_process_observer
        assert threading.current_thread().name == "cdh-ssh-startup"
        preparation_warning_observer(
            SshPreparationWarningKind.DIRECTORY_MODE_NONSTANDARD
        )
        return None

    service = RuntimeSshService(
        config,
        environment={b"PATH": b"/usr/bin"},
        starter=managed_starter,
        background_event_sink=RecordingRuntimeEventSink(),
        event_sink=Recorder(),
    )
    service.start()

    assert events == [
        (
            RuntimeSshWarning(RuntimeSshWarningKind.DIRECTORY_MODE_NONSTANDARD),
            threading.current_thread().name,
        )
    ]


def test_managed_ssh_direct_reap_warning_is_deduplicated() -> None:
    config = RuntimeConfig.model_validate(
        {"system": {"ssh": {"enable": True, "password": "secret"}}}
    )
    events: list[object] = []

    class Recorder:
        def emit(self, event: object, /) -> None:
            events.append(event)

    class ReapFailureSshd:
        returncode = 0

        def poll(self) -> int:
            return self.returncode

        def wait(self) -> int:
            raise OSError("raw-reap-sentinel")

        def terminate(self) -> None:
            pytest.fail("terminal sshd must not be terminated")

        def kill(self) -> None:
            pytest.fail("terminal sshd must not be killed")

    service = RuntimeSshService(
        config,
        environment={b"PATH": b"/usr/bin"},
        starter=lambda *_args, **_kwargs: ReapFailureSshd(),
        background_event_sink=RecordingRuntimeEventSink(),
        event_sink=Recorder(),
    )
    service.start()

    assert service.is_stopped() is False
    assert service.is_stopped() is False
    assert events == [RuntimeSshWarning(RuntimeSshWarningKind.SERVICE_REAP_FAILED)]


# Failure coverage protects pre-spawn abort behavior and credential redaction.
def test_ssh_start_failure_prevents_spawn_and_redacts_credentials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    secret = "super-secret-password"
    mounted = _write(
        tmp_path / "mounted.toml",
        f"""
[system.ssh]
enable = true
password = "{secret}"
pub_keys = ["{VALID_SSH_KEY}"]
""",
    )
    events: list[str] = []

    def runtime_ssh_starter(
        config: RuntimeConfig,
        **_kwargs: object,
    ) -> FakeSshdProcess:
        assert config.system.ssh.password == secret
        events.append("ssh-start")
        raise SshdStartupError("raw-host-key-sentinel\ncredential-url")

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

    with pytest.raises(RuntimeExecutionError) as raised:
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_ssh_starter=runtime_ssh_starter,
        )

    captured = capsys.readouterr()
    payload = f"{raised.value}\n{captured.out}\n{captured.err}"
    assert events == ["ssh-start"]
    assert str(raised.value) == "SSH startup/readiness failed"
    service_error = raised.value.__cause__
    assert isinstance(service_error, RuntimeSshServiceError)
    assert isinstance(service_error.__cause__, SshdStartupError)
    assert "raw-host-key-sentinel" not in payload
    assert "credential-url" not in payload
    assert secret not in payload
    assert VALID_SSH_KEY not in payload


def test_invalid_password_failure_before_spawn_redacts_password(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "line1\\nline2"
""",
    )

    with pytest.raises(RuntimeExecutionError) as raised:
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: FakeChild(0),
        )

    captured = capsys.readouterr()
    payload = f"{raised.value}\n{captured.out}\n{captured.err}"
    assert str(raised.value) == "SSH credential preparation failed"
    service_error = raised.value.__cause__
    assert isinstance(service_error, RuntimeSshServiceError)
    assert isinstance(service_error.__cause__, SshdStartupError)
    assert "SSH password must not contain line breaks or NUL bytes" not in payload
    assert "line1" not in payload
    assert "line2" not in payload


@pytest.mark.parametrize(
    ("failure_kind", "expected"),
    [
        pytest.param(
            "environment",
            "SSH environment projection failed",
            id="environment-projection",
        ),
        pytest.param(
            "config",
            "SSH protected configuration preparation failed",
            id="protected-config",
        ),
        pytest.param(
            "parser",
            "SSH configuration parser validation failed",
            id="parser-validation",
        ),
        pytest.param(
            "credential",
            "SSH credential preparation failed",
            id="credential",
        ),
        pytest.param(
            "readiness",
            "SSH startup/readiness failed",
            id="startup-readiness",
        ),
    ],
)
def test_ssh_activation_failures_use_fixed_top_level_categories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure_kind: str,
    expected: str,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "configured-secret"
""",
    )

    def runtime_ssh_starter(
        _config: RuntimeConfig,
        **_kwargs: object,
    ) -> FakeSshdProcess:
        sentinel = "ssh-private-activation-sentinel"
        if failure_kind == "environment":
            raise SshEnvironmentProjectionError(sentinel)
        if failure_kind == "config":
            raise SshdConfigPreparationError(sentinel)
        if failure_kind == "parser":
            raise SshdConfigValidationError(sentinel)
        if failure_kind == "credential":
            try:
                raise SshCredentialPreparationError(sentinel)
            except SshCredentialPreparationError as cause:
                raise SshdStartupError(sentinel) from cause
        if failure_kind == "readiness":
            raise SshdReadinessError(sentinel)
        raise AssertionError(f"unexpected failure kind: {failure_kind}")

    with pytest.raises(RuntimeExecutionError) as raised:
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin", "SSH_SECRET": "environment-secret"},
            runner=lambda *_args, **_kwargs: pytest.fail(
                "ComfyUI must not start after SSH activation failure"
            ),
            runtime_ssh_starter=runtime_ssh_starter,
        )

    captured = capsys.readouterr()
    payload = f"{raised.value}\n{captured.out}\n{captured.err}"
    assert str(raised.value) == expected
    assert "ssh-private-activation-sentinel" not in payload
    assert "environment-secret" not in payload


# A failed direct reap is not terminal evidence, so the SSH owner must report
# incomplete shutdown instead of accepting a successful poll result.
def test_cooperative_sshd_stop_fails_closed_on_wait_error() -> None:
    warnings: list[RuntimeSshWarningKind] = []

    class WaitErrorSshd:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            raise AssertionError("cooperative sshd must not be killed")

        def wait(self) -> int:
            raise OSError("wait failed")

    assert (
        stop_runtime_ssh_service(
            WaitErrorSshd(),
            cancel_requested=lambda: False,
            shutdown_requested=threading.Event(),
            warning_observer=warnings.append,
        )
        is False
    )
    assert warnings == [RuntimeSshWarningKind.SERVICE_SHUTDOWN_FAILED]


def test_unexpected_post_start_sshd_exit_warns_without_changing_comfyui_exit(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"
""",
    )
    sshd = FakeSshdProcess(wait_returncode=23)
    warning_observed = threading.Event()

    class WarningRecorder(RecordingRuntimeEventSink):
        def emit(self, event: object, /) -> None:
            super().emit(event)
            if event == RuntimeSshWarning(
                RuntimeSshWarningKind.EXITED_UNEXPECTEDLY,
                23,
            ):
                warning_observed.set()

    runtime_events = WarningRecorder()

    assert (
        run_runtime_generation_once(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: WaitForEventChild(
                warning_observed,
                returncode=7,
            ),
            runtime_ssh_starter=lambda *_args, **_kwargs: sshd,
            background_event_sink=runtime_events,
        )
        == 7
    )

    assert sshd.waited.wait(timeout=1)
    assert warning_observed.is_set()
    assert (
        RuntimeSshWarning(
            RuntimeSshWarningKind.EXITED_UNEXPECTEDLY,
            23,
        )
        in runtime_events.events
    )


def test_shutdown_stops_async_then_ssh_then_hooks_before_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    mounted = _write(
        tmp_path / "mounted.toml",
        """
[system.ssh]
enable = true
password = "secret"

[cdh]
default_download_mode = "async"

[[files]]
type = "http"
url = "https://example.com/async.bin"
target_dir = "models"
filename = "async.bin"
""",
    )
    hooks = tmp_path / "hooks"
    _write_hook(hooks, "stop", "90-stop.sh")
    handlers = _capture_signal_handlers(monkeypatch)
    events: list[str] = []

    def runtime_async_queue_starter(
        plan: RuntimeFilePlan,
        *,
        config: RuntimeConfig,
        runtime: ContainerRuntime,
        runtime_state_path: Path,
        expected_run_id: str,
        handle_observer: Callable[[FakeAsyncHandle], None],
        cancel_requested: Callable[[], bool],
        event_sink: object,
    ) -> FakeAsyncHandle:
        del config, runtime, runtime_state_path, expected_run_id, event_sink
        assert cancel_requested() is False
        assert [item.filename for item in plan.items] == ["async.bin"]
        events.append("async-start")
        handle = FakeAsyncHandle(events)
        handle_observer(handle)
        return handle

    def runtime_stop_hook_runner(
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        cancel_requested: Callable[[], bool],
        deadline: float | None,
        monotonic: Callable[[], float],
        sleep: Callable[[float], object],
        event_sink: object,
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, deadline, monotonic, sleep, event_sink
        assert cancel_requested() is False
        events.append("stop-hook")
        return ()

    def runner(
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        shell: bool,
    ) -> FakeChild:
        del argv, cwd, env, shell
        events.append("spawn")
        return FakeChild(
            -int(signal.SIGTERM),
            events=events,
            handlers=handlers,
            shutdown_signal=signal.SIGTERM,
        )

    assert (
        run_runtime_generation_once(
            runtime=runtime,
            runtime_state_path=tmp_path / "state.json",
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=hooks,
            environ={"PATH": "/usr/bin"},
            runner=runner,
            runtime_async_queue_starter=runtime_async_queue_starter,
            runtime_ssh_starter=lambda *_args, **_kwargs: FakeSshdProcess(
                events=events
            ),
            runtime_stop_hook_runner=runtime_stop_hook_runner,
        )
        == 143
    )

    assert events[:7] == [
        "async-start",
        "spawn",
        "async-stop",
        "async-terminate-backends",
        "ssh-stop",
        "stop-hook",
        "signal:SIGTERM",
    ]
    cleanup_tail = events[7:]
    assert cleanup_tail[0] == "wait"
    assert cleanup_tail.count("async-join") == 1
    assert set(cleanup_tail) == {"wait", "async-join"}


# SSH startup publishes each exact preparation child to the service owner, so
# cancellation terminates and reaps it before startup can return.
def test_ssh_startup_operation_cancels_and_reaps_published_process(
    owned_preparation_processes: list[subprocess.Popen[bytes]],
) -> None:
    config = RuntimeConfig.model_validate(
        {"system": {"ssh": {"enable": True, "password": "secret"}}}
    )
    published = threading.Event()
    starter_cancellation_seen = threading.Event()
    process: subprocess.Popen[bytes] | None = None

    def starter(
        config: RuntimeConfig,
        *,
        environment: Mapping[bytes, bytes],
        cancel_requested: Callable[[], bool],
        preparation_process_observer: Callable[[object | None], None],
        preparation_warning_observer: Callable[[SshPreparationWarningKind], object],
    ) -> None:
        nonlocal process
        del config, environment, preparation_warning_observer
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        owned_preparation_processes.append(process)
        preparation_process_observer(process)
        published.set()
        process.wait(timeout=_PREPARATION_PROCESS_TIMEOUT_SECONDS)
        if cancel_requested():
            starter_cancellation_seen.set()
        preparation_process_observer(None)
        return None

    class Cancellation:
        def __call__(self) -> bool:
            return published.is_set()

        def force_requested(self) -> bool:
            return False

    runtime_events = RecordingRuntimeEventSink()
    RuntimeSshService(
        config,
        environment={b"PATH": b"/usr/bin"},
        starter=starter,
        background_event_sink=runtime_events,
        event_sink=runtime_events,
    ).start(cancel_requested=Cancellation())

    assert process is not None
    assert process.returncode is not None
    assert starter_cancellation_seen.is_set()
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)


# A monitor wait exception does not manufacture reap evidence; the SSH owner
# retains the handle and continues to report non-quiescence.
def test_ssh_monitor_wait_failure_retains_unreaped_owner() -> None:
    config = RuntimeConfig.model_validate(
        {"system": {"ssh": {"enable": True, "password": "secret"}}}
    )
    waited = threading.Event()

    class WaitFailureSshd:
        returncode = 0

        def poll(self) -> int:
            return self.returncode

        def wait(self) -> int:
            waited.set()
            raise OSError("monitor wait failed")

        def terminate(self) -> None:
            raise AssertionError("terminal-looking owner must not be signaled")

        def kill(self) -> None:
            raise AssertionError("terminal-looking owner must not be signaled")

    sshd = WaitFailureSshd()
    runtime_events = RecordingRuntimeEventSink()
    service = RuntimeSshService(
        config,
        environment={b"PATH": b"/usr/bin"},
        starter=lambda *_args, **_kwargs: sshd,
        background_event_sink=runtime_events,
        event_sink=runtime_events,
    )
    service.start()
    service.monitor_after_comfyui_start()

    assert waited.wait(timeout=1)
    assert service.is_stopped() is False
    assert RuntimeSshWarning(RuntimeSshWarningKind.MONITOR_FAILED) in (
        runtime_events.events
    )


def test_ssh_stop_waits_for_inflight_monitor_warning_delivery() -> None:
    config = RuntimeConfig.model_validate(
        {"system": {"ssh": {"enable": True, "password": "secret"}}}
    )
    warning_entered = threading.Event()
    release_warning = threading.Event()
    stop_reap_entered = threading.Event()
    warnings: list[object] = []

    class Recorder:
        def emit(self, event: object, /) -> None:
            warning_entered.set()
            release_warning.wait(timeout=1)
            warnings.append(event)

        def emit_progress(self, _scope: object, _event: object) -> None:
            return

        def close_progress(self, _scope: object) -> None:
            return

    class TerminalSshd:
        returncode = 7

        def poll(self) -> int:
            if threading.current_thread().name == "test-ssh-stopper":
                stop_reap_entered.set()
            return self.returncode

        def wait(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            pytest.fail("a terminal sshd must not be terminated")

        def kill(self) -> None:
            pytest.fail("a terminal sshd must not be killed")

    sshd = TerminalSshd()
    service = RuntimeSshService(
        config,
        environment={b"PATH": b"/usr/bin"},
        starter=lambda *_args, **_kwargs: sshd,
        background_event_sink=Recorder(),
        event_sink=RecordingRuntimeEventSink(),
    )
    service.start()
    service.monitor_after_comfyui_start()
    assert warning_entered.wait(timeout=1)

    results: list[bool] = []
    stopping = threading.Thread(
        target=lambda: results.append(
            service.stop(cancel_requested=lambda: False, timeout=1)
        ),
        name="test-ssh-stopper",
    )
    stopping.start()
    assert stop_reap_entered.wait(timeout=1)
    stopping.join(timeout=0.05)
    assert stopping.is_alive() is True
    release_warning.set()
    stopping.join(timeout=1)

    assert results == [True]
    assert warnings == [RuntimeSshWarning(RuntimeSshWarningKind.EXITED_UNEXPECTEDLY, 7)]


# Password bytes stay on stdin and never enter the preparation process argv.
def test_sensitive_ssh_preparation_keeps_secret_out_of_process_argv() -> None:
    secret = b"root:super-secret-value\n"
    observed: list[subprocess.Popen[bytes] | None] = []

    assert (
        ssh_module._run_sensitive_command(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            input_data=secret,
            description="test sensitive input",
            process_observer=observed.append,
        )
        == 0
    )
    assert len(observed) == 2
    process = observed[0]
    assert process is not None
    assert secret.decode().strip() not in " ".join(process.args)
    assert observed[1] is None
