"""Subprocess and hook runners for container-side helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from comfyui_docker_helper.config.hook_validation import (
    validate_hook_digest,
    validate_hook_relative_path,
)
from comfyui_docker_helper.errors import ApplicationError


class ContainerCommandError(ApplicationError):
    """A user-facing container helper process failure."""


@dataclass(frozen=True, slots=True)
class ContainerRuntime:
    """Container paths and environment used by helper subprocesses."""

    workspace: Path = Path("/workspace")
    comfyui_path: Path = Path("/workspace/ComfyUI")
    virtual_env: Path = Path("/opt/venv")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> ContainerRuntime:
        """Build a CLI runtime from Docker-managed environment variables."""

        source = os.environ if env is None else env
        missing = [
            name for name in ("WORKSPACE", "COMFYUI_PATH") if not source.get(name)
        ]
        if missing:
            names = ", ".join(missing)
            raise ContainerCommandError(
                f"missing required container environment variable(s): {names}"
            )
        virtual_env = source.get("VIRTUAL_ENV", "/opt/venv")
        return cls(
            workspace=Path(source["WORKSPACE"]),
            comfyui_path=Path(source["COMFYUI_PATH"]),
            virtual_env=Path(virtual_env),
        )

    def env(
        self,
        base_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Return a container helper environment with required path variables."""

        source = os.environ if base_env is None else base_env
        env = dict(source)
        venv_bin = str(self.virtual_env / "bin")
        inherited_path = env.get("PATH", "")
        env.update(
            {
                "WORKSPACE": str(self.workspace),
                "COMFYUI_PATH": str(self.comfyui_path),
                "VIRTUAL_ENV": str(self.virtual_env),
                "PATH": (
                    venv_bin
                    if not inherited_path
                    else f"{venv_bin}{os.pathsep}{inherited_path}"
                ),
            }
        )
        return env

    @property
    def python(self) -> Path:
        """Return the Python interpreter inside the configured virtualenv."""

        return self.virtual_env / "bin" / "python"


_DEFAULT_RUNTIME = ContainerRuntime()


def run_argv(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    description: str = "command",
    start_new_session: bool = False,
    close_stdin: bool = False,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Run an argv subprocess with inherited stdout/stderr and strict failure."""

    command = [os.fspath(argument) for argument in argv]
    if not command:
        raise ContainerCommandError(f"{description} argv must not be empty")

    try:
        run_kwargs: dict[str, object] = {
            "cwd": os.fspath(cwd),
            "env": dict(env),
            "shell": False,
            "check": False,
        }
        if start_new_session:
            run_kwargs["start_new_session"] = True
        if close_stdin:
            run_kwargs["stdin"] = subprocess.DEVNULL
        if pass_fds:
            run_kwargs["pass_fds"] = pass_fds
        result = subprocess.run(command, **run_kwargs)
    except FileNotFoundError as error:
        raise ContainerCommandError(
            f"{description} executable not found: {command[0]}"
        ) from error
    except OSError as error:
        raise ContainerCommandError(
            f"{description} failed to start: {error}"
        ) from error

    if result.returncode != 0:
        exit_code = result.returncode if result.returncode > 0 else 1
        raise ContainerCommandError(
            f"{description} failed with exit code {result.returncode}: "
            f"{_format_argv(command)}",
            exit_code=exit_code,
        )
    return result


def start_argv(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    description: str = "command",
    start_new_session: bool = False,
) -> subprocess.Popen[bytes]:
    """Start an argv subprocess with inherited stdout/stderr."""

    command = [os.fspath(argument) for argument in argv]
    if not command:
        raise ContainerCommandError(f"{description} argv must not be empty")

    try:
        popen_kwargs: dict[str, object] = {
            "cwd": os.fspath(cwd),
            "env": dict(env),
            "shell": False,
        }
        if start_new_session:
            popen_kwargs["start_new_session"] = True
        return subprocess.Popen(command, **popen_kwargs)
    except FileNotFoundError as error:
        raise ContainerCommandError(
            f"{description} executable not found: {command[0]}"
        ) from error
    except OSError as error:
        raise ContainerCommandError(
            f"{description} failed to start: {error}"
        ) from error


def run_hook(
    hook: str,
    *,
    expected_digest: str,
    scripts_dir: str | Path,
    runtime: ContainerRuntime = _DEFAULT_RUNTIME,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one content-locked hook through an immutable inherited descriptor."""

    hook_path = _validate_hook_path(hook)
    _validate_hook_digest(expected_digest)
    source_fd = _open_hook(hook_path, scripts_dir=scripts_dir)
    try:
        sealed_fd = _seal_hook(source_fd, expected_digest=expected_digest, hook=hook)
    finally:
        os.close(source_fd)

    try:
        operand = f"/proc/self/fd/{sealed_fd}"
        if hook_path.suffix == ".sh":
            argv = ["bash", operand]
        else:
            argv = [str(runtime.python), operand]
        return run_argv(
            argv,
            cwd=runtime.comfyui_path,
            env=runtime.env(env),
            description=f"hook {hook}",
            pass_fds=(sealed_fd,),
        )
    finally:
        os.close(sealed_fd)


def _validate_hook_path(hook: str) -> PurePosixPath:
    try:
        return PurePosixPath(validate_hook_relative_path(hook))
    except (TypeError, ValueError) as error:
        message = str(error)
        if "canonical safe POSIX path" in message:
            message = "hook path must be one canonical relative path"
        raise ContainerCommandError(message) from error


def _validate_hook_digest(digest: str) -> None:
    try:
        validate_hook_digest(digest)
    except (TypeError, ValueError) as error:
        raise ContainerCommandError("hook expected digest is invalid") from error


def _open_hook(path: PurePosixPath, *, scripts_dir: str | Path) -> int:
    scripts_parts = _validate_scripts_root(scripts_dir)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    directory_fd: int | None = None
    source_fd: int | None = None
    try:
        directory_fd = os.open("/", directory_flags)
        for part in (*scripts_parts, *path.parts[:-1]):
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            previous_fd = directory_fd
            directory_fd = None
            try:
                os.close(previous_fd)
            except OSError:
                os.close(next_fd)
                raise
            directory_fd = next_fd
        source_fd = os.open(
            path.parts[-1],
            file_flags,
            dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise ContainerCommandError(
                "hook must reference one regular non-symlink file"
            )
        result = source_fd
        source_fd = None
        return result
    except ContainerCommandError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise ContainerCommandError(
            "hook must reference one regular non-symlink file"
        ) from error
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _validate_scripts_root(scripts_dir: str | Path) -> tuple[str, ...]:
    value = os.fspath(scripts_dir)
    if not isinstance(value, str):
        raise ContainerCommandError(
            "hook scripts root must be one canonical absolute path"
        )
    path = PurePosixPath(value)
    if (
        not value
        or not path.is_absolute()
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ContainerCommandError(
            "hook scripts root must be one canonical absolute path"
        )
    return path.parts[1:]


def _seal_hook(source_fd: int, *, expected_digest: str, hook: str) -> int:
    try:
        import fcntl

        # Standalone Python builds may omit names from the stable Linux UAPI.
        add_seals = getattr(fcntl, "F_ADD_SEALS", 1033)
        get_seals = getattr(fcntl, "F_GET_SEALS", 1034)
        required_seals = (
            getattr(fcntl, "F_SEAL_WRITE", 0x0008)
            | getattr(fcntl, "F_SEAL_GROW", 0x0004)
            | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
            | getattr(fcntl, "F_SEAL_SEAL", 0x0001)
        )
    except ImportError as error:
        raise ContainerCommandError(
            "immutable hook execution is unavailable"
        ) from error

    sealed_fd: int | None = None
    complete = False
    try:
        sealed_fd = _create_memfd()
        while chunk := os.read(source_fd, 1024 * 1024):
            _write_all(sealed_fd, chunk)
        fcntl.fcntl(sealed_fd, add_seals, required_seals)
        observed_seals = fcntl.fcntl(sealed_fd, get_seals)
        if observed_seals & required_seals != required_seals:
            raise ContainerCommandError("hook content could not be sealed")
        os.lseek(sealed_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while chunk := os.read(sealed_fd, 1024 * 1024):
            digest.update(chunk)
        observed_digest = f"sha256:{digest.hexdigest()}"
        if not hmac.compare_digest(observed_digest, expected_digest):
            raise ContainerCommandError(f"hook digest does not match: {hook}")
        os.lseek(sealed_fd, 0, os.SEEK_SET)
        complete = True
        return sealed_fd
    except ContainerCommandError:
        raise
    except OSError as error:
        raise ContainerCommandError("hook content could not be sealed") from error
    finally:
        if sealed_fd is not None and not complete:
            os.close(sealed_fd)


def _create_memfd() -> int:
    flags = getattr(os, "MFD_ALLOW_SEALING", 0x0002) | getattr(
        os, "MFD_CLOEXEC", 0x0001
    )
    create = getattr(os, "memfd_create", None)
    if create is not None:
        return create("cdh-hook", flags)

    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        create = libc.memfd_create
    except (AttributeError, ImportError) as error:
        raise OSError("memfd_create is unavailable") from error
    create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    create.restype = ctypes.c_int
    fd = create(b"cdh-hook", flags)
    if fd < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return fd


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written <= 0:
            raise OSError("short memfd write")
        offset += written


def _format_argv(argv: Sequence[str]) -> str:
    return " ".join(map(_quote_for_message, argv))


def _quote_for_message(argument: str) -> str:
    if not argument:
        return "''"
    if any(character.isspace() for character in argument):
        return repr(argument)
    return argument
