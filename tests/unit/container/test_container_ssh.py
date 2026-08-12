"""Tests for root SSH credential preparation."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

import comfyui_docker_helper.container.ssh as ssh_module
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
        self.fchown_calls: list[tuple[int, int]] = []
        self.fchmod_calls: list[int] = []

    def chown(self, path: str | Path, uid: int, gid: int) -> None:
        self.chown_calls.append((Path(path), uid, gid))

    def chmod(self, path: str | Path, mode: int) -> None:
        self.chmod_calls.append((Path(path), mode))
        Path(path).chmod(mode)

    def fchown(self, descriptor: int, uid: int, gid: int) -> None:
        del descriptor
        self.fchown_calls.append((uid, gid))

    def fchmod(self, descriptor: int, mode: int) -> None:
        self.fchmod_calls.append(mode)
        os.fchmod(descriptor, mode)


def _create_root_home(tmp_path: Path) -> Path:
    root_home = tmp_path / "root"
    root_home.mkdir(mode=0o700)
    return root_home


def _prepare_public_keys(root_home: Path) -> RootSshCredentialPreparationStatus:
    return prepare_root_ssh_credentials(
        RuntimeSystemSshConfig(enable=True, pub_keys=[VALID_SSH_KEY]),
        root_home=root_home,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )


# Credential tests protect no-op paths, safe mode admission and warning delivery,
# side effects, and secret redaction.
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


def test_start_sshd_logs_credential_path_mode_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warning = "WARNING: existing root SSH directory mode is nonstandard"

    def prepare_with_warning(
        _config: RuntimeSystemSshConfig,
        **_kwargs: object,
    ) -> RootSshCredentialPreparationStatus:
        return RootSshCredentialPreparationStatus(
            ssh_enabled=True,
            public_key_count=1,
            password_configured=False,
            authorized_keys_path=tmp_path / "root" / ".ssh" / "authorized_keys",
            warnings=(warning,),
        )

    monkeypatch.setattr(
        ssh_module,
        "prepare_root_ssh_credentials",
        prepare_with_warning,
    )
    command_runner = RecordingCommandRunner()
    process_starter = RecordingProcessStarter()
    messages: list[str] = []

    result = start_sshd_if_enabled(
        RuntimeConfig.model_validate(
            {"system": {"ssh": {"enable": True, "pub_keys": [VALID_SSH_KEY]}}}
        ),
        runtime=ContainerRuntime(),
        runtime_dir=tmp_path / "run" / "sshd",
        command_runner=command_runner,
        process_starter=process_starter,
        log=messages.append,
    )

    assert result is process_starter.process
    assert messages == [warning]
    assert VALID_SSH_KEY not in "\n".join(messages)


def test_public_keys_prepare_authorized_keys_permissions_and_ownership(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    ownership = OwnershipRecorder()
    root_home = _create_root_home(tmp_path)

    status = prepare_root_ssh_credentials(
        RuntimeSystemSshConfig(
            enable=True,
            pub_keys=["  ", f" {VALID_SSH_KEY} ", SECOND_SSH_KEY],
        ),
        root_home=root_home,
        command_runner=runner,
        chown=ownership.chown,
        chmod=ownership.chmod,
        fchown=ownership.fchown,
        fchmod=ownership.fchmod,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
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
        (ssh_dir, os.getuid(), os.getgid()),
    ]
    assert ownership.chmod_calls == [
        (ssh_dir, 0o700),
    ]
    assert ownership.fchown_calls == [(os.getuid(), os.getgid())]
    assert ownership.fchmod_calls == [0o600]
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
    root_home = _create_root_home(tmp_path)
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
        root_home=root_home,
        command_runner=runner,
        chown=ownership.chown,
        chmod=ownership.chmod,
        fchown=ownership.fchown,
        fchmod=ownership.fchmod,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert status.ssh_enabled is True
    assert status.has_credentials is True
    assert status.public_key_count == 1
    assert status.password_configured is True
    assert status.root_password_set is True
    assert status.root_unlocked is True
    assert status.authorized_keys_path == root_home / ".ssh" / "authorized_keys"
    assert [call.argv for call in runner.calls] == [
        ["chpasswd"],
        ["passwd", "-u", "root"],
    ]
    assert runner.calls[0].input_data == b"root:super-secret\n"


@pytest.mark.parametrize(
    "password",
    [
        pytest.param("line\nbreak", id="line-feed"),
        pytest.param("carriage\rreturn", id="carriage-return"),
        pytest.param("nul\x00byte", id="nul-byte"),
    ],
)
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


@pytest.mark.parametrize(
    "state",
    [
        "absent",
        "symlink",
        "dangling",
        "file",
        "fifo",
        "unsafe_mode",
    ],
)
def test_root_home_rejects_unsafe_static_states_without_key_disclosure(
    tmp_path: Path,
    state: str,
) -> None:
    root_home = tmp_path / "root"
    if state == "symlink":
        destination = tmp_path / "redirected-root"
        destination.mkdir()
        root_home.symlink_to(destination, target_is_directory=True)
    elif state == "dangling":
        root_home.symlink_to(tmp_path / "missing-root", target_is_directory=True)
    elif state == "file":
        root_home.write_text("unchanged", encoding="utf-8")
    elif state == "fifo":
        os.mkfifo(root_home)
    elif state == "unsafe_mode":
        root_home.mkdir()
        root_home.chmod(0o777)

    with pytest.raises(SshCredentialPreparationError) as raised:
        _prepare_public_keys(root_home)

    assert str(raised.value) == (
        "root SSH home must be an existing root-owned directory with a safe mode"
    )
    assert VALID_SSH_KEY not in str(raised.value)


def test_root_home_rejects_wrong_ownership_via_owner_seam(tmp_path: Path) -> None:
    root_home = _create_root_home(tmp_path)

    with pytest.raises(SshCredentialPreparationError) as raised:
        prepare_root_ssh_credentials(
            RuntimeSystemSshConfig(enable=True, pub_keys=[VALID_SSH_KEY]),
            root_home=root_home,
            owner_uid=os.getuid() + 1,
            owner_gid=os.getgid(),
        )

    assert "root SSH home" in str(raised.value)
    assert not (root_home / ".ssh").exists()


@pytest.mark.parametrize(
    ("path_kind", "safe_mode"),
    [
        pytest.param("root-home", stat.S_IFDIR | 0o700, id="root-home"),
        pytest.param("ssh-directory", stat.S_IFDIR | 0o700, id="ssh-directory"),
        pytest.param("authorized-keys", stat.S_IFREG | 0o600, id="authorized-keys"),
    ],
)
def test_existing_root_ssh_paths_admit_safe_non_root_gid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_kind: str,
    safe_mode: int,
) -> None:
    metadata = os.stat_result((safe_mode, 0, 0, 1, 0, 1234, 0, 0, 0, 0))
    monkeypatch.setattr(Path, "lstat", lambda _path: metadata)
    path = tmp_path / path_kind

    if path_kind == "root-home":
        ssh_module._validate_root_home(path, owner_uid=0)
    elif path_kind == "ssh-directory":
        ownership = OwnershipRecorder()
        created, warning = ssh_module._ensure_root_ssh_directory(
            path,
            chown=ownership.chown,
            chmod=ownership.chmod,
            owner_uid=0,
            owner_gid=0,
        )
        assert created is False
        assert warning is None
        assert ownership.chown_calls == []
        assert ownership.chmod_calls == []
    else:
        assert ssh_module._validate_authorized_keys_target(path, owner_uid=0) is None


@pytest.mark.parametrize(
    "state",
    ["symlink", "dangling", "file", "fifo", "unsafe_mode"],
)
def test_ssh_directory_rejects_unsafe_static_states(
    tmp_path: Path,
    state: str,
) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    if state == "symlink":
        destination = tmp_path / "redirected-ssh"
        destination.mkdir()
        ssh_dir.symlink_to(destination, target_is_directory=True)
    elif state == "dangling":
        ssh_dir.symlink_to(tmp_path / "missing-ssh", target_is_directory=True)
    elif state == "file":
        ssh_dir.write_text("unchanged", encoding="utf-8")
    elif state == "fifo":
        os.mkfifo(ssh_dir)
    else:
        ssh_dir.mkdir(mode=0o700)
        ssh_dir.chmod(0o777)

    with pytest.raises(SshCredentialPreparationError) as raised:
        _prepare_public_keys(root_home)

    assert str(raised.value) == (
        "root SSH directory must be root-owned and not writable by group or other"
    )
    assert VALID_SSH_KEY not in str(raised.value)


def test_ssh_directory_rejects_wrong_ownership_via_owner_seam(
    tmp_path: Path,
) -> None:
    ssh_dir = _create_root_home(tmp_path) / ".ssh"
    ssh_dir.mkdir(mode=0o700)

    with pytest.raises(SshCredentialPreparationError) as raised:
        ssh_module._ensure_root_ssh_directory(
            ssh_dir,
            chown=os.chown,
            chmod=os.chmod,
            owner_uid=os.getuid() + 1,
            owner_gid=os.getgid(),
        )

    assert "root SSH directory" in str(raised.value)


@pytest.mark.parametrize(
    "state",
    ["symlink", "dangling", "directory", "fifo", "unsafe_mode"],
)
def test_authorized_keys_rejects_unsafe_static_states_and_preserves_redirect(
    tmp_path: Path,
    state: str,
) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    target = ssh_dir / "authorized_keys"
    redirected = tmp_path / "redirected-keys"
    redirected.write_text("victim\n", encoding="utf-8")
    if state == "symlink":
        target.symlink_to(redirected)
    elif state == "dangling":
        target.symlink_to(tmp_path / "missing-keys")
    elif state == "directory":
        target.mkdir()
    elif state == "fifo":
        os.mkfifo(target)
    else:
        target.write_text("old\n", encoding="utf-8")
        target.chmod(0o664)

    with pytest.raises(SshCredentialPreparationError) as raised:
        _prepare_public_keys(root_home)

    assert str(raised.value) == (
        "root SSH authorized keys must be a root-owned regular file that is not "
        "writable by group or other"
    )
    assert redirected.read_text(encoding="utf-8") == "victim\n"
    assert VALID_SSH_KEY not in str(raised.value)


def test_authorized_keys_rejects_wrong_ownership_via_owner_seam(
    tmp_path: Path,
) -> None:
    target = _create_root_home(tmp_path) / "authorized_keys"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o600)

    with pytest.raises(SshCredentialPreparationError) as raised:
        ssh_module._validate_authorized_keys_target(
            target,
            owner_uid=os.getuid() + 1,
        )

    assert "root SSH authorized keys" in str(raised.value)


def test_safe_noncanonical_ssh_modes_warn_and_are_not_rejected(
    tmp_path: Path,
) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    ssh_dir.mkdir(mode=0o755)
    target = ssh_dir / "authorized_keys"
    target.write_text("old key material\n", encoding="utf-8")
    target.chmod(0o644)
    old_inode = target.stat().st_ino
    root_home.chmod(0o500)
    ownership = OwnershipRecorder()

    status = prepare_root_ssh_credentials(
        RuntimeSystemSshConfig(enable=True, pub_keys=[VALID_SSH_KEY]),
        root_home=root_home,
        chown=ownership.chown,
        chmod=ownership.chmod,
        fchown=ownership.fchown,
        fchmod=ownership.fchmod,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert len(status.warnings) == 2
    directory_warning, file_warning = status.warnings
    assert "directory" in directory_warning.lower()
    assert "preserving" in directory_warning.lower()
    assert "not writable by group or other" in directory_warning.lower()
    assert "authorized keys" in file_warning.lower()
    assert "replacing" in file_warning.lower()
    assert "atomically" in file_warning.lower()
    assert "0600" in file_warning
    warning_text = "\n".join(status.warnings)
    for sensitive_value in (*VALID_SSH_KEY.split(), "old key material"):
        assert sensitive_value not in warning_text
    assert stat.S_IMODE(ssh_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_ino != old_inode
    assert target.read_text(encoding="utf-8") == f"{VALID_SSH_KEY}\n"
    assert ownership.chown_calls == []
    assert ownership.chmod_calls == []
    assert ownership.fchown_calls == [(os.getuid(), os.getgid())]
    assert ownership.fchmod_calls == [0o600]


def test_interrupted_temporary_write_preserves_old_target_and_cleans_owned_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    target = ssh_dir / "authorized_keys"
    target.write_text("old key material\n", encoding="utf-8")
    target.chmod(0o600)

    def interrupt_fsync(descriptor: int) -> None:
        del descriptor
        raise KeyboardInterrupt

    monkeypatch.setattr(ssh_module.os, "fsync", interrupt_fsync)

    with pytest.raises(KeyboardInterrupt):
        _prepare_public_keys(root_home)

    assert target.read_text(encoding="utf-8") == "old key material\n"
    assert list(ssh_dir.glob(".authorized_keys.*.tmp")) == []


def test_successful_write_atomically_replaces_existing_regular_target(
    tmp_path: Path,
) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    target = ssh_dir / "authorized_keys"
    target.write_text("old key material\n", encoding="utf-8")
    target.chmod(0o600)
    old_inode = target.stat().st_ino

    _prepare_public_keys(root_home)

    assert target.read_text(encoding="utf-8") == f"{VALID_SSH_KEY}\n"
    assert target.stat().st_ino != old_inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(ssh_dir.glob(".authorized_keys.*.tmp")) == []


def test_replace_failure_preserves_old_target_and_cleans_owned_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    target = ssh_dir / "authorized_keys"
    target.write_text("old key material\n", encoding="utf-8")
    target.chmod(0o600)

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("simulated replace failure")

    monkeypatch.setattr(ssh_module.os, "replace", fail_replace)

    with pytest.raises(SshCredentialPreparationError) as raised:
        _prepare_public_keys(root_home)

    assert str(raised.value) == "failed to prepare root SSH authorized keys"
    assert target.read_text(encoding="utf-8") == "old key material\n"
    assert list(ssh_dir.glob(".authorized_keys.*.tmp")) == []


def test_successful_write_fsyncs_regular_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            events.append("fsync:file")
        else:
            events.append(f"fsync:{Path(os.readlink(f'/proc/self/fd/{descriptor}'))}")
        real_fsync(descriptor)

    def record_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(ssh_module.os, "fsync", record_fsync)
    monkeypatch.setattr(ssh_module.os, "replace", record_replace)

    _prepare_public_keys(root_home)

    assert events == [
        "fsync:file",
        "replace",
        f"fsync:{ssh_dir}",
        f"fsync:{root_home}",
    ]


def test_existing_ssh_directory_skips_root_home_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            events.append("fsync:file")
        else:
            events.append(f"fsync:{Path(os.readlink(f'/proc/self/fd/{descriptor}'))}")
        real_fsync(descriptor)

    def record_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(ssh_module.os, "fsync", record_fsync)
    monkeypatch.setattr(ssh_module.os, "replace", record_replace)

    _prepare_public_keys(root_home)

    assert events == ["fsync:file", "replace", f"fsync:{ssh_dir}"]


def test_directory_fsync_failure_reports_durability_after_atomic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    target = ssh_dir / "authorized_keys"
    target.write_text("old key material\n", encoding="utf-8")
    target.chmod(0o600)
    old_inode = target.stat().st_ino
    real_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(ssh_module.os, "fsync", fail_directory_fsync)

    with pytest.raises(SshCredentialPreparationError) as raised:
        _prepare_public_keys(root_home)

    assert str(raised.value) == "failed to make root SSH authorized keys durable"
    assert target.read_text(encoding="utf-8") == f"{VALID_SSH_KEY}\n"
    assert target.stat().st_ino != old_inode
    assert list(ssh_dir.glob(".authorized_keys.*.tmp")) == []


def test_writer_leaves_unowned_similarly_named_entry_untouched(tmp_path: Path) -> None:
    root_home = _create_root_home(tmp_path)
    ssh_dir = root_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    unrelated = ssh_dir / ".authorized_keys.preexisting.tmp"
    unrelated.write_text("unowned\n", encoding="utf-8")

    _prepare_public_keys(root_home)

    assert unrelated.read_text(encoding="utf-8") == "unowned\n"
    assert list(ssh_dir.glob(".authorized_keys.*.tmp")) == [unrelated]


# sshd argument tests lock down the effective auth mode passed to OpenSSH.
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


# Startup tests protect host-key generation, foreground argv, and redaction.
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
    root_home = _create_root_home(tmp_path)
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
    root_home = _create_root_home(tmp_path)
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
        credential_fchown=ownership.fchown,
        credential_fchmod=ownership.fchmod,
        credential_owner_uid=os.getuid(),
        credential_owner_gid=os.getgid(),
        command_runner=command_runner,
        process_starter=process_starter,
        log=messages.append,
    )

    authorized_keys = root_home / ".ssh" / "authorized_keys"
    assert result is process
    assert authorized_keys.read_text(encoding="utf-8") == f"{VALID_SSH_KEY}\n"
    assert ownership.chown_calls == [
        (root_home / ".ssh", os.getuid(), os.getgid()),
    ]
    assert ownership.chmod_calls == [
        (root_home / ".ssh", 0o700),
    ]
    assert ownership.fchown_calls == [(os.getuid(), os.getgid())]
    assert ownership.fchmod_calls == [0o600]
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
            runtime=ContainerRuntime(),
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


@pytest.mark.parametrize(
    ("public_key", "leaked_fragment"),
    [
        pytest.param(
            f"{VALID_SSH_KEY}\nssh-ed25519 injected",
            "injected",
            id="embedded-line-feed",
        ),
        pytest.param(
            f"{VALID_SSH_KEY}\x00comment",
            "comment",
            id="embedded-nul",
        ),
    ],
)
def test_prepare_root_ssh_credentials_rejects_control_public_key_before_write(
    tmp_path: Path,
    public_key: str,
    leaked_fragment: str,
) -> None:
    root_home = tmp_path / "root"

    with pytest.raises(SshCredentialPreparationError) as raised:
        prepare_root_ssh_credentials(
            RuntimeSystemSshConfig(
                enable=True,
                pub_keys=[public_key],
            ),
            root_home=root_home,
        )

    error = str(raised.value)
    assert "single authorized_keys line" in error
    assert VALID_SSH_KEY not in error
    assert leaked_fragment not in error
    assert not (root_home / ".ssh" / "authorized_keys").exists()


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
