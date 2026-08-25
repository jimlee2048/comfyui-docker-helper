"""Runtime SSH startup, readiness, and child cleanup coverage."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.unit.container.runtime.runtime_ssh_support import (
    VALID_SSH_KEY,
    OwnershipRecorder,
    RecordingRunner,
    create_root_home,
)

import comfyui_docker_helper.container.runtime.ssh.config as ssh_module
from comfyui_docker_helper.config import RuntimeConfig, RuntimeSystemSshConfig
from comfyui_docker_helper.container.runtime.ssh.config import (
    OwnedSshdProcess,
    RootSshCredentialPreparationStatus,
    SshCredentialPreparationError,
    SshdConfigPreparationError,
    SshdConfigValidationError,
    SshdReadinessError,
    SshdStartupError,
    SshPreparationWarningKind,
    build_sshd_argv,
    start_sshd_if_enabled,
)


@dataclass(frozen=True, slots=True)
class PlainCommandCall:
    argv: list[str]
    description: str


class RecordingCommandRunner:
    def __init__(self, returncodes: tuple[int, ...] = ()) -> None:
        self.calls: list[PlainCommandCall] = []
        self._returncodes = list(returncodes)

    def __call__(self, argv: list[str] | tuple[str, ...], *, description: str) -> int:
        self.calls.append(PlainCommandCall(argv=list(argv), description=description))
        if self._returncodes:
            return self._returncodes.pop(0)
        return 0


class FakeSshdProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.wait_calls = 0
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class RecordingProcessStarter:
    def __init__(self, process: FakeSshdProcess | None = None) -> None:
        self.process = FakeSshdProcess() if process is None else process
        self.calls: list[PlainCommandCall] = []

    def __call__(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        description: str,
    ) -> FakeSshdProcess:
        self.calls.append(PlainCommandCall(argv=list(argv), description=description))
        return self.process


def _create_config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "run" / "cdh"
    config_dir.mkdir(mode=0o700, parents=True)
    config_dir.chmod(0o700)
    return config_dir


_create_root_home = create_root_home


# The public startup owner delivers controlled credential warnings.
def test_start_sshd_observes_controlled_credential_path_mode_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prepare_with_warning(
        _config: RuntimeSystemSshConfig,
        **_kwargs: object,
    ) -> RootSshCredentialPreparationStatus:
        return RootSshCredentialPreparationStatus(
            ssh_enabled=True,
            public_key_count=1,
            password_configured=False,
            authorized_keys_path=tmp_path / "root" / ".ssh" / "authorized_keys",
            warnings=(SshPreparationWarningKind.DIRECTORY_MODE_NONSTANDARD,),
        )

    monkeypatch.setattr(
        ssh_module,
        "prepare_root_ssh_credentials",
        prepare_with_warning,
    )
    command_runner = RecordingCommandRunner()
    process_starter = RecordingProcessStarter()
    warnings: list[SshPreparationWarningKind] = []
    config_dir = _create_config_dir(tmp_path)

    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate(
            {"system": {"ssh": {"enable": True, "pub_keys": [VALID_SSH_KEY]}}}
        ),
        environment={},
        runtime_dir=tmp_path / "run" / "sshd",
        config_dir=config_dir,
        config_owner_uid=os.getuid(),
        config_owner_gid=os.getgid(),
        command_runner=command_runner,
        preflight_command_runner=command_runner,
        process_starter=process_starter,
        readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
        preparation_warning_observer=warnings.append,
    )

    assert isinstance(result, OwnedSshdProcess)
    assert warnings == [SshPreparationWarningKind.DIRECTORY_MODE_NONSTANDARD]
    assert result.wait() == 0


# Startup tests protect host-key generation, foreground argv, and redaction.
def test_start_sshd_if_enabled_with_no_credentials_does_not_start(
    tmp_path: Path,
) -> None:
    command_runner = RecordingCommandRunner()
    process_starter = RecordingProcessStarter()

    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate({"system": {"ssh": {"enable": True}}}),
        environment={b"UNREPRESENTABLE": b"line1\nline2"},
        root_home=tmp_path / "root",
        runtime_dir=tmp_path / "run" / "sshd",
        command_runner=command_runner,
        process_starter=process_starter,
        preparation_warning_observer=lambda _warning: None,
    )

    assert result is None
    assert command_runner.calls == []
    assert process_starter.calls == []
    assert not (tmp_path / "run" / "sshd").exists()


def test_start_sshd_if_enabled_generates_host_keys_runtime_dir_and_foreground_argv(
    tmp_path: Path,
) -> None:
    credential_runner = RecordingRunner()
    command_runner = RecordingCommandRunner()
    process = FakeSshdProcess()
    process_starter = RecordingProcessStarter(process)
    root_home = _create_root_home(tmp_path)
    runtime_dir = tmp_path / "run" / "sshd"
    config_dir = _create_config_dir(tmp_path)

    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate(
            {
                "system": {
                    "ssh": {
                        "enable": True,
                        "port": 2222,
                        "password": "secret",
                    }
                }
            }
        ),
        environment={b"TEST_SENTINEL": b"safe-environment-value"},
        root_home=root_home,
        runtime_dir=runtime_dir,
        config_dir=config_dir,
        credential_command_runner=credential_runner,
        config_owner_uid=os.getuid(),
        config_owner_gid=os.getgid(),
        command_runner=command_runner,
        preflight_command_runner=command_runner,
        process_starter=process_starter,
        readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
        preparation_warning_observer=lambda _warning: None,
    )

    assert isinstance(result, OwnedSshdProcess)
    assert runtime_dir.is_dir()
    assert [call.argv for call in credential_runner.calls] == [
        ["chpasswd"],
        ["passwd", "-u", "root"],
    ]
    assert len(process_starter.calls) == 1
    config_path = Path(process_starter.calls[0].argv[2])
    assert command_runner.calls == [
        PlainCommandCall(["/usr/bin/ssh-keygen", "-A"], "generate OpenSSH host keys"),
        PlainCommandCall(
            ["/usr/sbin/sshd", "-t", "-f", os.fspath(config_path)],
            "validate sshd configuration",
        ),
    ]
    assert process_starter.calls == [
        PlainCommandCall(build_sshd_argv(config_path), "start sshd")
    ]
    config_content = config_path.read_bytes()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert b"Port 2222\n" in config_content
    assert b"PasswordAuthentication yes\n" in config_content
    assert b"PubkeyAuthentication no\n" in config_content
    assert b'SetEnv "TEST_SENTINEL=safe-environment-value"\n' in config_content
    assert "safe-environment-value" not in repr(result)
    assert "secret" not in " ".join(process_starter.calls[0].argv)
    assert result.wait() == 0
    assert not config_path.exists()


def test_start_sshd_if_enabled_key_only_writes_keys_and_disables_password_auth(
    tmp_path: Path,
) -> None:
    credential_runner = RecordingRunner()
    command_runner = RecordingCommandRunner()
    process = FakeSshdProcess()
    process_starter = RecordingProcessStarter(process)
    ownership = OwnershipRecorder()
    root_home = _create_root_home(tmp_path)
    runtime_dir = tmp_path / "run" / "sshd"
    config_dir = _create_config_dir(tmp_path)

    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate(
            {
                "system": {
                    "ssh": {
                        "enable": True,
                        "port": 2222,
                        "pub_keys": [VALID_SSH_KEY],
                    }
                }
            }
        ),
        environment={},
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
        readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
        preparation_warning_observer=lambda _warning: None,
    )

    authorized_keys = root_home / ".ssh" / "authorized_keys"
    assert isinstance(result, OwnedSshdProcess)
    assert authorized_keys.read_text(encoding="utf-8") == f"{VALID_SSH_KEY}\n"
    assert ownership.chown_calls == [
        (root_home / ".ssh", os.getuid(), os.getgid()),
    ]
    assert ownership.chmod_calls == [
        (root_home / ".ssh", 0o700),
    ]
    assert ownership.fchown_calls == [(os.getuid(), os.getgid())]
    assert ownership.fchmod_calls == [0o600]
    assert runtime_dir.is_dir()
    assert credential_runner.calls == []
    assert len(process_starter.calls) == 1
    config_path = Path(process_starter.calls[0].argv[2])
    assert command_runner.calls == [
        PlainCommandCall(["/usr/bin/ssh-keygen", "-A"], "generate OpenSSH host keys"),
        PlainCommandCall(
            ["/usr/sbin/sshd", "-t", "-f", os.fspath(config_path)],
            "validate sshd configuration",
        ),
    ]
    assert process_starter.calls == [
        PlainCommandCall(build_sshd_argv(config_path), "start sshd")
    ]
    config_content = config_path.read_bytes()
    assert b"PasswordAuthentication no\n" in config_content
    assert b"PubkeyAuthentication yes\n" in config_content
    assert VALID_SSH_KEY not in " ".join(process_starter.calls[0].argv)
    assert VALID_SSH_KEY.encode() not in config_content
    assert result.wait() == 0
    assert not config_path.exists()


def test_start_sshd_if_enabled_fails_when_host_key_generation_fails(
    tmp_path: Path,
) -> None:
    credential_runner = RecordingRunner()
    command_runner = RecordingCommandRunner(returncodes=(19,))
    process_starter = RecordingProcessStarter()
    ownership = OwnershipRecorder()
    password = "secret-password"
    root_home = _create_root_home(tmp_path)
    runtime_dir = tmp_path / "run" / "sshd"

    with pytest.raises(SshdStartupError) as raised:
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {
                    "system": {
                        "ssh": {
                            "enable": True,
                            "password": password,
                            "pub_keys": [VALID_SSH_KEY],
                        }
                    }
                }
            ),
            environment={},
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
            preparation_warning_observer=lambda _warning: None,
        )

    error = str(raised.value)
    assert "generate OpenSSH host keys failed with exit code 19" in error
    assert "/usr/bin/ssh-keygen" not in error
    assert password not in error
    assert VALID_SSH_KEY not in error
    assert command_runner.calls == [
        PlainCommandCall(["/usr/bin/ssh-keygen", "-A"], "generate OpenSSH host keys")
    ]
    assert process_starter.calls == []
    assert not runtime_dir.exists()


def test_start_sshd_rejects_unadmitted_config_directory_without_publication(
    tmp_path: Path,
) -> None:
    config_dir = _create_config_dir(tmp_path)
    config_dir.chmod(0o755)

    with pytest.raises(
        SshdConfigPreparationError,
        match=r"^SSH runtime configuration preparation failed$",
    ):
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            environment={},
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            config_dir=config_dir,
            credential_command_runner=RecordingRunner(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=RecordingCommandRunner(),
            preflight_command_runner=RecordingCommandRunner(),
            process_starter=RecordingProcessStarter(),
            readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
            preparation_warning_observer=lambda _warning: None,
        )

    assert list(config_dir.iterdir()) == []


def test_sshd_config_publication_failure_cleans_unique_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = _create_config_dir(tmp_path)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("raw-publication-sentinel")

    monkeypatch.setattr(ssh_module.os, "replace", fail_replace)
    with pytest.raises(SshdConfigPreparationError) as raised:
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            environment={},
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            config_dir=config_dir,
            credential_command_runner=RecordingRunner(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=RecordingCommandRunner(),
            preflight_command_runner=RecordingCommandRunner(),
            process_starter=RecordingProcessStarter(),
            readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
            preparation_warning_observer=lambda _warning: None,
        )

    assert str(raised.value) == "SSH runtime configuration preparation failed"
    assert "raw-publication-sentinel" not in str(raised.value)
    assert list(config_dir.iterdir()) == []


def test_sshd_preflight_failure_cleans_config_before_process_start(
    tmp_path: Path,
) -> None:
    config_dir = _create_config_dir(tmp_path)
    process_starter = RecordingProcessStarter()

    with pytest.raises(
        SshdConfigValidationError,
        match=r"^sshd configuration validation failed$",
    ) as raised:
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            environment={b"TEST_SENTINEL": b"private-preflight-value"},
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            config_dir=config_dir,
            credential_command_runner=RecordingRunner(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=RecordingCommandRunner(),
            preflight_command_runner=RecordingCommandRunner(returncodes=(17,)),
            process_starter=process_starter,
            readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
            preparation_warning_observer=lambda _warning: None,
        )

    assert process_starter.calls == []
    assert list(config_dir.iterdir()) == []
    assert "private-preflight-value" not in str(raised.value)


@pytest.mark.parametrize(
    ("probe", "cancel_requested", "message"),
    [
        pytest.param(
            lambda _port, _timeout: b"SSH-1.99-test",
            lambda: False,
            "sshd readiness check failed",
            id="non-ssh-2-banner",
        ),
        pytest.param(
            lambda _port, _timeout: b"SSH-2.0-test",
            lambda: False,
            "sshd readiness check failed",
            id="truncated-banner",
        ),
        pytest.param(
            lambda _port, _timeout: b"SSH-2.0-\n",
            lambda: False,
            "sshd readiness check failed",
            id="empty-software-version",
        ),
        pytest.param(
            lambda _port, _timeout: b"SSH-2.0-test\n",
            lambda: True,
            "sshd startup was cancelled",
            id="cancelled",
        ),
    ],
)
def test_sshd_readiness_failure_terminates_reaps_and_cleans_config(
    tmp_path: Path,
    probe: Callable[[int, float], bytes],
    cancel_requested: Callable[[], bool],
    message: str,
) -> None:
    config_dir = _create_config_dir(tmp_path)
    process = FakeSshdProcess()
    observed: list[object | None] = []

    with pytest.raises(SshdReadinessError) as raised:
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            environment={},
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            config_dir=config_dir,
            credential_command_runner=RecordingRunner(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=RecordingCommandRunner(),
            preflight_command_runner=RecordingCommandRunner(),
            process_starter=RecordingProcessStarter(process),
            readiness_probe=probe,
            cancel_requested=cancel_requested,
            preparation_process_observer=observed.append,
            preparation_warning_observer=lambda _warning: None,
        )

    assert str(raised.value) == message
    assert len(observed) == 2
    assert isinstance(observed[0], OwnedSshdProcess)
    assert observed[1] is None
    assert process.terminated is True
    assert process.wait_calls == 1
    assert list(config_dir.iterdir()) == []


def test_sshd_readiness_timeout_is_fixed_and_cleans_owned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = _create_config_dir(tmp_path)
    process = FakeSshdProcess()
    monkeypatch.setattr(ssh_module, "_SSHD_READINESS_TIMEOUT_SECONDS", 0.0)

    def unavailable(_port: int, _timeout: float) -> bytes:
        raise OSError("not ready")

    with pytest.raises(
        SshdReadinessError,
        match=r"^sshd readiness timed out$",
    ):
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            environment={},
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            config_dir=config_dir,
            credential_command_runner=RecordingRunner(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=RecordingCommandRunner(),
            preflight_command_runner=RecordingCommandRunner(),
            process_starter=RecordingProcessStarter(process),
            readiness_probe=unavailable,
            preparation_warning_observer=lambda _warning: None,
        )

    assert process.terminated is True
    assert process.wait_calls == 1
    assert list(config_dir.iterdir()) == []


def test_sshd_valid_banner_is_followed_by_final_child_poll(tmp_path: Path) -> None:
    class ExitAfterBannerProcess(FakeSshdProcess):
        def __init__(self) -> None:
            super().__init__()
            self.poll_calls = 0

        def poll(self) -> int | None:
            self.poll_calls += 1
            if self.poll_calls == 1:
                return None
            self.returncode = 23
            return self.returncode

    config_dir = _create_config_dir(tmp_path)
    process = ExitAfterBannerProcess()

    with pytest.raises(
        SshdReadinessError,
        match=r"^sshd exited before becoming ready$",
    ):
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            environment={},
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            config_dir=config_dir,
            credential_command_runner=RecordingRunner(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=RecordingCommandRunner(),
            preflight_command_runner=RecordingCommandRunner(),
            process_starter=RecordingProcessStarter(process),
            readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
            preparation_warning_observer=lambda _warning: None,
        )

    assert process.poll_calls >= 2
    assert process.wait_calls == 1
    assert list(config_dir.iterdir()) == []


def test_sshd_readiness_checks_cancellation_after_probe(tmp_path: Path) -> None:
    config_dir = _create_config_dir(tmp_path)
    process = FakeSshdProcess()
    cancelled = False

    def cancel_during_probe(_port: int, _timeout: float) -> bytes:
        nonlocal cancelled
        cancelled = True
        return b"SSH-2.0-test\n"

    with pytest.raises(
        SshdReadinessError,
        match=r"^sshd startup was cancelled$",
    ):
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            environment={},
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            config_dir=config_dir,
            credential_command_runner=RecordingRunner(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=RecordingCommandRunner(),
            preflight_command_runner=RecordingCommandRunner(),
            process_starter=RecordingProcessStarter(process),
            readiness_probe=cancel_during_probe,
            cancel_requested=lambda: cancelled,
            preparation_warning_observer=lambda _warning: None,
        )

    assert process.terminated is True
    assert process.wait_calls == 1
    assert list(config_dir.iterdir()) == []


def test_sshd_readiness_poll_error_is_fixed_and_retains_config(
    tmp_path: Path,
) -> None:
    class PollErrorProcess(FakeSshdProcess):
        def poll(self) -> int | None:
            raise OSError("raw-poll-sentinel")

    config_dir = _create_config_dir(tmp_path)
    process = PollErrorProcess()

    with pytest.raises(
        SshdReadinessError,
        match=r"^sshd readiness check failed$",
    ) as raised:
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            environment={b"POLL_SECRET": b"private-poll-value"},
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            config_dir=config_dir,
            credential_command_runner=RecordingRunner(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=RecordingCommandRunner(),
            preflight_command_runner=RecordingCommandRunner(),
            process_starter=RecordingProcessStarter(process),
            readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
            preparation_warning_observer=lambda _warning: None,
        )

    assert str(raised.value) == "sshd readiness check failed"
    assert "raw-poll-sentinel" not in str(raised.value)
    assert list(config_dir.glob("sshd_config.*"))
    assert process.wait_calls == 0


def test_owned_sshd_process_does_not_remove_replacement_identity(
    tmp_path: Path,
) -> None:
    config_dir = _create_config_dir(tmp_path)
    process_starter = RecordingProcessStarter()
    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate(
            {"system": {"ssh": {"enable": True, "password": "secret"}}}
        ),
        environment={},
        root_home=tmp_path / "root",
        runtime_dir=tmp_path / "run" / "sshd",
        config_dir=config_dir,
        credential_command_runner=RecordingRunner(),
        config_owner_uid=os.getuid(),
        config_owner_gid=os.getgid(),
        command_runner=RecordingCommandRunner(),
        preflight_command_runner=RecordingCommandRunner(),
        process_starter=process_starter,
        readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
        preparation_warning_observer=lambda _warning: None,
    )
    assert isinstance(result, OwnedSshdProcess)
    config_path = Path(process_starter.calls[0].argv[2])
    original_path = config_dir / "original-config"
    os.replace(config_path, original_path)
    config_path.write_bytes(b"replacement\n")

    assert result.wait() == 0

    assert config_path.read_bytes() == b"replacement\n"
    assert original_path.exists()


def test_owned_sshd_process_overlapping_terminal_observation_cleans_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RacingSshdProcess(FakeSshdProcess):
        def __init__(self) -> None:
            super().__init__()
            self.racing = False
            self.barrier = threading.Barrier(2)

        def wait(self) -> int:
            if self.racing:
                self.barrier.wait(timeout=1)
                self.returncode = 0
                return 0
            return super().wait()

        def poll(self) -> int | None:
            if self.racing:
                self.barrier.wait(timeout=1)
                self.returncode = 0
                return 0
            return super().poll()

    config_dir = _create_config_dir(tmp_path)
    process = RacingSshdProcess()
    process_starter = RecordingProcessStarter(process)
    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate(
            {"system": {"ssh": {"enable": True, "password": "secret"}}}
        ),
        environment={},
        root_home=tmp_path / "root",
        runtime_dir=tmp_path / "run" / "sshd",
        config_dir=config_dir,
        credential_command_runner=RecordingRunner(),
        config_owner_uid=os.getuid(),
        config_owner_gid=os.getgid(),
        command_runner=RecordingCommandRunner(),
        preflight_command_runner=RecordingCommandRunner(),
        process_starter=process_starter,
        readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
        preparation_warning_observer=lambda _warning: None,
    )
    assert isinstance(result, OwnedSshdProcess)
    config_path = Path(process_starter.calls[0].argv[2])
    real_unlink = ssh_module._unlink_owned_sshd_config
    cleanup_calls = 0

    def record_cleanup(config: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        real_unlink(config)  # type: ignore[arg-type]

    monkeypatch.setattr(ssh_module, "_unlink_owned_sshd_config", record_cleanup)
    process.racing = True
    outcomes: list[int | None] = []
    failures: list[BaseException] = []

    def observe(operation: Callable[[], int | None]) -> None:
        try:
            outcomes.append(operation())
        except BaseException as error:
            failures.append(error)

    threads = [
        threading.Thread(target=observe, args=(result.wait,)),
        threading.Thread(target=observe, args=(result.poll,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert failures == []
    assert outcomes == [0, 0]
    assert cleanup_calls == 1
    assert not config_path.exists()


def test_owned_sshd_process_cleanup_failure_does_not_replace_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = _create_config_dir(tmp_path)
    process_starter = RecordingProcessStarter()
    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate(
            {"system": {"ssh": {"enable": True, "password": "secret"}}}
        ),
        environment={},
        root_home=tmp_path / "root",
        runtime_dir=tmp_path / "run" / "sshd",
        config_dir=config_dir,
        credential_command_runner=RecordingRunner(),
        config_owner_uid=os.getuid(),
        config_owner_gid=os.getgid(),
        command_runner=RecordingCommandRunner(),
        preflight_command_runner=RecordingCommandRunner(),
        process_starter=process_starter,
        readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
        preparation_warning_observer=lambda _warning: None,
    )
    assert isinstance(result, OwnedSshdProcess)
    config_path = Path(process_starter.calls[0].argv[2])
    cleanup_calls = 0

    def fail_cleanup(_config: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise OSError("raw-cleanup-sentinel")

    monkeypatch.setattr(ssh_module, "_unlink_owned_sshd_config", fail_cleanup)

    assert result.wait() == 0
    assert result.poll() == 0
    assert cleanup_calls == 1
    assert config_path.exists()


@pytest.mark.parametrize("operation", ["wait", "poll"])
def test_owned_sshd_process_observation_error_retains_config(
    tmp_path: Path,
    operation: str,
) -> None:
    class ObservationErrorProcess(FakeSshdProcess):
        fail_observation = False

        def wait(self) -> int:
            if self.fail_observation:
                raise OSError("raw-wait-sentinel")
            return super().wait()

        def poll(self) -> int | None:
            if self.fail_observation:
                raise OSError("raw-poll-sentinel")
            return super().poll()

    config_dir = _create_config_dir(tmp_path)
    process = ObservationErrorProcess()
    process_starter = RecordingProcessStarter(process)
    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate(
            {"system": {"ssh": {"enable": True, "password": "secret"}}}
        ),
        environment={},
        root_home=tmp_path / "root",
        runtime_dir=tmp_path / "run" / "sshd",
        config_dir=config_dir,
        credential_command_runner=RecordingRunner(),
        config_owner_uid=os.getuid(),
        config_owner_gid=os.getgid(),
        command_runner=RecordingCommandRunner(),
        preflight_command_runner=RecordingCommandRunner(),
        process_starter=process_starter,
        readiness_probe=lambda _port, _timeout: b"SSH-2.0-test\n",
        preparation_warning_observer=lambda _warning: None,
    )
    assert isinstance(result, OwnedSshdProcess)
    config_path = Path(process_starter.calls[0].argv[2])
    process.fail_observation = True

    with pytest.raises(OSError):
        getattr(result, operation)()

    assert config_path.exists()


def test_ssh_default_runner_missing_executables_are_not_disclosed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = FileNotFoundError("raw-missing-executable-sentinel")

    def fail_popen(*_args: object, **_kwargs: object) -> None:
        raise missing

    monkeypatch.setattr(ssh_module.subprocess, "Popen", fail_popen)

    with pytest.raises(SshdStartupError) as command_error:
        ssh_module._run_command(
            ["/credential-url/ssh-keygen"],
            description="generate OpenSSH host keys",
        )
    with pytest.raises(SshCredentialPreparationError) as sensitive_error:
        ssh_module._run_sensitive_command(
            ["credential-bearing-command"],
            input_data=b"secret",
            description="set root SSH password",
        )
    with pytest.raises(SshdStartupError) as start_error:
        ssh_module._start_process(
            ["/credential-url/sshd"],
            description="start sshd",
        )

    for error in (command_error.value, sensitive_error.value, start_error.value):
        assert isinstance(error.__cause__, FileNotFoundError)
        assert "raw-missing-executable-sentinel" not in str(error)
        assert "credential" not in str(error)


def test_start_sshd_if_enabled_fails_when_sshd_exits_during_startup(
    tmp_path: Path,
) -> None:
    config_dir = _create_config_dir(tmp_path)

    with pytest.raises(SshdReadinessError) as raised:
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            environment={},
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            config_dir=config_dir,
            credential_command_runner=RecordingRunner(),
            config_owner_uid=os.getuid(),
            config_owner_gid=os.getgid(),
            command_runner=RecordingCommandRunner(),
            preflight_command_runner=RecordingCommandRunner(),
            process_starter=RecordingProcessStarter(FakeSshdProcess(returncode=255)),
            preparation_warning_observer=lambda _warning: None,
        )

    assert str(raised.value) == "sshd exited before becoming ready"
    assert list(config_dir.glob("sshd_config.*")) == []
