"""End-to-end SSH entrypoint integration coverage."""

from __future__ import annotations

import os
import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pytest

from comfyui_docker_helper.config import RuntimeConfig
from comfyui_docker_helper.container import entrypoint as entrypoint_module
from comfyui_docker_helper.container.entrypoint import EntrypointError, run_entrypoint
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.runtime_files import (
    Logger,
    RuntimeFileDownloadResult,
    RuntimeFilePlan,
)
from comfyui_docker_helper.container.runtime_hooks import (
    RuntimeHookPlan,
    RuntimeHookResult,
)
from comfyui_docker_helper.container.ssh import SshdStartupError, start_sshd_if_enabled

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


class FakeChild:
    """Minimal ComfyUI process handle for entrypoint integration tests."""

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
        event: entrypoint_module.threading.Event,
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
        self.waited = entrypoint_module.threading.Event()
        self.released = entrypoint_module.threading.Event()

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


class EventStderr(StringIO):
    def __init__(self, pattern: str) -> None:
        super().__init__()
        self._pattern = pattern
        self.observed = entrypoint_module.threading.Event()

    def write(self, value: str) -> int:
        written = super().write(value)
        if self._pattern in self.getvalue():
            self.observed.set()
        return written


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


# Default/inactive SSH coverage ensures normal entrypoint startup is untouched.
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
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return FakeSshdProcess()

    assert (
        run_entrypoint(
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
url = "https://example.com/sync.bin"
dir = "models"
filename = "sync.bin"
download_mode = "sync"

[[files]]
url = "https://example.com/async.bin"
dir = "models"
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
        log: Logger,
        **_kwargs: object,
    ) -> tuple[RuntimeFileDownloadResult, ...]:
        del config, log
        assert [item.filename for item in plan.items] == ["sync.bin"]
        events.append("sync-download")
        return ()

    def runtime_hook_runner(
        plan: RuntimeHookPlan,
        phase: str,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log
        assert phase == "pre-start"
        assert cancel_requested() is False
        assert events == ["sync-download"]
        events.append("pre-start")
        return ()

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del runtime, log
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
        log: Logger,
    ) -> FakeAsyncHandle:
        del config, runtime, runtime_state_path, expected_run_id, log
        assert [item.filename for item in plan.items] == ["async.bin"]
        assert events == ["sync-download", "pre-start", "ssh-start"]
        events.append("async-start")
        return FakeAsyncHandle(events, alive=False)

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
        run_entrypoint(
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


# Real helper coverage exercises credential prep and entrypoint sshd argv.
def test_entrypoint_can_start_real_ssh_helper_with_fake_system_dependencies(
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
    events: list[str] = []

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess | None:
        events.append("ssh-start")
        return start_sshd_if_enabled(
            config,
            runtime=runtime,
            log=log,
            root_home=root_home,
            runtime_dir=runtime_dir,
            credential_command_runner=credential_runner,
            credential_chown=ownership.chown,
            credential_chmod=ownership.chmod,
            credential_fchown=ownership.fchown,
            credential_fchmod=ownership.fchmod,
            credential_owner_uid=os.getuid(),
            credential_owner_gid=os.getgid(),
            command_runner=command_runner,
            process_starter=process_starter,
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
        run_entrypoint(
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
        PlainCommandCall(["/usr/bin/ssh-keygen", "-A"], "generate OpenSSH host keys")
    ]
    assert runtime_dir.is_dir()
    assert process_starter.calls == [
        PlainCommandCall(
            [
                "/usr/sbin/sshd",
                "-f",
                "/dev/null",
                "-D",
                "-e",
                "-o",
                "Port=3022",
                "-o",
                "PermitRootLogin=yes",
                "-o",
                "PasswordAuthentication=yes",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-o",
                "PubkeyAuthentication=yes",
                "-o",
                "AuthorizedKeysFile=/root/.ssh/authorized_keys",
            ],
            "start sshd",
        )
    ]
    assert secret not in " ".join(process_starter.calls[0].argv)
    assert VALID_SSH_KEY not in " ".join(process_starter.calls[0].argv)


def test_env_ssh_overrides_and_appends_key_at_entrypoint_boundary(
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

    def runtime_ssh_starter(
        config: RuntimeConfig,
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del runtime, log
        seen.append(config)
        return FakeSshdProcess()

    assert (
        run_entrypoint(
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


# Disabled and credential-less SSH cases must continue without starting sshd.
@pytest.mark.parametrize(
    ("document", "environ"),
    [
        (
            """
[system.ssh]
enable = false
password = "configured-secret"
pub_keys = []
""",
            {"SSH_PUB_KEY": SECOND_SSH_KEY},
        ),
        (
            f"""
[system.ssh]
enable = true
password = "configured-secret"
pub_keys = ["{VALID_SSH_KEY}"]
""",
            {"SSH_ENABLE": " false ", "SSH_PASSWORD": "env-secret"},
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
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del config, runtime, log
        events.append("ssh-start")
        return FakeSshdProcess()

    assert (
        run_entrypoint(
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
    capsys: pytest.CaptureFixture[str],
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

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: events.append("spawn") or FakeChild(0),
        )
        == 0
    )

    captured = capsys.readouterr()
    assert events == ["spawn"]
    assert "WARNING: SSH is enabled but no root SSH credentials are configured" in (
        captured.out
    )


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
        *,
        runtime: ContainerRuntime,
        log: Logger,
    ) -> FakeSshdProcess:
        del runtime, log
        assert config.system.ssh.password == secret
        events.append("ssh-start")
        raise SshdStartupError("host key generation unavailable")

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

    with pytest.raises(EntrypointError) as raised:
        run_entrypoint(
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
    assert "SSH runtime service failed to start" in str(raised.value)
    assert "host key generation unavailable" in str(raised.value)
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

    with pytest.raises(EntrypointError) as raised:
        run_entrypoint(
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
    assert "SSH runtime service failed to start" in str(raised.value)
    assert "SSH password must not contain line breaks or NUL bytes" in str(raised.value)
    assert "line1" not in payload
    assert "line2" not in payload


# Lifecycle coverage protects sshd monitoring and async/SSH shutdown ordering.
def test_cooperative_sshd_stop_keeps_wait_errors_best_effort(
    capsys: pytest.CaptureFixture[str],
) -> None:
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
        entrypoint_module._stop_sshd_runtime_service(
            WaitErrorSshd(),
            cancel_requested=lambda: False,
            shutdown_requested=entrypoint_module.threading.Event(),
        )
        is True
    )
    assert "SSH runtime service stopped" in capsys.readouterr().out


def test_unexpected_post_start_sshd_exit_warns_without_changing_comfyui_exit(
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
""",
    )
    sshd = FakeSshdProcess(wait_returncode=23)
    stderr = EventStderr(
        "WARNING: SSH runtime service exited unexpectedly: returncode=23"
    )
    monkeypatch.setattr(entrypoint_module.sys, "stderr", stderr)

    assert (
        run_entrypoint(
            runtime=runtime,
            baked_config_path=_missing_path(tmp_path, "baked-config.toml"),
            mounted_config_path=mounted,
            baked_hooks_path=_missing_path(tmp_path, "baked-hooks"),
            mounted_hooks_path=_missing_path(tmp_path, "mounted-hooks"),
            environ={"PATH": "/usr/bin"},
            runner=lambda *_args, **_kwargs: WaitForEventChild(
                stderr.observed,
                returncode=7,
            ),
            runtime_ssh_starter=lambda *_args, **_kwargs: sshd,
        )
        == 7
    )

    assert sshd.waited.wait(timeout=1)
    assert stderr.observed.is_set()
    assert "WARNING: SSH runtime service exited unexpectedly: returncode=23" in (
        stderr.getvalue()
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
url = "https://example.com/async.bin"
dir = "models"
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
        log: Logger,
    ) -> FakeAsyncHandle:
        del config, runtime, runtime_state_path, expected_run_id, log
        assert [item.filename for item in plan.items] == ["async.bin"]
        events.append("async-start")
        return FakeAsyncHandle(events)

    def runtime_stop_hook_runner(
        plan: RuntimeHookPlan,
        *,
        runtime: ContainerRuntime,
        env: Mapping[str, str] | None = None,
        log: Logger,
        cancel_requested: Callable[[], bool],
    ) -> tuple[RuntimeHookResult, ...]:
        del plan, runtime, env, log
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
        run_entrypoint(
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

    assert events == [
        "async-start",
        "spawn",
        "async-stop",
        "async-join",
        "ssh-stop",
        "stop-hook",
        "signal:SIGTERM",
        "wait",
    ]
