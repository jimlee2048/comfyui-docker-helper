"""Subprocess and hook runners for container-side helpers."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from comfyui_docker_helper.errors import ApplicationError

_HOOK_SUFFIXES = frozenset({".py", ".sh"})


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
    scripts_dir: str | Path,
    runtime: ContainerRuntime = _DEFAULT_RUNTIME,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one validated hook script with the configured container runtime."""

    script_path = resolve_hook_path(hook, scripts_dir=scripts_dir)
    hook_env = runtime.env(env)
    if script_path.suffix == ".sh":
        argv = ["bash", str(script_path)]
    elif script_path.suffix == ".py":
        argv = [str(runtime.python), str(script_path)]
    else:  # pragma: no cover - resolve_hook_path already rejects this branch.
        raise ContainerCommandError(f"unsupported hook extension: {hook}")

    return run_argv(
        argv,
        cwd=runtime.comfyui_path,
        env=hook_env,
        description=f"hook {hook}",
    )


def run_hooks(
    hooks: Sequence[str],
    *,
    scripts_dir: str | Path,
    runtime: ContainerRuntime = _DEFAULT_RUNTIME,
    env: Mapping[str, str] | None = None,
) -> None:
    """Run hooks in declaration order and stop on the first failure."""

    hook_env = runtime.env(env)
    for hook in hooks:
        run_hook(hook, scripts_dir=scripts_dir, runtime=runtime, env=hook_env)


def resolve_hook_path(hook: str, *, scripts_dir: str | Path) -> Path:
    """Resolve a hook path below scripts-dir and repeat runtime validation."""

    hook_path = PurePosixPath(hook)
    if hook_path.is_absolute():
        raise ContainerCommandError(
            f"hook path must be relative to scripts-dir: {hook}"
        )
    if ".." in hook_path.parts:
        raise ContainerCommandError(f"hook path must not contain '..': {hook}")
    if hook_path.suffix not in _HOOK_SUFFIXES:
        raise ContainerCommandError(f"hook path must end in .sh or .py: {hook}")

    source = Path(scripts_dir).joinpath(*hook_path.parts)
    if not source.is_file():
        raise ContainerCommandError(
            f"hook script does not exist or is not a file: {hook}"
        )
    return source


def _format_argv(argv: Sequence[str]) -> str:
    return " ".join(map(_quote_for_message, argv))


def _quote_for_message(argument: str) -> str:
    if not argument:
        return "''"
    if any(character.isspace() for character in argument):
        return repr(argument)
    return argument
