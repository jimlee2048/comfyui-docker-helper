"""Host-side Docker Buildx invocation service."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from comfyui_docker_helper.errors import ApplicationError


class BuildxBuildError(ApplicationError):
    """A user-facing Docker Buildx invocation failure."""


@dataclass(frozen=True, slots=True)
class BuildxBuildResult:
    """Successful Buildx invocation details."""

    argv: tuple[str, ...]
    context_dir: Path
    image_tag: str


class BuildxRunner(Protocol):
    """Injectable subprocess-compatible runner."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[object]: ...


BuildxLogger = Callable[[str], None]


def build_image_with_buildx(
    *,
    image_tag: str,
    context_dir: str | Path,
    cwd: str | Path | None = None,
    docker_executable: str = "docker",
    runner: BuildxRunner = subprocess.run,
    log: BuildxLogger = print,
) -> BuildxBuildResult:
    """Run the supported v0.1 Docker Buildx command and stream output.

    The subprocess inherits stdout/stderr from the current process because no
    capture arguments are passed. v0.1 intentionally does not use the Docker
    SDK, retry, or fall back to ``docker build``.
    """
    resolved_context = Path(context_dir)
    argv = (
        docker_executable,
        "buildx",
        "build",
        "--load",
        "-t",
        image_tag,
        str(resolved_context),
    )
    log(f"Running Docker Buildx: {shlex.join(argv)}")

    try:
        completed = runner(argv, cwd=cwd, check=False)
    except FileNotFoundError as error:
        raise BuildxBuildError(
            f"{docker_executable!r} executable was not found; install Docker "
            "with Buildx and ensure it is on PATH"
        ) from error
    except KeyboardInterrupt:
        raise
    except OSError as error:
        raise BuildxBuildError(
            f"Docker Buildx could not be started: {error}"
        ) from error
    except subprocess.SubprocessError as error:
        raise BuildxBuildError(f"Docker Buildx failed to start: {error}") from error

    if completed.returncode != 0:
        raise BuildxBuildError(
            f"Docker Buildx failed with exit code {completed.returncode}"
        )

    log(f"Docker Buildx loaded image: {image_tag}")
    return BuildxBuildResult(
        argv=argv,
        context_dir=resolved_context,
        image_tag=image_tag,
    )
