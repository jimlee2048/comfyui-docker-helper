"""Host-side Docker Buildx invocation service."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from python_on_whales import DockerClient
from python_on_whales.exceptions import DockerException

from comfyui_docker_helper.errors import ApplicationError

type BuildxOutput = Literal["load", "push"]


class BuildxBuildError(ApplicationError):
    """A user-facing Docker Buildx invocation failure."""


BuildxLogger = Callable[[str], None]


def build_image_with_buildx(
    *,
    image_tags: Sequence[str],
    output: BuildxOutput = "load",
    context_dir: str | Path,
    platforms: Sequence[str] = ("linux/amd64",),
    cwd: str | Path | None = None,
    log: BuildxLogger = print,
) -> None:
    """Build one image through the public python-on-whales Buildx API."""
    base_directory = Path(cwd) if cwd is not None else Path.cwd()
    resolved_context = Path(context_dir)
    if not resolved_context.is_absolute():
        resolved_context = base_directory / resolved_context
    resolved_context = resolved_context.resolve()
    tags = tuple(image_tags)
    target_platforms = tuple(platforms)
    tag_summary = ", ".join(tags)
    log(
        "Running Docker Buildx "
        f"({output}) for {tag_summary} on {', '.join(target_platforms)}"
    )

    try:
        stream = DockerClient().buildx.build(
            resolved_context,
            tags=list(tags),
            load=output == "load",
            push=output == "push",
            platforms=list(target_platforms),
            progress="plain",
            stream_logs=True,
        )
        if stream is None:  # pragma: no cover - public API contract
            raise BuildxBuildError("Docker Buildx did not provide its live log stream")
        for message in stream:
            log(message.rstrip("\n"))
    except FileNotFoundError as error:
        raise BuildxBuildError(
            "Docker was not found; install Docker with Buildx and ensure it is on PATH"
        ) from error
    except KeyboardInterrupt:
        raise
    except OSError as error:
        raise BuildxBuildError(
            f"Docker Buildx could not be started: {error}"
        ) from error
    except DockerException as error:
        raise BuildxBuildError(
            f"Docker Buildx failed with exit code {error.return_code}"
        ) from error

    if output == "push":
        log(f"Docker Buildx pushed image tags: {tag_summary}")
    else:
        log(f"Docker Buildx loaded image tags: {tag_summary}")
