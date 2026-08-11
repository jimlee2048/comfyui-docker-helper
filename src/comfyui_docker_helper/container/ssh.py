"""Container-side SSH credential preparation helpers."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from comfyui_docker_helper.config import RuntimeConfig, RuntimeSystemSshConfig
from comfyui_docker_helper.config.ssh_keys import normalize_ssh_public_keys
from comfyui_docker_helper.container.process_control import (
    DirectProcess,
    terminate_direct_process_until,
)
from comfyui_docker_helper.container.runners import ContainerRuntime
from comfyui_docker_helper.errors import ApplicationError

_ROOT_UID = 0
_ROOT_GID = 0
type Chown = Callable[
    [str | bytes | os.PathLike[str] | os.PathLike[bytes], int, int],
    None,
]
type Chmod = Callable[[str | bytes | os.PathLike[str] | os.PathLike[bytes], int], None]
type Fchown = Callable[[int, int, int], None]
type Fchmod = Callable[[int, int], None]
type PreparationProcessObserver = Callable[[DirectProcess | None], object]


class SshCredentialPreparationError(ApplicationError):
    """A user-facing SSH credential preparation failure."""


class SshdStartupError(ApplicationError):
    """A user-facing sshd startup failure."""


class SensitiveCommandRunner(Protocol):
    """Run a command whose sensitive input is provided out-of-band."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input_data: bytes,
        description: str,
    ) -> int:
        """Run argv with sensitive stdin bytes and return the process exit code."""


class CommandRunner(Protocol):
    """Run a non-sensitive argv command."""

    def __call__(self, argv: Sequence[str], *, description: str) -> int:
        """Run argv and return the process exit code."""


class SshdProcess(Protocol):
    """Minimal sshd child process interface used by the entrypoint."""

    returncode: int | None

    def wait(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class SshdProcessStarter(Protocol):
    """Start foreground sshd and return its child process handle."""

    def __call__(self, argv: Sequence[str], *, description: str) -> SshdProcess:
        """Start argv without shell expansion."""


@dataclass(frozen=True, slots=True)
class RootSshCredentialPreparationStatus:
    """Structured result for later SSH activation decisions."""

    ssh_enabled: bool
    public_key_count: int
    password_configured: bool
    authorized_keys_path: Path | None = None
    root_password_set: bool = False
    root_unlocked: bool = False

    @property
    def has_credentials(self) -> bool:
        """Return whether SSH has at least one effective credential."""
        return self.public_key_count > 0 or self.password_configured


def start_sshd_if_enabled(
    config: RuntimeConfig,
    *,
    runtime: ContainerRuntime,
    log: Callable[[str], object] = print,
    root_home: Path = Path("/root"),
    runtime_dir: Path = Path("/run/sshd"),
    credential_command_runner: SensitiveCommandRunner | None = None,
    credential_chown: Chown | None = None,
    credential_chmod: Chmod | None = None,
    credential_fchown: Fchown | None = None,
    credential_fchmod: Fchmod | None = None,
    credential_owner_uid: int = _ROOT_UID,
    credential_owner_gid: int = _ROOT_GID,
    command_runner: CommandRunner | None = None,
    process_starter: SshdProcessStarter | None = None,
    preparation_process_observer: PreparationProcessObserver = lambda _process: None,
) -> SshdProcess | None:
    """Prepare and start foreground sshd when effective SSH is active."""
    del runtime
    ssh = config.system.ssh
    if not ssh.enable:
        return None

    try:
        status = prepare_root_ssh_credentials(
            ssh,
            root_home=root_home,
            command_runner=credential_command_runner,
            chown=credential_chown,
            chmod=credential_chmod,
            fchown=credential_fchown,
            fchmod=credential_fchmod,
            owner_uid=credential_owner_uid,
            owner_gid=credential_owner_gid,
            process_observer=preparation_process_observer,
        )
    except SshCredentialPreparationError as error:
        raise SshdStartupError(f"SSH credential preparation failed: {error}") from error

    if not status.has_credentials:
        log("WARNING: SSH is enabled but no root SSH credentials are configured")
        return None

    run_command = (
        (
            lambda argv, *, description: _run_command(
                argv,
                description=description,
                process_observer=preparation_process_observer,
            )
        )
        if command_runner is None
        else command_runner
    )
    starter = _start_process if process_starter is None else process_starter
    _ensure_host_keys(run_command)
    _ensure_sshd_runtime_dir(runtime_dir)
    argv = build_sshd_argv(ssh, status)
    child = _start_foreground_sshd(argv, starter)
    return child


def build_sshd_argv(
    ssh: RuntimeSystemSshConfig,
    status: RootSshCredentialPreparationStatus,
) -> list[str]:
    """Build cdh-controlled foreground sshd argv without credential material."""
    return [
        "/usr/sbin/sshd",
        "-f",
        "/dev/null",
        "-D",
        "-e",
        "-o",
        f"Port={ssh.port}",
        "-o",
        "PermitRootLogin=yes",
        "-o",
        f"PasswordAuthentication={_yes_no(status.password_configured)}",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        f"PubkeyAuthentication={_yes_no(status.public_key_count > 0)}",
        "-o",
        "AuthorizedKeysFile=/root/.ssh/authorized_keys",
    ]


def prepare_root_ssh_credentials(
    config: RuntimeConfig | RuntimeSystemSshConfig,
    *,
    root_home: Path = Path("/root"),
    command_runner: SensitiveCommandRunner | None = None,
    chown: Chown | None = None,
    chmod: Chmod | None = None,
    fchown: Fchown | None = None,
    fchmod: Fchmod | None = None,
    owner_uid: int = _ROOT_UID,
    owner_gid: int = _ROOT_GID,
    process_observer: PreparationProcessObserver = lambda _process: None,
) -> RootSshCredentialPreparationStatus:
    """Prepare root SSH credentials without starting sshd."""
    ssh = _coerce_ssh_config(config)
    runner = (
        (
            lambda argv, *, input_data, description: _run_sensitive_command(
                argv,
                input_data=input_data,
                description=description,
                process_observer=process_observer,
            )
        )
        if command_runner is None
        else command_runner
    )
    chown_func = os.chown if chown is None else chown
    chmod_func = os.chmod if chmod is None else chmod
    fchown_func = os.fchown if fchown is None else fchown
    fchmod_func = os.fchmod if fchmod is None else fchmod

    if not ssh.enable:
        return RootSshCredentialPreparationStatus(
            ssh_enabled=False,
            public_key_count=0,
            password_configured=False,
        )
    if ssh.password:
        _validate_password_for_chpasswd(ssh.password)

    public_keys = _normalize_runtime_public_keys(ssh.pub_keys)
    authorized_keys_path = None
    if public_keys:
        authorized_keys_path = _write_authorized_keys(
            public_keys,
            root_home=root_home,
            chown=chown_func,
            chmod=chmod_func,
            fchown=fchown_func,
            fchmod=fchmod_func,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )

    root_password_set = False
    root_unlocked = False
    if ssh.password:
        _set_root_password(ssh.password, runner)
        root_password_set = True
        _unlock_root(runner)
        root_unlocked = True

    return RootSshCredentialPreparationStatus(
        ssh_enabled=ssh.enable,
        public_key_count=len(public_keys),
        password_configured=bool(ssh.password),
        authorized_keys_path=authorized_keys_path,
        root_password_set=root_password_set,
        root_unlocked=root_unlocked,
    )


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _ensure_host_keys(runner: CommandRunner) -> None:
    _run_checked_command(
        ["/usr/bin/ssh-keygen", "-A"],
        description="generate OpenSSH host keys",
        runner=runner,
    )


def _ensure_sshd_runtime_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
    except OSError as error:
        raise SshdStartupError("failed to create sshd runtime directory") from error


def _start_foreground_sshd(
    argv: Sequence[str],
    starter: SshdProcessStarter,
) -> SshdProcess:
    try:
        child = starter(argv, description="start sshd")
    except SshdStartupError:
        raise
    except OSError as error:
        raise SshdStartupError("sshd failed to start") from error
    returncode = child.poll()
    if returncode is not None:
        raise SshdStartupError(f"sshd exited during startup with code {returncode}")
    return child


def _coerce_ssh_config(
    config: RuntimeConfig | RuntimeSystemSshConfig,
) -> RuntimeSystemSshConfig:
    if isinstance(config, RuntimeConfig):
        return config.system.ssh
    return config


def _run_checked_command(
    argv: Sequence[str],
    *,
    description: str,
    runner: CommandRunner,
) -> None:
    returncode = runner(argv, description=description)
    if returncode != 0:
        exit_code = returncode if returncode > 0 else 1
        raise SshdStartupError(
            f"{description} failed with exit code {returncode}: {_format_argv(argv)}",
            exit_code=exit_code,
        )


def _run_command(
    argv: Sequence[str],
    *,
    description: str,
    process_observer: PreparationProcessObserver = lambda _process: None,
) -> int:
    command = list(argv)
    if not command:
        raise SshdStartupError(f"{description} argv must not be empty")
    try:
        process = subprocess.Popen(
            command,
            shell=False,
        )
    except FileNotFoundError as error:
        raise SshdStartupError(
            f"{description} executable not found: {command[0]}"
        ) from error
    except OSError as error:
        raise SshdStartupError(f"{description} failed to start") from error
    process_observer(process)
    try:
        return process.wait()
    except BaseException:
        terminate_direct_process_until(
            process,
            deadline=time.monotonic() + 1.0,
            poll_interval=0.05,
        )
        raise
    finally:
        process_observer(None)


def _start_process(argv: Sequence[str], *, description: str) -> SshdProcess:
    command = list(argv)
    if not command:
        raise SshdStartupError(f"{description} argv must not be empty")
    try:
        return subprocess.Popen(command, shell=False)
    except FileNotFoundError as error:
        raise SshdStartupError(
            f"{description} executable not found: {command[0]}"
        ) from error
    except OSError as error:
        raise SshdStartupError(f"{description} failed to start") from error


def _write_authorized_keys(
    public_keys: tuple[str, ...],
    *,
    root_home: Path,
    chown: Chown,
    chmod: Chmod,
    fchown: Fchown,
    fchmod: Fchmod,
    owner_uid: int,
    owner_gid: int,
) -> Path:
    ssh_dir = root_home / ".ssh"
    authorized_keys = ssh_dir / "authorized_keys"
    try:
        _validate_root_home(root_home, owner_uid=owner_uid, owner_gid=owner_gid)
        ssh_directory_created = _ensure_root_ssh_directory(
            ssh_dir,
            chown=chown,
            chmod=chmod,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        _validate_authorized_keys_target(
            authorized_keys,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        _atomic_replace_authorized_keys(
            authorized_keys,
            "".join(f"{key}\n" for key in public_keys).encode(),
            fchown=fchown,
            fchmod=fchmod,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            sync_root_home=ssh_directory_created,
        )
    except SshCredentialPreparationError:
        raise
    except OSError as error:
        raise SshCredentialPreparationError(
            "failed to prepare root SSH authorized keys"
        ) from error
    return authorized_keys


def _validate_root_home(path: Path, *, owner_uid: int, owner_gid: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise SshCredentialPreparationError(
            "root SSH home must be an existing root-owned directory with a safe mode"
        ) from error
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_gid != owner_gid
        or mode & 0o022
    ):
        raise SshCredentialPreparationError(
            "root SSH home must be an existing root-owned directory with a safe mode"
        )


def _ensure_root_ssh_directory(
    path: Path,
    *,
    chown: Chown,
    chmod: Chmod,
    owner_uid: int,
    owner_gid: int,
) -> bool:
    created = False
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        created = True
        chown(path, owner_uid, owner_gid)
        chmod(path, 0o700)
        metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_gid != owner_gid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SshCredentialPreparationError(
            "root SSH directory must be a root-owned directory with mode 0700"
        )
    return created


def _validate_authorized_keys_target(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_gid != owner_gid
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise SshCredentialPreparationError(
            "root SSH authorized keys must be a root-owned regular file with mode 0600"
        )


def _atomic_replace_authorized_keys(
    target: Path,
    content: bytes,
    *,
    fchown: Fchown,
    fchmod: Fchmod,
    owner_uid: int,
    owner_gid: int,
    sync_root_home: bool,
) -> None:
    temporary_path: Path | None = None
    committed = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".authorized_keys.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise SshCredentialPreparationError(
                    "root SSH authorized keys temporary must be a regular file"
                )
            fchown(stream.fileno(), owner_uid, owner_gid)
            fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        committed = True
        try:
            _sync_directory(target.parent)
            if sync_root_home:
                _sync_directory(target.parent.parent)
        except OSError as error:
            raise SshCredentialPreparationError(
                "failed to make root SSH authorized keys durable"
            ) from error
    finally:
        if temporary_path is not None and not committed:
            temporary_path.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _normalize_runtime_public_keys(values: list[str]) -> tuple[str, ...]:
    normalization = normalize_ssh_public_keys(
        values,
        path=("system", "ssh", "pub_keys"),
        code="ssh.invalid_public_key",
    )
    if normalization.diagnostics:
        raise SshCredentialPreparationError(normalization.diagnostics[0].message)
    return normalization.values


def _set_root_password(password: str, runner: SensitiveCommandRunner) -> None:
    input_data = f"root:{password}\n".encode()
    _run_checked_sensitive_command(
        ["chpasswd"],
        input_data=input_data,
        description="set root SSH password",
        runner=runner,
    )


def _validate_password_for_chpasswd(password: str) -> None:
    if any(character in password for character in ("\n", "\r", "\x00")):
        raise SshCredentialPreparationError(
            "SSH password must not contain line breaks or NUL bytes"
        )


def _unlock_root(runner: SensitiveCommandRunner) -> None:
    _run_checked_sensitive_command(
        ["passwd", "-u", "root"],
        input_data=b"",
        description="unlock root SSH account",
        runner=runner,
    )


def _run_checked_sensitive_command(
    argv: Sequence[str],
    *,
    input_data: bytes,
    description: str,
    runner: SensitiveCommandRunner,
) -> None:
    returncode = runner(argv, input_data=input_data, description=description)
    if returncode != 0:
        exit_code = returncode if returncode > 0 else 1
        raise SshCredentialPreparationError(
            f"{description} failed with exit code {returncode}: {_format_argv(argv)}",
            exit_code=exit_code,
        )


def _run_sensitive_command(
    argv: Sequence[str],
    *,
    input_data: bytes,
    description: str,
    process_observer: PreparationProcessObserver = lambda _process: None,
) -> int:
    command = list(argv)
    if not command:
        raise SshCredentialPreparationError(f"{description} argv must not be empty")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            shell=False,
        )
    except FileNotFoundError as error:
        raise SshCredentialPreparationError(
            f"{description} executable not found: {command[0]}"
        ) from error
    except OSError as error:
        raise SshCredentialPreparationError(f"{description} failed to start") from error
    process_observer(process)
    try:
        process.communicate(input_data)
        assert process.returncode is not None
        return process.returncode
    except BaseException:
        terminate_direct_process_until(
            process,
            deadline=time.monotonic() + 1.0,
            poll_interval=0.05,
        )
        raise
    finally:
        process_observer(None)


def _format_argv(argv: Sequence[str]) -> str:
    return " ".join(argv)
