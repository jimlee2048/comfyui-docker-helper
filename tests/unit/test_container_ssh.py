"""Tests for root SSH credential preparation."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from comfyui_docker_helper.config import RuntimeConfig, RuntimeSystemSshConfig
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.container.ssh import (
    RootSshCredentialPreparationStatus,
    SshCredentialPreparationError,
    SshdStartupError,
    build_sshd_argv,
    prepare_root_ssh_credentials,
    start_sshd_if_enabled,
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


@dataclass(frozen=True, slots=True)
class CommandCall:
    argv: list[str]
    input_data: bytes
    description: str


class RecordingRunner:
    def __init__(self, returncodes: tuple[int, ...] = ()) -> None:
        self.calls: list[CommandCall] = []
        self._returncodes = list(returncodes)

    def __call__(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        input_data: bytes,
        description: str,
    ) -> int:
        self.calls.append(
            CommandCall(
                argv=list(argv),
                input_data=input_data,
                description=description,
            )
        )
        if self._returncodes:
            return self._returncodes.pop(0)
        return 0


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

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


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


class OwnershipRecorder:
    def __init__(self) -> None:
        self.chown_calls: list[tuple[Path, int, int]] = []
        self.chmod_calls: list[tuple[Path, int]] = []

    def chown(self, path: str | Path, uid: int, gid: int) -> None:
        self.chown_calls.append((Path(path), uid, gid))

    def chmod(self, path: str | Path, mode: int) -> None:
        self.chmod_calls.append((Path(path), mode))
        Path(path).chmod(mode)


def test_no_credentials_prepare_no_files_or_commands(tmp_path: Path) -> None:
    runner = RecordingRunner()
    ownership = OwnershipRecorder()

    status = prepare_root_ssh_credentials(
        RuntimeSystemSshConfig(),
        root_home=tmp_path / "root",
        command_runner=runner,
        chown=ownership.chown,
        chmod=ownership.chmod,
    )

    assert status.ssh_enabled is False
    assert status.has_credentials is False
    assert status.public_key_count == 0
    assert status.password_configured is False
    assert status.authorized_keys_path is None
    assert runner.calls == []
    assert ownership.chown_calls == []
    assert ownership.chmod_calls == []
    assert not (tmp_path / "root").exists()


def test_disabled_ssh_with_credentials_is_noop(tmp_path: Path) -> None:
    runner = RecordingRunner()
    ownership = OwnershipRecorder()

    status = prepare_root_ssh_credentials(
        RuntimeSystemSshConfig(
            enable=False,
            password="secret",
            pub_keys=[VALID_SSH_KEY],
        ),
        root_home=tmp_path / "root",
        command_runner=runner,
        chown=ownership.chown,
        chmod=ownership.chmod,
    )

    assert status.ssh_enabled is False
    assert status.has_credentials is False
    assert status.public_key_count == 0
    assert status.password_configured is False
    assert status.authorized_keys_path is None
    assert status.root_password_set is False
    assert status.root_unlocked is False
    assert runner.calls == []
    assert ownership.chown_calls == []
    assert ownership.chmod_calls == []
    assert not (tmp_path / "root").exists()


def test_enabled_ssh_without_credentials_reports_no_credentials_no_side_effects(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    ownership = OwnershipRecorder()

    status = prepare_root_ssh_credentials(
        RuntimeSystemSshConfig(enable=True),
        root_home=tmp_path / "root",
        command_runner=runner,
        chown=ownership.chown,
        chmod=ownership.chmod,
    )

    assert status.ssh_enabled is True
    assert status.has_credentials is False
    assert status.public_key_count == 0
    assert status.password_configured is False
    assert status.authorized_keys_path is None
    assert status.root_password_set is False
    assert status.root_unlocked is False
    assert runner.calls == []
    assert ownership.chown_calls == []
    assert ownership.chmod_calls == []
    assert not (tmp_path / "root").exists()


def test_public_keys_prepare_authorized_keys_permissions_and_ownership(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    ownership = OwnershipRecorder()
    root_home = tmp_path / "root"

    status = prepare_root_ssh_credentials(
        RuntimeSystemSshConfig(
            enable=True,
            pub_keys=["  ", f" {VALID_SSH_KEY} ", SECOND_SSH_KEY],
        ),
        root_home=root_home,
        command_runner=runner,
        chown=ownership.chown,
        chmod=ownership.chmod,
    )

    ssh_dir = root_home / ".ssh"
    authorized_keys = ssh_dir / "authorized_keys"
    assert status.has_credentials is True
    assert status.public_key_count == 2
    assert status.password_configured is False
    assert status.authorized_keys_path == authorized_keys
    assert authorized_keys.read_text(encoding="utf-8") == (
        f"{VALID_SSH_KEY}\n{SECOND_SSH_KEY}\n"
    )
    assert stat.S_IMODE(ssh_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(authorized_keys.stat().st_mode) == 0o600
    assert ownership.chown_calls == [
        (ssh_dir, 0, 0),
        (authorized_keys, 0, 0),
    ]
    assert ownership.chmod_calls == [
        (ssh_dir, 0o700),
        (authorized_keys, 0o600),
    ]
    assert runner.calls == []


def test_password_only_sets_password_via_stdin_and_unlocks_root(tmp_path: Path) -> None:
    runner = RecordingRunner()
    password = "secret with spaces"

    status = prepare_root_ssh_credentials(
        RuntimeSystemSshConfig(enable=True, password=password),
        root_home=tmp_path / "root",
        command_runner=runner,
    )

    assert status.has_credentials is True
    assert status.public_key_count == 0
    assert status.password_configured is True
    assert status.root_password_set is True
    assert status.root_unlocked is True
    assert status.authorized_keys_path is None
    assert [call.argv for call in runner.calls] == [
        ["chpasswd"],
        ["passwd", "-u", "root"],
    ]
    assert runner.calls[0].input_data == b"root:secret with spaces\n"
    assert runner.calls[1].input_data == b""
    for call in runner.calls:
        assert password not in call.argv
        assert password not in call.description


def test_runtime_config_with_password_and_keys_prepares_both_credentials(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    ownership = OwnershipRecorder()
    config = RuntimeConfig.model_validate(
        {
            "system": {
                "ssh": {
                    "enable": True,
                    "password": "super-secret",
                    "pub_keys": [VALID_SSH_KEY],
                }
            }
        }
    )

    status = prepare_root_ssh_credentials(
        config,
        root_home=tmp_path / "root",
        command_runner=runner,
        chown=ownership.chown,
        chmod=ownership.chmod,
    )

    assert status.ssh_enabled is True
    assert status.has_credentials is True
    assert status.public_key_count == 1
    assert status.password_configured is True
    assert status.root_password_set is True
    assert status.root_unlocked is True
    assert status.authorized_keys_path == tmp_path / "root" / ".ssh" / "authorized_keys"
    assert [call.argv for call in runner.calls] == [
        ["chpasswd"],
        ["passwd", "-u", "root"],
    ]
    assert runner.calls[0].input_data == b"root:super-secret\n"


@pytest.mark.parametrize("password", ["line\nbreak", "carriage\rreturn", "nul\x00byte"])
def test_unsafe_password_content_is_rejected_without_runner_or_leak(
    tmp_path: Path,
    password: str,
) -> None:
    runner = RecordingRunner()

    with pytest.raises(SshCredentialPreparationError) as raised:
        prepare_root_ssh_credentials(
            RuntimeSystemSshConfig(enable=True, password=password),
            root_home=tmp_path / "root",
            command_runner=runner,
        )

    assert runner.calls == []
    assert password not in str(raised.value)
    assert "line breaks or NUL bytes" in str(raised.value)


def test_public_key_plus_unsafe_password_is_rejected_before_file_side_effects(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    ownership = OwnershipRecorder()
    password = "unsafe\npassword"

    with pytest.raises(SshCredentialPreparationError) as raised:
        prepare_root_ssh_credentials(
            RuntimeSystemSshConfig(
                enable=True,
                password=password,
                pub_keys=[VALID_SSH_KEY],
            ),
            root_home=tmp_path / "root",
            command_runner=runner,
            chown=ownership.chown,
            chmod=ownership.chmod,
        )

    assert runner.calls == []
    assert ownership.chown_calls == []
    assert ownership.chmod_calls == []
    assert not (tmp_path / "root" / ".ssh").exists()
    assert not (tmp_path / "root" / ".ssh" / "authorized_keys").exists()
    assert password not in str(raised.value)


def test_password_command_failure_does_not_leak_credential_material(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(returncodes=(17,))
    password = "top-secret-password"

    with pytest.raises(SshCredentialPreparationError) as raised:
        prepare_root_ssh_credentials(
            RuntimeSystemSshConfig(enable=True, password=password),
            root_home=tmp_path / "root",
            command_runner=runner,
        )

    assert "top-secret-password" not in str(raised.value)
    assert "top-secret-password" not in runner.calls[0].description
    assert "top-secret-password" not in " ".join(runner.calls[0].argv)
    assert runner.calls[0].input_data == b"root:top-secret-password\n"


def test_build_sshd_argv_enforces_effective_config_without_credentials() -> None:
    status = RootSshCredentialPreparationStatus(
        ssh_enabled=True,
        public_key_count=1,
        password_configured=False,
    )

    argv = build_sshd_argv(
        RuntimeSystemSshConfig(enable=True, port=2022, pub_keys=[VALID_SSH_KEY]),
        status,
    )

    assert argv == [
        "/usr/sbin/sshd",
        "-f",
        "/dev/null",
        "-D",
        "-e",
        "-o",
        "Port=2022",
        "-o",
        "PermitRootLogin=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PubkeyAuthentication=yes",
        "-o",
        "AuthorizedKeysFile=/root/.ssh/authorized_keys",
    ]


def test_start_sshd_if_enabled_with_no_credentials_warns_without_start(
    tmp_path: Path,
) -> None:
    messages: list[str] = []
    command_runner = RecordingCommandRunner()
    process_starter = RecordingProcessStarter()

    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate({"system": {"ssh": {"enable": True}}}),
        runtime=ContainerRuntime(),
        root_home=tmp_path / "root",
        runtime_dir=tmp_path / "run" / "sshd",
        command_runner=command_runner,
        process_starter=process_starter,
        log=messages.append,
    )

    assert result is None
    assert messages == [
        "WARNING: SSH is enabled but no root SSH credentials are configured"
    ]
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
    root_home = tmp_path / "root"
    runtime_dir = tmp_path / "run" / "sshd"

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
        runtime=ContainerRuntime(),
        root_home=root_home,
        runtime_dir=runtime_dir,
        credential_command_runner=credential_runner,
        command_runner=command_runner,
        process_starter=process_starter,
        log=lambda message: None,
    )

    assert result is process
    assert command_runner.calls == [
        PlainCommandCall(["/usr/bin/ssh-keygen", "-A"], "generate OpenSSH host keys")
    ]
    assert runtime_dir.is_dir()
    assert [call.argv for call in credential_runner.calls] == [
        ["chpasswd"],
        ["passwd", "-u", "root"],
    ]
    assert process_starter.calls == [
        PlainCommandCall(
            [
                "/usr/sbin/sshd",
                "-f",
                "/dev/null",
                "-D",
                "-e",
                "-o",
                "Port=2222",
                "-o",
                "PermitRootLogin=yes",
                "-o",
                "PasswordAuthentication=yes",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "AuthorizedKeysFile=/root/.ssh/authorized_keys",
            ],
            "start sshd",
        )
    ]
    assert "secret" not in " ".join(process_starter.calls[0].argv)


def test_start_sshd_if_enabled_key_only_writes_keys_and_disables_password_auth(
    tmp_path: Path,
) -> None:
    credential_runner = RecordingRunner()
    command_runner = RecordingCommandRunner()
    process = FakeSshdProcess()
    process_starter = RecordingProcessStarter(process)
    ownership = OwnershipRecorder()
    messages: list[str] = []
    root_home = tmp_path / "root"
    runtime_dir = tmp_path / "run" / "sshd"

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
        runtime=ContainerRuntime(),
        root_home=root_home,
        runtime_dir=runtime_dir,
        credential_command_runner=credential_runner,
        credential_chown=ownership.chown,
        credential_chmod=ownership.chmod,
        command_runner=command_runner,
        process_starter=process_starter,
        log=messages.append,
    )

    authorized_keys = root_home / ".ssh" / "authorized_keys"
    assert result is process
    assert authorized_keys.read_text(encoding="utf-8") == f"{VALID_SSH_KEY}\n"
    assert ownership.chown_calls == [
        (root_home / ".ssh", 0, 0),
        (authorized_keys, 0, 0),
    ]
    assert ownership.chmod_calls == [
        (root_home / ".ssh", 0o700),
        (authorized_keys, 0o600),
    ]
    assert command_runner.calls == [
        PlainCommandCall(["/usr/bin/ssh-keygen", "-A"], "generate OpenSSH host keys")
    ]
    assert runtime_dir.is_dir()
    assert credential_runner.calls == []
    assert process_starter.calls == [
        PlainCommandCall(
            [
                "/usr/sbin/sshd",
                "-f",
                "/dev/null",
                "-D",
                "-e",
                "-o",
                "Port=2222",
                "-o",
                "PermitRootLogin=yes",
                "-o",
                "PasswordAuthentication=no",
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
    assert VALID_SSH_KEY not in " ".join(process_starter.calls[0].argv)
    assert VALID_SSH_KEY not in "\n".join(messages)


def test_start_sshd_if_enabled_fails_when_host_key_generation_fails(
    tmp_path: Path,
) -> None:
    credential_runner = RecordingRunner()
    command_runner = RecordingCommandRunner(returncodes=(19,))
    process_starter = RecordingProcessStarter()
    ownership = OwnershipRecorder()
    password = "secret-password"
    root_home = tmp_path / "root"
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
            runtime=ContainerRuntime(),
            root_home=root_home,
            runtime_dir=runtime_dir,
            credential_command_runner=credential_runner,
            credential_chown=ownership.chown,
            credential_chmod=ownership.chmod,
            command_runner=command_runner,
            process_starter=process_starter,
            log=lambda message: None,
        )

    error = str(raised.value)
    assert "generate OpenSSH host keys failed with exit code 19" in error
    assert password not in error
    assert VALID_SSH_KEY not in error
    assert command_runner.calls == [
        PlainCommandCall(["/usr/bin/ssh-keygen", "-A"], "generate OpenSSH host keys")
    ]
    assert process_starter.calls == []
    assert not runtime_dir.exists()


def test_start_sshd_if_enabled_fails_when_sshd_exits_during_startup(
    tmp_path: Path,
) -> None:
    with pytest.raises(SshdStartupError) as raised:
        start_sshd_if_enabled(
            RuntimeConfig.model_validate(
                {"system": {"ssh": {"enable": True, "password": "secret"}}}
            ),
            runtime=ContainerRuntime(),
            root_home=tmp_path / "root",
            runtime_dir=tmp_path / "run" / "sshd",
            credential_command_runner=RecordingRunner(),
            command_runner=RecordingCommandRunner(),
            process_starter=RecordingProcessStarter(FakeSshdProcess(returncode=255)),
            log=lambda message: None,
        )

    assert "sshd exited during startup with code 255" in str(raised.value)
